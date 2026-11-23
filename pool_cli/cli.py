"""ipool: a command-line client of the Isabelle Pool Server, for agents that call it from a shell.

It talks to the Pool Server's HTTP API directly (no MCP layer) and keeps, between invocations, what
the MCP layer keeps in memory: which server session (and lease) holds each .thy file, and the hash of
the text last loaded into it. Every file command re-reads the file and loads it only if it changed
(the server re-checks incrementally from the first changed command), and on 403/404 (lease or
session gone) acquires a fresh session and loads again.

Long commands (``check``, ``fill``, ``build-heap``, ``sh``) can report progress: with
``--progress SECS`` they print one line per change of state (and a heartbeat at most every SECS
seconds while nothing changes) before the final result, so a caller that streams stdout (Claude
Code's Monitor) gets several messages. Every command ends with ONE line of JSON, its result.

Run: ``python -m pool_cli <command> ...`` on the machine of the Pool Server (the files are read
from its disk). ``python -m pool_cli -h`` lists the commands.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import sys
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

# The header context below reads its knobs from the environment at import time: start sessions on
# the image the imports name, as the resident MCP does (compute-env/bin/pool-mcp).
os.environ.setdefault("ISABELLE_MCP_LSP_FIELD_FROM_IMPORTS", "true")

import httpx  # noqa: E402

from client.async_client import PoolAsyncClient  # noqa: E402
from mcp_servers.lsp.header_context import file_context  # noqa: E402
from mcp_servers.lsp.sibling_imports import master_dir_of  # noqa: E402

URL = os.environ.get("IPOOL_URL", "http://127.0.0.1:8000")
STATE_DIR = os.environ.get("IPOOL_STATE_DIR", os.path.expanduser("~/.cache/ipool"))
LOAD_TIMEOUT = float(os.environ.get("IPOOL_LOAD_TIMEOUT", "30"))
IDLE_RELEASE_S = float(os.environ.get("IPOOL_IDLE_RELEASE_S", "1800"))
DEFAULT_TASK_GROUP = os.environ.get("IPOOL_TASK_GROUP", "default")
HTTP_TIMEOUT = 3600.0  # a heap build or a long sledgehammer must not be cut by the client

FILL_TERMINAL = ("done", "cancelled", "error")


# ------------------------------------------------------------------ output

class Out:
    """Progress lines (only with --progress) and the final JSON line."""

    def __init__(self, every: float) -> None:
        self.every = every
        self.last = 0.0

    def progress(self, msg: str, force: bool = True) -> None:
        """Print a progress line: always when `force` (a change of state), else only when the
        last line is at least `every` seconds old (a heartbeat)."""
        if not self.every:
            return
        now = time.monotonic()
        if not force and now - self.last < self.every:
            return
        self.last = now
        print(f"[ipool {time.strftime('%H:%M:%S')}] {msg}", flush=True)

    @staticmethod
    def result(obj: Any) -> None:
        print(json.dumps(obj, ensure_ascii=False), flush=True)


async def heartbeat(out: Out, aw: Awaitable[Any], label: str) -> Any:
    """Await `aw`, printing `label: running Ns` every --progress seconds meanwhile."""
    task = asyncio.ensure_future(aw)
    if not out.every:
        return await task
    t0 = time.monotonic()
    while True:
        done, _ = await asyncio.wait({task}, timeout=out.every)
        if done:
            return task.result()
        out.progress(f"{label}: running {int(time.monotonic() - t0)}s")


def http_error(e: httpx.HTTPStatusError) -> Dict[str, Any]:
    try:
        detail = e.response.json()
    except ValueError:
        detail = e.response.text[:500]
    if isinstance(detail, dict) and set(detail) == {"detail"}:
        detail = detail["detail"]
    return {"error": "http_" + str(e.response.status_code), "detail": detail}


def rebindable(e: BaseException) -> bool:
    """The session or its lease is gone (reaped, released, evicted): acquire a new one."""
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (403, 404)


# ------------------------------------------------------------------- state

class State:
    """file path -> binding record, in one JSON file guarded by flock."""

    def __init__(self, root: str = STATE_DIR) -> None:
        self.root = root
        os.makedirs(os.path.join(root, "locks"), exist_ok=True)
        self.path = os.path.join(root, "bindings.json")

    @contextlib.contextmanager
    def edit(self):
        with open(os.path.join(self.root, "state.lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            yield data
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)

    def get(self, canon: str) -> Optional[Dict[str, Any]]:
        with self.edit() as d:
            return d.get(canon)

    def put(self, canon: str, rec: Optional[Dict[str, Any]]) -> None:
        with self.edit() as d:
            if rec is None:
                d.pop(canon, None)
            else:
                d[canon] = rec

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self.edit() as d:
            return dict(d)

    @contextlib.contextmanager
    def file_lock(self, canon: str, blocking: bool = True):
        """One command at a time per file (a sync and a call on its session belong together)."""
        name = hashlib.sha1(canon.encode()).hexdigest()[:16]
        with open(os.path.join(self.root, "locks", name), "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            yield True


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


# ----------------------------------------------------------------- binding

class Bound:
    """One file and the server session that holds it."""

    def __init__(self, c: PoolAsyncClient, st: State, path: str,
                 task_group: Optional[str] = None, heap_session: Optional[str] = None) -> None:
        self.c, self.st = c, st
        self.canon = canonical(path)
        if not os.path.isfile(self.canon):
            raise FileNotFoundError(f"file not found: {self.canon}")
        self.rec = st.get(self.canon)
        self.want_group, self.want_heap = task_group, heap_session
        self.report: Optional[Dict[str, Any]] = None  # of the last load in this process

    def text(self) -> str:
        with open(self.canon, encoding="utf-8") as f:
            return f.read()

    def save(self) -> None:
        if self.rec is not None:
            self.rec["used"] = time.time()
        self.st.put(self.canon, self.rec)

    async def acquire(self, text: str) -> None:
        old = self.rec or {}
        group = self.want_group or old.get("task_group") or DEFAULT_TASK_GROUP
        heap = self.want_heap if self.want_heap is not None else old.get("heap_session")
        imports, field, dirs = await file_context(self.c, text, self.canon, heap)
        extra = {"session_dirs": list(dirs)} if dirs else {}
        r = await self.c.acquire_session(
            theories=imports or None, field=field, reuse_dirty=False, task_group=group,
            heap_session=heap, label=f"ipool {self.canon}", **extra)
        self.rec = {"session_id": r["session_id"], "lease_id": r["lease_id"], "task_group": group,
                    "heap_session": heap, "sha": None, "timed_out": False}
        self.save()

    async def _load(self, text: str) -> Dict[str, Any]:
        res = await self.c.load_document(
            self.rec["session_id"], text, report=True, timeout=LOAD_TIMEOUT,
            lease_id=self.rec["lease_id"], master_dir=master_dir_of(self.canon),
            keep_on_timeout=True)
        return res.get("report") or {}

    async def sync(self, force: bool = False) -> bool:
        """Load the file if it changed since the last load (or that load timed out, or `force`).
        Returns whether a load happened; its report is in self.report."""
        text = self.text()
        rebind = self.rec is not None and (
            (self.want_group and self.want_group != self.rec.get("task_group"))
            or (self.want_heap is not None and self.want_heap != self.rec.get("heap_session")))
        if rebind:
            await self.release()
        if self.rec is None:
            await self.acquire(text)
        elif not force and self.rec.get("sha") == sha(text) and not self.rec.get("timed_out"):
            return False
        try:
            rep = await self._load(text)
        except httpx.HTTPStatusError as e:
            if not rebindable(e):
                raise
            await self.acquire(text)
            rep = await self._load(text)
        self.report = rep
        self.rec["sha"] = sha(text)
        self.rec["timed_out"] = bool(rep.get("timed_out"))
        self.save()
        return True

    async def last_report(self) -> Dict[str, Any]:
        if self.report is not None:
            return self.report
        r = await self.call(lambda sid, lease: self.c.get_last_report(sid, lease_id=lease))
        return r.get("report") or {}

    async def call(self, fn: Callable[[str, str], Awaitable[Any]]) -> Any:
        """Run fn(session_id, lease_id); on 403/404 rebind, reload and run it again."""
        try:
            return await fn(self.rec["session_id"], self.rec["lease_id"])
        except httpx.HTTPStatusError as e:
            if not rebindable(e):
                raise
        await self.acquire(self.text())
        await self.sync(force=True)
        return await fn(self.rec["session_id"], self.rec["lease_id"])

    async def release(self, destroy: bool = False) -> None:
        if self.rec is None:
            return
        sid, lease = self.rec["session_id"], self.rec["lease_id"]
        try:
            if destroy:
                await self.c.close_session(sid, lease_id=lease)
            else:
                await self.c.release_session(sid, lease_id=lease)
        except httpx.HTTPStatusError as e:
            if not rebindable(e):
                raise
        self.rec = None
        self.st.put(self.canon, None)


async def release_idle(c: PoolAsyncClient, st: State, older_s: float,
                       keep: Optional[str] = None) -> List[str]:
    """Release the bindings not used for `older_s` seconds (skipping files a command holds)."""
    released = []
    now = time.time()
    for canon, rec in st.all().items():
        if canon == keep or now - rec.get("used", 0) < older_s:
            continue
        with st.file_lock(canon, blocking=False) as got:
            if not got:
                continue
            try:
                await c.release_session(rec["session_id"], lease_id=rec["lease_id"])
            except httpx.HTTPStatusError as e:
                if not rebindable(e):
                    continue  # busy (a fill job runs): keep it
            except httpx.HTTPError:
                continue
            st.put(canon, None)
            released.append(canon)
    return released


# ---------------------------------------------------------------- reports

def counts(rep: Dict[str, Any]) -> Dict[str, int]:
    n: Dict[str, int] = {}
    for cmd in rep.get("commands") or []:
        s = cmd.get("status") or "?"
        n[s] = n.get(s, 0) + 1
    return n


def progress_line(name: str, rep: Dict[str, Any], elapsed: float) -> str:
    n = counts(rep)
    total = sum(n.values())
    running = [cmd.get("line") for cmd in rep.get("commands") or [] if cmd.get("status") == "running"]
    s = f"{name}: {n.get('ok', 0)}/{total} commands ok, {n.get('failed', 0)} failed"
    if running:
        s += f", running at line(s) {running[:5]}"
    return s + f", {int(elapsed)}s"


def diagnostics(rep: Dict[str, Any], severity: Optional[str] = None,
                max_text: Optional[int] = None) -> Dict[str, Any]:
    msgs = []
    for cmd in rep.get("commands") or []:
        start = (cmd.get("range") or {}).get("start") or {}
        for m in cmd.get("messages") or []:
            if severity and m.get("sev") != severity:
                continue
            text = m.get("text") or ""
            if max_text and len(text) > max_text:
                text = text[:max_text] + " …"
            msgs.append({"line": cmd.get("line"), "col": start.get("col"), "kind": cmd.get("kind"),
                         "status": cmd.get("status"), "severity": m.get("sev"), "text": text})
    lines = rep.get("sorry_lines")
    out = {"success": rep.get("success"), "proof_open": rep.get("proof_open"),
           "used_sorry": rep.get("used_sorry") if lines is None else bool(lines),
           "sorry_lines": lines, "messages": msgs}
    if rep.get("timed_out"):
        out["timed_out"] = True
        out["running"] = [cmd.get("line") for cmd in rep.get("commands") or []
                          if cmd.get("status") == "running"]
    return out


async def sync_until_done(b: Bound, out: Out, wait: float) -> Dict[str, Any]:
    """Load the file and, while the load timed out with commands still running, load it again
    (an unchanged text keeps them running) until they finish or `wait` seconds pass."""
    t0 = time.monotonic()
    name = os.path.basename(b.canon)
    await b.sync()
    rep = await b.last_report()
    while rep.get("timed_out") and time.monotonic() - t0 < wait:
        out.progress(progress_line(name, rep, time.monotonic() - t0))
        await b.sync()
        rep = await b.last_report()
    return rep


# --------------------------------------------------------------- commands

async def cmd_sync(c, st, out, a) -> Any:
    b = Bound(c, st, a.file, a.task_group, a.heap_session)
    with st.file_lock(b.canon):
        rep = await sync_until_done(b, out, a.wait)
    return diagnostics(rep, a.severity, a.max_text)


async def cmd_check(c, st, out, a) -> Any:
    sem = asyncio.Semaphore(max(1, a.jobs))
    t0 = time.monotonic()
    done = [0]

    async def one(path: str) -> Dict[str, Any]:
        async with sem:
            try:
                b = Bound(c, st, path, a.task_group, a.heap_session)
                with st.file_lock(b.canon):
                    rep = await sync_until_done(b, out, a.wait)
                d = diagnostics(rep, "error", 300)
                row = {"file": b.canon, "success": d["success"], "sorry_lines": d["sorry_lines"],
                       "errors": [{"line": m["line"], "text": m["text"]} for m in d["messages"]]}
                if d.get("timed_out"):
                    row["timed_out"], row["running"] = True, d["running"]
            except FileNotFoundError as e:
                row = {"file": path, "error": str(e)}
            except httpx.HTTPStatusError as e:
                row = {"file": path, **http_error(e)}
            done[0] += 1
            verdict = ("ERROR" if row.get("error") else "timed out" if row.get("timed_out")
                       else "ok" if row["success"] and not row["sorry_lines"]
                       else f"{len(row['errors'])} error(s), sorry at {row['sorry_lines'] or []}")
            out.progress(f"[{done[0]}/{len(a.files)}] {os.path.basename(path)}: {verdict}, "
                         f"{int(time.monotonic() - t0)}s")
            return row

    rows = await asyncio.gather(*(one(p) for p in a.files))
    ok = sum(1 for r in rows if r.get("success") and not r.get("sorry_lines"))
    return {"files": len(rows), "ok": ok, "results": rows}


async def cmd_goal(c, st, out, a) -> Any:
    b = Bound(c, st, a.file)
    with st.file_lock(b.canon):
        await b.sync()
        return await b.call(lambda sid, l: c.goals_at_line(sid, a.line, lease_id=l))


async def cmd_query(c, st, out, a) -> Any:
    b = Bound(c, st, a.file)
    with st.file_lock(b.canon):
        await b.sync()
        return await b.call(lambda sid, l: c.diagnostic(sid, a.command, lease_id=l))


async def cmd_sh(c, st, out, a) -> Any:
    b = Bound(c, st, a.file)
    with st.file_lock(b.canon):
        await b.sync()
        if a.line is None:
            fn = lambda sid, l: c.sledgehammer(sid, timeout_s=a.timeout, lease_id=l)  # noqa: E731
        else:
            fn = lambda sid, l: c.sledgehammer_at(  # noqa: E731
                sid, a.line, subgoal=a.subgoal, timeout_s=a.timeout, lease_id=l)
        where = f"line {a.line}" if a.line else "tip"
        return await heartbeat(out, b.call(fn), f"sledgehammer {os.path.basename(b.canon)} {where}")


def fill_shape(body: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("status", "n", "done", "filled", "queue_position", "parked", "parked_reason",
            "elapsed_s", "eta_s", "error")
    o = {k: body.get(k) for k in keys if body.get(k) is not None}
    o["results"] = [{k: v for k, v in r.items() if k != "index"} for r in body.get("results") or []]
    return o


async def fill_poll(b: Bound, out: Out, wait: Optional[float], write: bool,
                    started_sha: Optional[str]) -> Dict[str, Any]:
    c, name = b.c, os.path.basename(b.canon)
    t0 = time.monotonic()
    last = None
    while True:
        try:
            body = await c.fill_status(b.rec["session_id"], lease_id=b.rec["lease_id"])
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {"error": "no_fill_job"}
            raise
        status = body.get("status")
        key = (status, body.get("done"), body.get("filled"), body.get("queue_position"),
               body.get("parked"))
        eta = body.get("eta_s") or {}
        msg = (f"fill {name}: {status}, {body.get('done')}/{body.get('n')} tried, "
               f"{body.get('filled')} proved" + (", parked" if body.get("parked") else "")
               + (f", eta {eta.get('typical')}-{eta.get('worst')}s" if eta else ""))
        out.progress(msg, force=key != last)
        last = key
        if status in FILL_TERMINAL:
            break
        if wait is not None and time.monotonic() - t0 >= wait:
            o = fill_shape(body)
            o["continuing"] = True
            return o
        await asyncio.sleep(3.0)
    o = fill_shape(body)
    o["continuing"] = False
    patched = body.get("patched_text")
    if write and body.get("filled") and patched:
        if started_sha is not None and sha(b.text()) != started_sha:
            o["written"] = False
            o["write_error"] = "the file changed since the job started; not overwritten"
        else:
            with open(b.canon, "w", encoding="utf-8") as f:
                f.write(patched)
            o["written"] = True
    return o


async def cmd_fill(c, st, out, a) -> Any:
    b = Bound(c, st, a.file)
    with st.file_lock(b.canon):
        await b.sync()
        text = b.text()
        try:
            await b.call(lambda sid, l: c.fill_sorries(
                sid, text, mode="document", timeout_s=a.timeout_s, max_total_s=a.max_total_s,
                load_timeout=LOAD_TIMEOUT, lease_id=l, master_dir=master_dir_of(b.canon)))
        except httpx.HTTPStatusError as e:
            return http_error(e)
    # The job runs server-side; polling does not hold the file lock.
    return await fill_poll(b, out, a.wait, a.write, sha(text))


async def cmd_fill_status(c, st, out, a) -> Any:
    canon = canonical(a.file)
    if st.get(canon) is None:
        return {"error": "no_fill_job"}
    b = Bound(c, st, a.file)
    return await fill_poll(b, out, a.wait, a.write, None)


async def cmd_fill_cancel(c, st, out, a) -> Any:
    rec = st.get(canonical(a.file))
    if rec is None:
        return {"error": "no_fill_job"}
    try:
        return await c.fill_cancel(rec["session_id"], lease_id=rec["lease_id"])
    except httpx.HTTPStatusError as e:
        return http_error(e)


async def cmd_build_heap(c, st, out, a) -> Any:
    task = asyncio.ensure_future(c.heap_build(a.task_group, a.project, session_name=a.session))
    t0 = time.monotonic()
    last = None
    while True:
        done, _ = await asyncio.wait({task}, timeout=min(15.0, out.every) if out.every else None)
        if done:
            break
        try:
            m = await c.get_heap(a.task_group, a.project)
            tail = (m.get("build_log_tail") or "").strip().splitlines()
            key = (m.get("status"), tail[-1] if tail else "")
        except httpx.HTTPError:
            key = ("?", "")
        out.progress(f"heap {a.task_group}/{a.project}: {key[0]}, {int(time.monotonic() - t0)}s"
                     + (f", log: {key[1][:160]}" if key[1] else ""), force=key != last)
        last = key
    try:
        r = task.result()
    except httpx.HTTPStatusError as e:
        return http_error(e)
    if r.get("status") == "ready":
        r.pop("build_log_tail", None)
    return r


async def cmd_heaps(c, st, out, a) -> Any:
    if a.project:
        return await c.get_heap(a.task_group or DEFAULT_TASK_GROUP, a.project)
    r = await c.list_heaps(a.task_group)
    with contextlib.suppress(httpx.HTTPError):
        r["available_heaps"] = (await c.list_available_heaps()).get("heaps")
    return r


async def cmd_list(c, st, out, a) -> Any:
    now = time.time()
    return [{"file": k, "idle_s": int(now - v.get("used", 0)), "task_group": v.get("task_group"),
             "heap_session": v.get("heap_session"), "timed_out": v.get("timed_out")}
            for k, v in st.all().items()]


async def cmd_close(c, st, out, a) -> Any:
    canon = canonical(a.file)
    rec = st.get(canon)
    if rec is None:
        return {"closed": False, "note": "not open"}
    with st.file_lock(canon):
        b = Bound.__new__(Bound)
        b.c, b.st, b.canon, b.rec = c, st, canon, rec
        await b.release(destroy=a.destroy)
    return {"closed": True, "destroyed": a.destroy}


async def cmd_release_idle(c, st, out, a) -> Any:
    return {"released": await release_idle(c, st, a.older)}


async def cmd_memory(c, st, out, a) -> Any:
    return await c.memory_status()


# ------------------------------------------------------------------- main

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ipool", description=__doc__.split("\n\n")[0])
    p.add_argument("--progress", type=float, default=0, metavar="SECS",
                   help="print progress lines (each change of state, heartbeat every SECS s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        s = sub.add_parser(name, help=help_, description=help_)
        s.set_defaults(fn=fn)
        return s

    s = add("sync", cmd_sync, "check a file (load it if changed); print its diagnostics")
    s.add_argument("file")
    s.add_argument("--wait", type=float, default=0,
                   help="keep loading while commands still run, up to this many seconds")
    s.add_argument("--severity", choices=["error", "warning"])
    s.add_argument("--max-text", type=int, help="truncate each message to this many characters")
    s.add_argument("--task-group")
    s.add_argument("--heap-session", help="start the file's session on this pool-built heap")

    s = add("check", cmd_check, "check several files; one summary row per file")
    s.add_argument("files", nargs="+")
    s.add_argument("--wait", type=float, default=1800, help="per file, seconds (default 1800)")
    s.add_argument("--jobs", type=int, default=1, help="files checked at the same time")
    s.add_argument("--task-group")
    s.add_argument("--heap-session")

    s = add("goal", cmd_goal, "goals before/after the command at LINE")
    s.add_argument("file")
    s.add_argument("line", type=int)

    s = add("query", cmd_query, "one read-only query (thm, term, find_theorems, print_*) at the end")
    s.add_argument("file")
    s.add_argument("command")

    s = add("sh", cmd_sh, "sledgehammer the goal at LINE (or at the tip)")
    s.add_argument("file")
    s.add_argument("line", type=int, nargs="?")
    s.add_argument("--subgoal", type=int, default=1)
    s.add_argument("--timeout", type=int, default=30)

    s = add("fill", cmd_fill, "server-side sledgehammer on every sorry of the file")
    s.add_argument("file")
    s.add_argument("--timeout-s", type=int, default=30, help="per sorry (default 30)")
    s.add_argument("--max-total-s", type=float, default=600, help="whole job (default 600)")
    s.add_argument("--wait", type=float, help="stop waiting after this many seconds (job goes on)")
    s.add_argument("--write", action="store_true",
                   help="write the patched text to the file if it did not change meanwhile")

    s = add("fill-status", cmd_fill_status, "state of the file's fill job (wait for it with --wait)")
    s.add_argument("file")
    s.add_argument("--wait", type=float, default=0)
    s.add_argument("--write", action="store_true")

    s = add("fill-cancel", cmd_fill_cancel, "cancel the file's fill job")
    s.add_argument("file")

    s = add("build-heap", cmd_build_heap, "build (or rebuild) the heap of a project directory")
    s.add_argument("task_group")
    s.add_argument("project")
    s.add_argument("--session", help="session name (default: from the project)")

    s = add("heaps", cmd_heaps, "heap pool listing, or one heap's manifest")
    s.add_argument("task_group", nargs="?")
    s.add_argument("project", nargs="?")

    add("list", cmd_list, "files this client holds a session for")
    s = add("close", cmd_close, "release the file's session")
    s.add_argument("file")
    s.add_argument("--destroy", action="store_true", help="close it instead of a warm release")
    s = add("release-idle", cmd_release_idle, "release sessions of files unused for a while")
    s.add_argument("--older", type=float, default=0, help="seconds (default 0: all)")
    add("memory", cmd_memory, "the server's memory tier and holders")
    return p


async def amain(argv: Optional[List[str]] = None) -> int:
    a = parser().parse_args(argv)
    out = Out(a.progress)
    st = State()
    async with PoolAsyncClient(base_url=URL, timeout=HTTP_TIMEOUT) as c:
        try:
            if IDLE_RELEASE_S > 0 and a.cmd not in ("release-idle", "list"):
                keep = canonical(a.file) if getattr(a, "file", None) else None
                await release_idle(c, st, IDLE_RELEASE_S, keep=keep)
            res = await a.fn(c, st, out, a)
        except FileNotFoundError as e:
            res = {"error": "file_not_found", "detail": str(e)}
        except httpx.HTTPStatusError as e:
            res = http_error(e)
        except httpx.HTTPError as e:
            res = {"error": "connection", "detail": f"{type(e).__name__}: {e} ({URL})"}
    out.result(res)
    return 1 if isinstance(res, dict) and res.get("error") else 0


def main() -> None:
    try:
        code = asyncio.run(amain())
    except BrokenPipeError:  # the reader went away (e.g. `| head`)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 1
    sys.exit(code)

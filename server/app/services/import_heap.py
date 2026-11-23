"""Build the heap a session needs in the background, then start the session on it.

A session used to load every imported theory that no up-to-date image holds from source while it
started, inside the request: minutes for a project-sized import, and after 600 s the work was
dropped and reported as a gateway failure (agent runs 2026-11: 597 such timeouts in two weeks).
Here the server finds the image the session needs, builds it with `isabelle build -b` in the
background, answers "building" (HTTP 202, with progress) until it is ready, and then starts the
session on it, where the imported theories come from the heap and are not checked again.

The image (`Plan`), from the session's dependency theories (the MCP gives a sibling theory as its
absolute path, other imports by qualified name), its field and its session directories:
  - siblings (theories of the importing file's directory): a generated session
    `IPS_<hash> in "<that directory>" = <parent> + theories <siblings>`, its ROOT in
    STATE_DIR/_imports/<name>; parent = the project session the file also imports, if any (it is
    built as an ancestor), else the field. The Scala backend opens documents on such an image
    with the session as qualifier (Edit_Utils.document_qualifier), so `imports B` is that heap's B;
  - imports of exactly one session of the project (session_dirs): that session;
  - otherwise the field itself, when its image is missing or out of date (a shared library whose
    sources changed, a distribution session never built here).
`isabelle build -b` rebuilds out-of-date ancestors on the way (also shared ones: they must be
rebuilt for anything on top of them to be usable). The same plan always has the same name, so a
build is shared by every request that needs it; at most Heap.MAX_CONCURRENT_BUILDS run at once.
Whether an image is up to date is image_select.current (cached on the state of its inputs).
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import os
import re
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from server.app.core.config import Heap, Server
from server.app.core.input_guards import find_code_execution
from server.app.core.logging import get_logger
from server.app.services import build_process, image_select

logger = get_logger(__name__)

PREFIX = "IPS_"  # = Edit_Utils.GENERATED_SESSION_PREFIX (Scala)
FAILED_RETRY_S = 60.0  # a failed build is tried again after this (or at once for a changed source)
_SESSION_RE = re.compile(r'\bsession\s+"?([A-Za-z0-9_+\-.]+)"?')
_BUILDING_RE = re.compile(r"^Building (\S+)")
_FINISHED_RE = re.compile(r"^(?:Finished|FAILED|Unfinished) (\S+)")


@dataclass
class Plan:
    name: str                       # the image to build and start the session on
    dirs: List[str]                 # -d directories of the build and of the session
    theories: List[str]             # what the session's wrapper imports once the image exists
    root_dir: Optional[str] = None  # a generated session's ROOT directory
    root_text: Optional[str] = None
    watch: List[str] = dc_field(default_factory=list)  # sources whose change retries a failure
    # (directory, recursive) whose theories the build runs: scanned for code-executing commands
    # like heap_pool does (Server.ALLOW_ML_COMMANDS); empty for registered/distribution sessions
    scan: List[Tuple[str, bool]] = dc_field(default_factory=list)


class ImportHeapBuilding(Exception):
    """The image is being built: HTTP 202 with `status` (routes: main.py)."""

    def __init__(self, status: Dict[str, Any]):
        super().__init__(f"building {status.get('session')}")
        self.status = status


class ImportHeapFailed(Exception):
    """The build failed: HTTP 422 with the end of its log."""

    def __init__(self, status: Dict[str, Any]):
        super().__init__(f"building {status.get('session')} failed")
        self.status = status


def _sessions_of(dirs: List[str]) -> Dict[str, str]:
    """Session name -> directory, for the ROOT files of `dirs`."""
    out: Dict[str, str] = {}
    for d in dirs:
        try:
            text = (Path(d) / "ROOT").read_text(encoding="utf-8")
        except OSError:
            continue
        for name in _SESSION_RE.findall(text):
            out.setdefault(name, d)
    return out


def plan(theories: Optional[List[str]], field: str, session_dirs: Optional[List[str]]) -> Optional[Plan]:
    """The image a session with these inputs needs built first, or None (see the module doc)."""
    # One name must always come with one ROOT text: the same theories arrive in different orders
    # (opening a file, starting a scratch session), and a ROOT rewritten under the same name makes
    # `isabelle build` judge the heap out of date and rebuild it. So everything the name and the
    # ROOT are made of is taken in sorted order, without duplicates.
    theories = list(dict.fromkeys(theories or []))
    dirs = sorted(set(session_dirs or []))
    siblings = sorted(t for t in theories if t.startswith("/"))
    own = _sessions_of(dirs)
    own_used = sorted({
        t.split(".", 1)[0] for t in theories if not t.startswith("/") and t.split(".", 1)[0] in own})
    if siblings:
        masters = {os.path.dirname(s) for s in siblings}
        if len(masters) != 1:
            return None
        master = masters.pop()
        parent, others = (own_used[0], own_used[1:]) if own_used else (field, [])
        key = json.dumps([field, sorted(theories), sorted(dirs), master])
        name = PREFIX + hashlib.sha256(key.encode()).hexdigest()[:12]
        root_dir = str(Path(Heap.STATE_DIR) / "_imports" / name)
        names = [os.path.basename(s) for s in siblings]
        text = (f'session {name} in "{master}" = "{parent}" +\n'
                + (f"  sessions {' '.join(others)}\n" if others else "")
                + "  theories\n" + "".join(f"    {n}\n" for n in names))
        wrapper = [f"{name}.{n}" for n in names] + [t for t in theories if not t.startswith("/")]
        watch = sorted(str(p) for p in Path(master).glob("*.thy"))
        scan = [(master, False)] + [(own[s], True) for s in own_used]
        return Plan(name, [root_dir] + dirs, wrapper, root_dir, text, watch, scan)
    if len(own_used) == 1:
        return Plan(own_used[0], dirs, theories, scan=[(own[own_used[0]], True)])
    if theories and not image_select.current(field, image_select.images(), tuple(dirs)):
        return Plan(field, dirs, theories)
    return None


def _progress(log_path: Path) -> Tuple[List[str], List[str]]:
    """(sessions started, sessions finished) from `isabelle build -v` output."""
    started: List[str] = []
    finished: List[str] = []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return started, finished
    for ln in lines:
        m = _BUILDING_RE.match(ln)
        if m and m.group(1) not in started:
            started.append(m.group(1))
        m = _FINISHED_RE.match(ln)
        if m and m.group(1) not in finished:
            finished.append(m.group(1))
    return started, finished


def code_execution(p: Plan) -> Optional[str]:
    """The first theory of the build that contains a code-executing command (ML, setup, file IO),
    as a message, unless the server allows them: `isabelle build` would run it, so such a build is
    refused, as heap_pool refuses one (documents are checked the same way, input_guards)."""
    if Server.ALLOW_ML_COMMANDS:
        return None
    for d, recursive in p.scan:
        for thy in sorted(Path(d).rglob("*.thy") if recursive else Path(d).glob("*.thy")):
            try:
                kw = find_code_execution(thy.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if kw is not None:
                return (f"{thy}: contains the code-executing command '{kw}'; the server does not "
                        "build heaps of such theories (ISABELLE_ALLOW_ML_COMMANDS=false)")
    return None


def _watch_state(paths: List[str]) -> Tuple:
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((p, None, None))
    return tuple(out)


@dataclass
class _Job:
    plan: Plan
    log_path: Path
    started_at: float
    task: Optional[asyncio.Task] = None
    returncode: Optional[int] = None
    finished_at: Optional[float] = None
    watch_state: Tuple = ()


class Builder:
    """The background builds of this server process, one per image name."""

    def __init__(self, isabelle: Optional[str] = None):
        self.isabelle = isabelle
        self._jobs: Dict[str, _Job] = {}
        self._sem: Optional[asyncio.Semaphore] = None

    def _status(self, job: _Job, state: str) -> Dict[str, Any]:
        started, finished = _progress(job.log_path)
        running = [s for s in started if s not in finished]
        end = job.finished_at or time.time()
        return {"status": state, "session": job.plan.name, "sessions_built": finished,
                "sessions_running": running, "elapsed_s": round(end - job.started_at),
                "log_tail": build_process.tail(job.log_path, 1500) if state == "failed" else None}

    async def ensure(self, p: Plan) -> Dict[str, Any]:
        """{"status": "ready"} when the image is up to date; otherwise the state of its build
        (started now if none runs and the last one did not fail on these sources)."""
        job = self._jobs.get(p.name)
        if job is not None and job.finished_at is None:
            return self._status(job, "building")
        if job is not None and job.returncode not in (0, None):
            unchanged = _watch_state(p.watch) == job.watch_state if p.watch else (
                time.time() - (job.finished_at or 0) < FAILED_RETRY_S)
            if unchanged:
                return self._status(job, "failed")
        if p.root_dir:
            Path(p.root_dir).mkdir(parents=True, exist_ok=True)
            root = Path(p.root_dir) / "ROOT"
            if not root.is_file() or root.read_text() != p.root_text:
                root.write_text(p.root_text or "")
            (Path(p.root_dir) / "last_used").touch()
        refused = await asyncio.to_thread(code_execution, p)
        if refused:
            return {"status": "failed", "session": p.name, "log_tail": refused}
        if await asyncio.to_thread(self._current, p):
            return {"status": "ready", "session": p.name}
        # another request may have started the build while this one checked (the await above)
        job = self._jobs.get(p.name)
        if job is not None and job.finished_at is None:
            return self._status(job, "building")
        self._start(p)
        return self._status(self._jobs[p.name], "building")

    def _current(self, p: Plan) -> bool:
        imgs = image_select.images()
        return p.name in imgs and image_select.current(p.name, imgs, tuple(p.dirs))

    def _start(self, p: Plan) -> None:
        log_path = Path(Heap.STATE_DIR) / "_imports" / "_logs" / f"{p.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.unlink(missing_ok=True)
        job = _Job(p, log_path, time.time(), watch_state=_watch_state(p.watch))
        self._jobs[p.name] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job))

    async def _run(self, job: _Job) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(Heap.MAX_CONCURRENT_BUILDS)
        isabelle = self.isabelle or image_select._isabelle()
        cmd = [isabelle, "build", "-b", "-v"]
        for d in job.plan.dirs:
            cmd += ["-d", d]
        cmd.append(job.plan.name)
        try:
            async with self._sem:
                job.started_at = time.time()  # queued time is not build time
                logger.info("import heap build started: %s", " ".join(cmd))
                job.returncode, _ = await build_process.run(cmd, job.log_path)
        except Exception as e:  # noqa: BLE001
            job.returncode = -1
            with open(job.log_path, "a", encoding="utf-8") as f:
                f.write(f"\nfailed to run isabelle build: {e}\n")
        finally:
            job.finished_at = time.time()
            logger.info("import heap build %s ended rc=%s after %.0f s", job.plan.name,
                        job.returncode, job.finished_at - job.started_at)

    async def cancel_all(self) -> None:
        """End every running build (server shutdown): build_process ends the process groups."""
        for job in list(self._jobs.values()):
            if job.task is not None and not job.task.done():
                job.task.cancel()
        await asyncio.gather(*(j.task for j in self._jobs.values() if j.task), return_exceptions=True)


BUILDER = Builder()


async def prepare(theories: Optional[List[str]], field: str, session_dirs: Optional[List[str]],
                  dependency_extra: Optional[str]):
    """The session inputs to start with: unchanged when no image has to be built first, else
    those of the built image. Raises ImportHeapBuilding (202) or ImportHeapFailed (422)."""
    p = await asyncio.to_thread(plan, theories, field, session_dirs)
    if p is None:
        return theories, field, session_dirs, dependency_extra
    status = await BUILDER.ensure(p)
    if status["status"] == "building":
        raise ImportHeapBuilding(status)
    if status["status"] == "failed":
        raise ImportHeapFailed(status)
    extra = f"img:{p.name}"
    return p.theories, p.name, p.dirs, f"{dependency_extra}|{extra}" if dependency_extra else extra


_last_gc = 0.0


def maybe_gc(period_s: float = 3600.0) -> None:
    """gc_generated at most once per `period_s` (called on session requests)."""
    global _last_gc
    if time.time() - _last_gc < period_s:
        return
    _last_gc = time.time()
    try:
        removed = gc_generated()
        if removed:
            logger.info("removed unused generated heaps: %s", removed)
    except Exception:  # noqa: BLE001  (housekeeping must not fail a request)
        logger.exception("generated heap GC failed")


def gc_generated(max_age_s: float = 7 * 86400) -> List[str]:
    """Remove the generated images (and ROOT dirs) not used for `max_age_s`."""
    removed: List[str] = []
    base = Path(Heap.STATE_DIR) / "_imports"
    imgs = image_select.images()
    for d in base.glob(PREFIX + "*"):
        marker = d / "last_used"
        if marker.is_file() and time.time() - marker.stat().st_mtime < max_age_s:
            continue
        if d.name in BUILDER._jobs and BUILDER._jobs[d.name].finished_at is None:
            continue
        img = imgs.get(d.name)
        if img is not None:
            for f in [img, *img.parent.glob(f"log/{d.name}.*")]:
                f.unlink(missing_ok=True)
        for f in d.iterdir():
            f.unlink(missing_ok=True)
        d.rmdir()
        removed.append(d.name)
    return removed

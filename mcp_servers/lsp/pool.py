"""File→session bindings (the copied-buffer sync model) + warm scratch pool.

Copied-buffer model: the MCP NEVER writes files. Each open file maps to a leased
session; EVERY file-scoped tool call re-reads the file from disk and, if the text
changed since the last sync, pushes it via ``load_document(text, report=true)``
(the lean-lsp-mcp reload_from_disk analog). A server-side 404 (session evicted)
rebinds transparently.

Bindings ACQUIRE (not create) their session, passing the file's own header
imports as the acquire `theories`: a session released by another binding with
the same dependency key (task_group + heap / same imports, default field) is
reused warm — every sync reloads via load_document, which resets the backend,
so dirty reuse is safe. Theories-keyed acquire is also what lets non-`Main`
parents resolve at all: sessions only see theories from their own
heap-ancestor chain plus what they were acquired with.

Scratch sessions (for multi_attempt / run_code) are leased sessions keyed by
context (task_group, heap_session, imports, field), kept warm and reused — each
use is a load_document reset, so candidates never pollute each other.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from client.async_client import PoolAsyncClient

from ..common import (  # noqa: F401  (is_not_found re-exported)
    KIND_BINDING,
    KIND_SCRATCH,
    GymClientMixin,
    SessionRegistry,
    is_not_found,
)
from .config import Config


def canonical_path(file_path: str) -> str:
    return os.path.realpath(os.path.expanduser(file_path))


async def header_fields(
    client: PoolAsyncClient, text: str,
) -> Tuple[List[str], Optional[str]]:
    """(imports, suggested_field) of a full .thy source's header.

    Asks the server's canonical parser (`POST /api/v1/parse_theory_header`,
    comments stripped first so a leading `(* TASK: ... *)` comment can never
    pollute the imports — isabelle-pool-server-header-imports-issue.md). Going through
    the endpoint keeps the MCP free of server code and guarantees it parses
    headers, and picks the session image for them, exactly as the server it
    talks to does (`suggested_field`: the largest distribution image named)."""
    resp = await client.parse_theory_header(text)
    return list(resp.get("imports") or []), resp.get("suggested_field") or None


async def header_imports(client: PoolAsyncClient, text: str) -> List[str]:
    """Import names from a full .thy source's header (quotes stripped)."""
    imports, _ = await header_fields(client, text)
    return imports


def header_text(imports: List[str]) -> str:
    """A one-line header naming `imports`, for the server's parser when a tool
    gets an import list instead of a source (isabelle_run_code)."""
    return "theory Scratch imports " + " ".join(f'"{i}"' for i in imports) + " begin"


def field_for(suggested: Optional[str]) -> str:
    """The field a binding or scratch session starts on: the server's
    suggestion when Config.FIELD_FROM_IMPORTS is on and the header named a
    session image, else Config.DEFAULT_FIELD (ISABELLE_MCP_LSP_FIELD, "HOL":
    what the client sent on its own before the knob existed). An image the
    server lacks is built on first use, inside the acquire."""
    if Config.FIELD_FROM_IMPORTS and suggested:
        return suggested
    return Config.DEFAULT_FIELD


def attempt_prefix(text: str, line: int) -> str:
    """The file's text BEFORE 1-based `line` (multi_attempt prefix truncation)."""
    lines = text.splitlines()
    return "\n".join(lines[: line - 1]) + "\n"


@dataclass
class FileBinding:
    file_path: str  # canonical
    session_id: str
    lease_id: str
    task_group: str
    heap_session: Optional[str] = None
    cached_text: Optional[str] = None
    last_report: Optional[Dict[str, Any]] = None
    # (node_line, kind) of each command the last report shows `running` -> the
    # monotonic time of the load in which this MCP first saw it running (a
    # lower bound of when it started: PIDE keeps an unchanged command running
    # across incremental syncs).
    running_since: Dict[Tuple[Any, Any], float] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LspPool(GymClientMixin):
    def __init__(self) -> None:
        self._init_client(Config.GYM_URL, Config.HTTP_TIMEOUT)
        self._bindings: Dict[str, FileBinding] = {}
        self._bindings_lock = asyncio.Lock()
        self._creating: Dict[str, asyncio.Future] = {}  # in-flight binding creations (single-flight)
        # scratch pool: context key -> (queue of idle sessions, created count)
        self._scratch: Dict[Tuple, asyncio.Queue] = {}
        self._scratch_counts: Dict[Tuple, int] = {}
        self._scratch_lock = asyncio.Lock()
        self._retire_tasks: set = set()  # deferred closes of busy dropped sessions
        # What this process holds on the server (bindings + scratch; session and
        # lease kept here, never shown to the agent) — plan §2.C.
        self.registry = SessionRegistry()

    # ---------------------------------------------------------- file bindings

    async def _create_binding(
        self, canon: str, task_group: Optional[str], heap_session: Optional[str],
        label: Optional[str],
    ) -> FileBinding:
        # Check the path BEFORE acquiring anything: a binding used to be created
        # (and a server session leased) for any path, and only the later sync()
        # noticed the file was missing — so every typo or not-yet-written file
        # pinned a leased session under a bogus path until close / the lease
        # reaper (docs/ISSUES.md Bug 19, audit MCP-2).
        if not os.path.isfile(canon):
            raise FileNotFoundError(f"file not found: {canon}")
        c = await self.client()
        group = task_group or Config.DEFAULT_TASK_GROUP
        # The session must be acquired WITH the file's own imports: gym REPL
        # sessions only see theories from their own heap-ancestor chain, and
        # only a theories-keyed acquire pulls extra parents (Complex_Main,
        # "HOL-Analysis.Derivative", …) into the session's dependency context.
        # Without this, every LSP tool call on a non-Main file ran against a
        # session that could not resolve the file's imports ("Undefined type
        # name" after a ~130 s doomed parent-resolution attempt).
        # An unreadable (but existing) file falls back to the empty-deps key.
        imports: Optional[List[str]] = None
        suggested: Optional[str] = None
        try:
            with open(canon, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = None
        if text is not None:
            imports, suggested = await header_fields(c, text)
            imports = imports or None
        # Acquire (not create): sessions released by other bindings with the
        # same dependency key (task_group + heap / imports, default field)
        # are reused WARM instead of building a fresh session per file — but
        # CLEAN ONLY (reuse_dirty=False, the rule the chunk-centric pool has
        # always used, DESIGN_CHOICES 1.5/2.2): a released binding's session
        # still holds the previous file's document, and the server treats any
        # session with command history (load_document records one) as dirty.
        # Inheriting it exposed another attempt's proof between acquire and
        # the first sync and cost a full backend reset on that sync anyway —
        # no cheaper than a fresh session (docs/ISSUES.md Bug 21, audit MCP-4).
        # The label is re-applied on every acquire server-side, so a reused
        # session shows THIS file in the admin console, not its previous holder.
        # The field: the server's suggestion for the dotted imports when
        # Config.FIELD_FROM_IMPORTS is on, else the LSP's default (field_for).
        resp = await c.acquire_session(
            theories=imports, field=field_for(suggested), reuse_dirty=False,
            task_group=group, heap_session=heap_session, label=label or canon,
        )
        binding = FileBinding(
            file_path=canon,
            session_id=resp["session_id"],
            lease_id=resp["lease_id"],
            task_group=group,
            heap_session=heap_session,
        )
        self.registry.register(
            binding.session_id,
            binding.lease_id,
            KIND_BINDING,
            canon,
            label=f"file {os.path.basename(canon)}",
            task_group=group,
            heap_session=heap_session,
            imports=tuple(imports or ()),
        )
        async with self._bindings_lock:
            old = self._bindings.pop(canon, None)
            self._bindings[canon] = binding
        if old is not None:
            await self._safe_release(old)
        return binding

    def peek_binding(self, file_path: str) -> Optional[FileBinding]:
        """The binding for a file if this process holds one — NEVER opens
        (fill_status / fill_cancel after an MCP restart or on a mistyped path
        must answer `no_fill_job`, not acquire a session; review aa96c6d S1)."""
        binding = self._bindings.get(canonical_path(file_path))
        if binding is not None:
            self.registry.touch(binding.session_id)
        return binding

    async def get_binding(
        self, file_path: str, task_group: Optional[str] = None,
        heap_session: Optional[str] = None, label: Optional[str] = None,
    ) -> FileBinding:
        """The binding for a file; AUTO-OPENS with defaults on first use.

        SINGLE-FLIGHT per path: the lookup happens under `_bindings_lock`, and
        when no binding exists the first caller creates it while concurrent
        callers await the same in-flight creation. Before this, the unlocked
        `dict.get` let two concurrent first calls on one file (typical: a
        harness firing `isabelle_goal` and `isabelle_diagnostic_messages`
        together right after writing the file) each acquire a session; the
        second registration popped the first binding and released its session
        while the first caller was still using it (docs/ISSUES.md Bug 20,
        audit MCP-3)."""
        canon = canonical_path(file_path)
        async with self._bindings_lock:
            binding = self._bindings.get(canon)
            if binding is not None:
                self.registry.touch(binding.session_id)
                return binding
            fut = self._creating.get(canon)
            owner = fut is None
            if owner:
                fut = asyncio.get_running_loop().create_future()
                self._creating[canon] = fut
        if not owner:
            return await asyncio.shield(fut)  # the creator's result (or error)
        try:
            binding = await self._create_binding(canon, task_group, heap_session, label)
        except BaseException as e:
            if isinstance(e, Exception):
                fut.set_exception(e)
            else:
                fut.cancel()  # cancellation: waiters see CancelledError and may retry
            raise
        else:
            fut.set_result(binding)
            return binding
        finally:
            async with self._bindings_lock:
                if self._creating.get(canon) is fut:
                    del self._creating[canon]

    async def sync(self, binding: FileBinding) -> bool:
        """Re-read the file from disk; if changed vs the cache, push it via
        load_document(report=true). Returns True if a reload happened.

        A file whose last load timed out is loaded again even if unchanged: the
        retained report is the state at the timeout, and the commands then
        running may have finished since.

        On 404 (session evicted/closed server-side) the binding is recreated
        once and the load retried. Raises FileNotFoundError for a missing file.
        """
        canon = binding.file_path
        if not os.path.isfile(canon):
            raise FileNotFoundError(f"file not found: {canon}")
        with open(canon, encoding="utf-8") as f:
            text = f.read()
        async with binding.lock:
            if text == binding.cached_text and not (binding.last_report or {}).get("timed_out"):
                return False
            started = time.monotonic()
            c = await self.client()
            try:
                result = await c.load_document(
                    binding.session_id, text, report=True,
                    timeout=Config.LOAD_TIMEOUT, lease_id=binding.lease_id,
                )
            except httpx.HTTPStatusError as e:
                if not is_not_found(e):
                    raise
                # session evicted server-side: rebind once and retry
                self.registry.unregister(binding.session_id)
                fresh = await self._create_binding(
                    canon, binding.task_group, binding.heap_session, None)
                binding.session_id = fresh.session_id
                binding.lease_id = fresh.lease_id
                binding.cached_text = None
                result = await c.load_document(
                    binding.session_id, text, report=True,
                    timeout=Config.LOAD_TIMEOUT, lease_id=binding.lease_id,
                )
            binding.cached_text = text
            binding.last_report = result.get("report") or {}
            running = {
                (cmd.get("node_line"), cmd.get("kind"))
                for cmd in binding.last_report.get("commands") or []
                if cmd.get("status") == "running"
            }
            binding.running_since = {
                key: binding.running_since.get(key, started) for key in running
            }
            return True

    def running_commands(self, binding: FileBinding) -> List[Dict[str, Any]]:
        """The commands the last report shows `running`, if that load timed out:
        line, kind and the seconds since this MCP first saw each running."""
        rep = binding.last_report or {}
        if not rep.get("timed_out"):
            return []
        now = time.monotonic()
        out = []
        for cmd in rep.get("commands") or []:
            if cmd.get("status") != "running":
                continue
            since = binding.running_since.get((cmd.get("node_line"), cmd.get("kind")), now)
            out.append({"line": cmd.get("line"), "kind": cmd.get("kind"),
                        "running_for_s": round(now - since)})
        return out

    async def call(self, binding: FileBinding, fn, *args, **kwargs):
        """Run a client call against a binding's session, rebinding once on 404."""
        c = await self.client()
        self.registry.touch(binding.session_id)
        try:
            return await fn(c, binding.session_id, *args, **kwargs)
        except httpx.HTTPStatusError as e:
            if not is_not_found(e):
                raise
        self.registry.unregister(binding.session_id)
        fresh = await self._create_binding(
            binding.file_path, binding.task_group, binding.heap_session, None)
        binding.session_id = fresh.session_id
        binding.lease_id = fresh.lease_id
        binding.cached_text = None  # force reload on next sync
        await self.sync(binding)
        return await fn(c, binding.session_id, *args, **kwargs)

    async def close_binding(self, file_path: str, destroy: bool = False) -> bool:
        canon = canonical_path(file_path)
        async with self._bindings_lock:
            binding = self._bindings.pop(canon, None)
        if binding is None:
            return False
        if destroy:
            await self._safe_destroy(binding)
        else:
            await self._safe_release(binding)
        return True

    async def release_idle_bindings(self, max_idle_s: float) -> List[str]:
        """Release (warm, not destroy) every open file whose session has not
        been used for ``max_idle_s`` seconds: an unleased session is one the
        server may reclaim under pressure (owner decision D3 — the MCP lets
        go, the server never takes). Opt-in: the next sync costs a reload."""
        now = time.time()
        idle = self.registry.idle(min_idle_s=max_idle_s, now=now, kinds=(KIND_BINDING,))
        paths = [e.owner for e in idle if isinstance(e.owner, str)]
        released = []
        for path in paths:
            if await self.close_binding(path, destroy=False):
                released.append(path)
        return released

    async def _safe_release(self, binding: FileBinding) -> None:
        self.registry.unregister(binding.session_id)
        try:
            c = await self.client()
            await c.release_session(binding.session_id, lease_id=binding.lease_id)
        except Exception:
            pass

    async def _safe_destroy(self, binding: FileBinding) -> None:
        """Sanctioned immediate teardown: DELETE the binding's own session
        (authorized by the binding's own lease). The rebind-on-404 path
        recovers transparently if the file is opened again afterwards."""
        self.registry.unregister(binding.session_id)
        try:
            c = await self.client()
            await c.close_session(binding.session_id, lease_id=binding.lease_id)
        except Exception:
            pass

    # ---------------------------------------------------------- scratch pool

    def scratch_key(
        self, task_group: Optional[str], heap_session: Optional[str],
        imports: List[str], field: Optional[str],
    ) -> Tuple:
        return (
            task_group or Config.DEFAULT_TASK_GROUP,
            heap_session or "",
            tuple(sorted(imports)),
            field or Config.DEFAULT_FIELD,
        )

    async def acquire_scratch(self, key: Tuple) -> Tuple[str, str]:
        """Get a warm scratch session for the context (create if under the cap,
        else wait — at most Config.SCRATCH_WAIT_TIMEOUT — for one to be returned).

        Prefer the `scratch_session` bracket: a slot taken here and never
        returned (a cancelled caller, an exception path without `finally`)
        used to leave the NEXT caller waiting forever once the cap was reached
        (docs/ISSUES.md Bug 17, audit MCP-1). The bounded wait turns that into a
        clear error naming the cap and the wait."""
        async with self._scratch_lock:
            queue = self._scratch.setdefault(key, asyncio.Queue())
        while True:
            try:
                sid, lease, released_at = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if time.time() - released_at > Config.SCRATCH_IDLE_S:
                # Warm but idle too long: let go (it is leased, so only the MCP
                # can free it — review f1fb31a / plan G4).
                await self.drop_scratch(key, sid, lease)
                continue
            self.registry.touch(sid)
            return sid, lease
        async with self._scratch_lock:
            count = self._scratch_counts.get(key, 0)
            if count < Config.SCRATCH_POOL_SIZE:
                self._scratch_counts[key] = count + 1
                create = True
            else:
                create = False
        if not create:
            try:
                sid, lease, _ = await asyncio.wait_for(
                    queue.get(), Config.SCRATCH_WAIT_TIMEOUT
                )
                self.registry.touch(sid)
                return sid, lease
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"scratch pool exhausted: all {Config.SCRATCH_POOL_SIZE} scratch "
                    f"session(s) for this context stayed busy for "
                    f"{Config.SCRATCH_WAIT_TIMEOUT:.0f}s (ISABELLE_MCP_LSP_SCRATCH_POOL_SIZE / "
                    f"ISABELLE_MCP_LSP_SCRATCH_WAIT_TIMEOUT)"
                ) from None
        task_group, heap_session, imports, field = key
        try:
            c = await self.client()
            resp = await c.create_session(
                theories=list(imports) or None,
                field=field,
                task_group=task_group,
                heap_session=heap_session or None,
                label=f"scratch:{task_group}:{heap_session or 'plain'}",
            )
            self.registry.register(
                resp["session_id"],
                resp["lease_id"],
                KIND_SCRATCH,
                f"scratch:{task_group}:{heap_session or 'plain'}",
                label=f"scratch {task_group}/{heap_session or 'plain'}",
                task_group=task_group,
                heap_session=heap_session or None,
                imports=imports,
            )
            return resp["session_id"], resp["lease_id"]
        except BaseException:
            async with self._scratch_lock:
                self._scratch_counts[key] -= 1
            raise

    @contextlib.asynccontextmanager
    async def scratch_session(self, key: Tuple):
        """Acquire a scratch session and ALWAYS give the slot back, whatever
        happens in the body:
          - normal exit, or an ordinary exception → release (warm, reusable);
          - a 404 from the server (session evicted) → drop (count decremented,
            closed best-effort) so the next acquire creates a fresh one;
          - cancellation / any other BaseException → drop as well: a load may
            still be in flight on that session, so handing it to the next caller
            would answer them with "busy"; a fresh session is the safe choice.
        Before this bracket the tools released only on the success path and in
        an `except Exception`, which a CancelledError (BaseException) skips — one
        client disconnect mid-attempt lost the slot for the rest of the run
        (Bug 17 / audit MCP-1)."""
        session_id, lease_id = await self.acquire_scratch(key)
        outcome = "release"
        try:
            yield session_id, lease_id
        except Exception as e:
            outcome = "drop" if is_not_found(e) else "release"
            raise
        except BaseException:
            outcome = "drop"
            raise
        finally:
            if outcome == "drop":
                await self.drop_scratch(key, session_id, lease_id)
            else:
                await self.release_scratch(key, session_id, lease_id)

    async def release_scratch(self, key: Tuple, session_id: str, lease_id: str) -> None:
        queue = self._scratch.get(key)
        self.registry.touch(session_id)
        if queue is not None:
            queue.put_nowait((session_id, lease_id, time.time()))

    async def retire_idle_scratch(self, max_idle_s: float = 0.0) -> int:
        """Close every warm scratch session idle for at least ``max_idle_s``
        seconds (0 = all idle ones). Returns how many were closed. Used by
        isabelle_release_idle and by the memory reminder hook."""
        now = time.time()
        dropped = 0
        for key, queue in list(self._scratch.items()):
            keep = []
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                sid, lease, released_at = item
                if now - released_at >= max_idle_s:
                    await self.drop_scratch(key, sid, lease)
                    dropped += 1
                else:
                    keep.append(item)
            for item in keep:
                queue.put_nowait(item)
        return dropped

    async def drop_scratch(self, key: Tuple, session_id: str, lease_id: str) -> None:
        """Discard a scratch session (404, or an aborted use) and close it
        best-effort. A session whose load is still running server-side cannot be
        closed OR released (busy → 409); then a background task retries the
        close for a while (`_retire_later`), so the session does not sit leased
        and idle — blocking a pool slot — until the abandoned-lease reaper."""
        async with self._scratch_lock:
            self._scratch_counts[key] = max(0, self._scratch_counts.get(key, 1) - 1)
        self.registry.unregister(session_id)
        try:
            c = await self.client()
            await c.close_session(session_id, lease_id=lease_id)
        except Exception:
            task = asyncio.create_task(self._retire_later(session_id, lease_id))
            self._retire_tasks.add(task)
            task.add_done_callback(self._retire_tasks.discard)

    async def _retire_later(self, session_id: str, lease_id: str,
                            every_s: float = 5.0) -> None:
        """Retry closing a busy scratch session until it succeeds, it vanishes,
        or the attempt budget (+ grace) is exhausted."""
        deadline = asyncio.get_running_loop().time() + Config.ATTEMPT_TIMEOUT + 60.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(every_s)
            try:
                c = await self.client()
                await c.close_session(session_id, lease_id=lease_id)
                return
            except Exception as e:  # noqa: BLE001
                if is_not_found(e):
                    return  # already gone (evicted / closed elsewhere)

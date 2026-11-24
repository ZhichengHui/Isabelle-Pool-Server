"""Soft memory limit per session (efficiency plan D2).

Every ``Memory.GUARD_INTERVAL_S`` (5 s) the guard reads the ML heap of every open session
WITHOUT the session's worker queue and without a worker thread of the prover: the latest
runtime statistics Isabelle's own monitor posted (``ReplBackend.ml_heap``,
backend_memory.scala; refreshed every 0.5 s). The figure compared is the heap in use,
``size_heap - size_heap_free_last_GC`` (``size_heap`` alone is the heap the runtime has
reserved, which does not shrink when a big computation ends).

When it exceeds ``GUARD_HEAP_FRACTION`` (0.75) x the ML heap cap (``--maxheap``):
- a running ``attempt`` (isabelle_multi_attempt) is cancelled as a whole:
  ``cancel_attempts("memory")``; its unfinished candidates are reported
  ``cancelled == "memory"`` (the MCP shows ``cancelled_memory: true``);
- otherwise the file's running commands are cancelled (``Session.cancel_exec``,
  ``cancel_running_commands``); the next report of the session marks those commands
  and carries ``error_kind = "memory"`` (``annotate_report`` / ``memory_kind_since``).
After a cancel the session is skipped until its runtime reports another garbage
collection: until then the figures still show the memory of the cancelled work.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from server.app.core.config import Memory
from server.app.core.logging import get_logger
from server.app.services.internal_models import SessionStatus
from server.app.services.success_checker import ERROR_KIND_MEMORY

logger = get_logger(__name__)

MEMORY_CANCEL_TEXT = (
    "cancelled by the server's memory guard: the session's ML heap in use exceeded "
    "{pct}% of its limit ({limit_mb} MB) while this command ran; the command was "
    "stopped to keep the session alive (error_kind=memory). Change the command (a "
    "smaller step, a narrower simp set, a bounded search) before checking it again."
)


def parse_heap(text: str) -> Dict[str, int]:
    """``key=value`` lines of ``ml_heap`` as integers (unparseable values dropped)."""
    out: Dict[str, int] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        try:
            out[key.strip()] = int(float(value.strip()))
        except ValueError:
            continue
    return out


def heap_in_use(stats: Dict[str, int]) -> Optional[int]:
    """Bytes of heap in use after the last GC plus what was allocated since."""
    size = stats.get("size_heap")
    if size is None:
        return None
    return max(0, size - stats.get("size_heap_free_last_GC", 0))


def gc_count(stats: Dict[str, int]) -> int:
    return stats.get("full_GCs", 0) + stats.get("partial_GCs", 0)


_MAXHEAP_RE = re.compile(r"--maxheap[ =]+(\d+)\s*([KkMmGg]?)")


def effective_maxheap_mb(isabelle: Optional[str], default_mb: int) -> int:
    """The ``--maxheap`` the provers actually get (``isabelle getenv ML_OPTIONS``; the
    user settings may differ from ISABELLE_ML_MAXHEAP_MB, ensure_settings.sh keeps an
    existing value). Poly/ML reads a bare number as MB. ``default_mb`` when unknown."""
    if not isabelle:
        return default_mb
    try:
        out = subprocess.run([isabelle, "getenv", "-b", "ML_OPTIONS"], capture_output=True,
                             text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return default_mb
    m = _MAXHEAP_RE.search(out or "")
    if not m:
        return default_mb
    n, unit = int(m.group(1)), m.group(2).upper()
    return {"K": n // 1024, "G": n * 1024}.get(unit, n)


class MemoryGuard:
    """The periodic check (see the module docstring); one per server."""

    def __init__(self, session_manager: Any, maxheap_mb: int,
                 fraction: float = Memory.GUARD_HEAP_FRACTION,
                 interval_s: float = Memory.GUARD_INTERVAL_S) -> None:
        self.sm = session_manager
        self.maxheap_mb = maxheap_mb
        self.fraction = fraction
        self.interval_s = interval_s
        self.limit_bytes = int(maxheap_mb * fraction * 1024 * 1024)
        self._task: Optional[asyncio.Task] = None
        self.events: List[Dict[str, Any]] = []  # last actions, for logs/tests

    def start(self) -> None:
        if self._task is None and self.interval_s > 0:
            logger.info("memory guard on: every %.0f s, limit %d MB (%.0f%% of --maxheap %d MB)",
                        self.interval_s, self.limit_bytes // 2**20, self.fraction * 100,
                        self.maxheap_mb)
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("memory guard check failed")  # the loop must never die

    def _sessions(self) -> List[Any]:
        lock = getattr(self.sm, "_lock", None)
        lru = getattr(self.sm, "_lru", {})
        if lock is None:
            return list(lru.values())
        with lock:
            return list(lru.values())

    def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """One check of every open session; the actions taken."""
        now = time.time() if now is None else now
        actions = []
        for session in self._sessions():
            if getattr(session, "status", None) == SessionStatus.CLOSED:
                continue
            try:
                action = self.check(session, now)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("memory guard: session %s", getattr(session, "session_id", "?"))
                continue
            if action:
                actions.append(action)
        self.events = (self.events + actions)[-50:]
        return actions

    def check(self, session: Any, now: float) -> Optional[Dict[str, Any]]:
        backend = getattr(session, "backend", None)
        if backend is None:
            return None
        stats = parse_heap(str(backend.raw.ml_heap() or ""))
        session.guard_heap = stats
        used = heap_in_use(stats)
        hold = getattr(session, "guard_hold_gc", None)
        if hold is not None:
            if gc_count(stats) <= hold:
                return None  # no GC since the cancel: the figures are the cancelled work's
            session.guard_hold_gc = None
        if used is None or used <= self.limit_bytes:
            return None
        cancelled = int(session.cancel_attempts("memory")) if hasattr(session, "cancel_attempts") else 0
        lines: List[Dict[str, Any]] = []
        if cancelled == 0:
            session.memory_cancel_at = now  # before the cancel: a report built meanwhile sees it
            lines = json.loads(str(backend.raw.cancel_running_commands() or "[]"))
            marked = getattr(session, "memory_cancelled_lines", None) or set()
            session.memory_cancelled_lines = marked | {int(c["line"]) for c in lines}
        if not cancelled and not lines:
            return None
        session.guard_hold_gc = gc_count(stats)
        action = {"session_id": str(session.session_id), "at": now, "heap_in_use_mb":
                  round(used / 2**20), "limit_mb": self.limit_bytes // 2**20,
                  "candidates_cancelled": cancelled, "commands_cancelled": lines}
        logger.warning("memory guard: session %s heap in use %d MB > %d MB; cancelled %d "
                       "candidate(s), commands %s", session.session_id, action["heap_in_use_mb"],
                       action["limit_mb"], cancelled, lines)
        return action

    def cancel_text(self) -> str:
        return MEMORY_CANCEL_TEXT.format(pct=round(self.fraction * 100),
                                         limit_mb=self.maxheap_mb)


GUARD: Optional[MemoryGuard] = None  # set by main.py


def _text() -> str:
    return GUARD.cancel_text() if GUARD is not None else MEMORY_CANCEL_TEXT.format(
        pct=round(Memory.GUARD_HEAP_FRACTION * 100), limit_mb=Memory.ML_MAXHEAP_MB)


def _interrupted(cmd: Dict[str, Any]) -> bool:
    return any("interrupt" in str(m.get("text", "")).lower() for m in cmd.get("messages") or [])


def annotate_report(session: Any, report: Optional[Dict[str, Any]], started_at: float) -> bool:
    """Mark the commands of a document report that the guard cancelled: a failed command
    at a cancelled line, or (cancel during this request) a failed command whose message is
    an interrupt. Adds the guard's message, sets ``report["error_kind"] = "memory"``.
    A cancelled line is forgotten once its command reports ``ok`` (edited, re-run); while it
    still shows ``running`` (a report taken just before the cancel took effect) or is absent,
    the mark stays. PIDE marks a cancelled command failed without a message of its own, so
    the guard's message is what the client sees. Returns whether the report was marked."""
    lines = getattr(session, "memory_cancelled_lines", None) or set()
    during = memory_kind_since(session, started_at) is not None
    if not report or (not lines and not during):
        return False
    marked = False
    still = set(lines)
    for cmd in report.get("commands") or []:
        line = cmd.get("node_line") or cmd.get("line")
        failed = cmd.get("status") == "failed"
        if failed and (line in lines or (during and _interrupted(cmd))):
            msgs = list(cmd.get("messages") or [])
            if not any(m.get("text") == _text() for m in msgs):
                msgs.append({"sev": "error", "text": _text()})
            cmd["messages"] = msgs
            marked = True
        elif cmd.get("status") == "ok":
            still.discard(line)
    session.memory_cancelled_lines = still
    if marked:
        report["error_kind"] = ERROR_KIND_MEMORY
    return marked


def memory_kind_since(session: Any, started_at: float) -> Optional[str]:
    """``"memory"`` when the guard cancelled this session's commands since ``started_at``."""
    at = getattr(session, "memory_cancel_at", None)
    return ERROR_KIND_MEMORY if at is not None and float(at) >= started_at else None

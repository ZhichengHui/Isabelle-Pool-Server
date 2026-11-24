"""Run `isabelle build` as a process group that can be ended as a whole.

`isabelle build` is a launcher script: the work happens in its children (the JVM and one `poly`
per session). Ending only the launcher (what `proc.kill()` or a timeout of `communicate()` does,
or nothing at all, as heap_pool did after its 3600 s budget) leaves those running, holding memory
and the build database lock. Here the build starts in its own session (`start_new_session`), so
its process group id is its pid and `stop` ends every process of it.

Every build also runs in its own memory-limited systemd scope when the server found
`systemd-run --user` usable at start-up (cgroup_scope.build_prefix, efficiency plan D4;
`systemd-run --scope` execs the command, so pid and process group stay the same), and all
heap-pool and import-heap builds share ONE bound of builds running at once
(`build_slots`, Heap.MAX_CONCURRENT_BUILDS, default 4).
"""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
import weakref
from typing import Callable, List, Optional, Tuple

from server.app.core.config import Heap
from server.app.services import cgroup_scope

TERM_GRACE_S = 10.0

# One semaphore per event loop (an asyncio.Semaphore belongs to the loop it first waits
# on; tests run several loops): the server has one loop, so this is one global bound.
_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary())


def build_slots() -> asyncio.Semaphore:
    """The bound shared by heap-pool and import-heap builds (Heap.MAX_CONCURRENT_BUILDS)."""
    loop = asyncio.get_running_loop()
    sem = _SLOTS.get(loop)
    if sem is None:
        sem = _SLOTS[loop] = asyncio.Semaphore(max(1, Heap.MAX_CONCURRENT_BUILDS))
    return sem


def scoped(cmd: List[str]) -> List[str]:
    """`cmd` inside a memory-limited scope when scopes are usable, else unchanged."""
    return cgroup_scope.build_prefix() + list(cmd)


async def start(cmd: List[str], log_path: Path, env: Optional[dict] = None) -> asyncio.subprocess.Process:
    """Start `cmd` in its own process group, stdout and stderr appended to `log_path`."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        return await asyncio.create_subprocess_exec(
            *scoped(cmd), stdout=log, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True, env=env)


async def stop(proc: asyncio.subprocess.Process, grace_s: float = TERM_GRACE_S) -> None:
    """End the whole process group of `proc` (SIGTERM, then SIGKILL after `grace_s`)."""
    if proc.returncode is not None:
        return
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, grace_s)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except asyncio.TimeoutError:
            continue


async def run(cmd: List[str], log_path: Path, timeout_s: Optional[float] = None,
              env: Optional[dict] = None,
              on_start: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
              ) -> Tuple[Optional[int], bool]:
    """Run `cmd` to its end: (returncode, timed_out). On timeout or cancellation the whole
    process group is ended before this returns (or re-raises the cancellation)."""
    proc = await start(cmd, log_path, env)
    if on_start is not None:
        on_start(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
        return proc.returncode, False
    except asyncio.TimeoutError:
        await stop(proc)
        return proc.returncode, True
    except asyncio.CancelledError:
        await asyncio.shield(stop(proc))
        raise


def tail(log_path: Path, n_bytes: int = 4000) -> str:
    """The end of a build log."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

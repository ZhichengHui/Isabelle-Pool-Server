"""Run `isabelle build` as a process group that can be ended as a whole.

`isabelle build` is a launcher script: the work happens in its children (the JVM and one `poly`
per session). Ending only the launcher (what `proc.kill()` or a timeout of `communicate()` does,
or nothing at all, as heap_pool did after its 3600 s budget) leaves those running, holding memory
and the build database lock. Here the build starts in its own session (`start_new_session`), so
its process group id is its pid and `stop` ends every process of it.
"""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Callable, List, Optional, Tuple

TERM_GRACE_S = 10.0


async def start(cmd: List[str], log_path: Path, env: Optional[dict] = None) -> asyncio.subprocess.Process:
    """Start `cmd` in its own process group, stdout and stderr appended to `log_path`."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        return await asyncio.create_subprocess_exec(
            *cmd, stdout=log, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
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

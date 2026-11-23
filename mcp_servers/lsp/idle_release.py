"""Release what no client has used for ``Config.IDLE_RELEASE_S`` seconds.

A stdio MCP lives as long as its one client: when the client ends, the process
ends, lease renewal (lease_renewal.py) stops and the server reaps the sessions.
A resident streamable-http MCP serves many clients that come and go and never
exits, so it would renew the leases of an ended client's files forever. This
background task gives it the same end: every open file whose session was not
used for IDLE_RELEASE_S is released (warm, back to the pool; the next
file-scoped call reopens it) and every warm scratch session idle that long is
closed. A file whose binding lock is held (a sync or tool call in progress) is
skipped this round.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List

from ..common import KIND_BINDING
from .config import Config

logger = logging.getLogger(__name__)


def ensure(pool: Any) -> None:
    """Start the release task once, from inside the running event loop."""
    if Config.IDLE_RELEASE_S <= 0:
        return
    task = getattr(pool, "_idle_release_task", None)
    if task is not None and not task.done():
        return
    try:
        pool._idle_release_task = asyncio.get_running_loop().create_task(_run(pool))
    except RuntimeError:  # no running loop (import time, sync tests)
        pool._idle_release_task = None


async def release_once(pool: Any, max_idle_s: float, now: float | None = None) -> List[str]:
    """One round: release idle open files, close idle warm scratch sessions.
    Returns the released file paths."""
    now = time.time() if now is None else now
    released: List[str] = []
    for entry in pool.registry.idle(min_idle_s=max_idle_s, now=now, kinds=(KIND_BINDING,)):
        path = entry.owner
        if not isinstance(path, str):
            continue
        binding = pool.peek_binding_quiet(path)
        if binding is None or binding.lock.locked():
            continue
        if await pool.close_binding(path, destroy=False):
            released.append(path)
    dropped = await pool.retire_idle_scratch(max_idle_s)
    if released or dropped:
        logger.info("idle release: %d file(s) %s, %d scratch session(s)",
                    len(released), released, dropped)
    return released


async def _run(pool: Any) -> None:
    period = max(5.0, min(60.0, Config.IDLE_RELEASE_S / 4))
    while Config.IDLE_RELEASE_S > 0:
        await asyncio.sleep(period)
        try:
            await release_once(pool, Config.IDLE_RELEASE_S)
        except Exception as e:  # noqa: BLE001  (server down: try again next round)
            logger.debug("idle release round failed: %s", e)

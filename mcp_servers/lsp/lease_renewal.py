"""Keep the leases of every session this MCP holds alive while it runs.

The server closes a leased session that was neither used nor renewed for
``ISABELLE_MAX_LEASE_AGE`` seconds (the abandoned-lease rule). Sessions this MCP
holds without using them for a while — warm scratch sessions and spares, or an
open file while a long check of another file runs — would otherwise be closed
under it. One background task renews every lease in the registry every
``Config.LEASE_RENEW_S`` seconds; when the MCP process dies, the renewals stop
and the server reaps its sessions as before.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import Config

logger = logging.getLogger(__name__)


def ensure(pool: Any) -> None:
    """Start the renewal task once, from inside the running event loop."""
    if Config.LEASE_RENEW_S <= 0:
        return
    task = getattr(pool, "_renew_task", None)
    if task is not None and not task.done():
        return
    try:
        pool._renew_task = asyncio.get_running_loop().create_task(_run(pool))
    except RuntimeError:  # no running loop (import time, sync tests)
        pool._renew_task = None


async def renew_all(pool: Any) -> int:
    """Renew the lease of every registered session; returns how many succeeded.
    A session the server no longer has is left to the tool that next uses it
    (rebind on 404, drop of a scratch session)."""
    renewed = 0
    c = await pool.client()
    for entry in pool.registry.all():
        if not entry.lease_id:
            continue
        try:
            await c.renew_lease(entry.session_id, lease_id=entry.lease_id)
            renewed += 1
        except Exception as e:  # noqa: BLE001  (one failure must not stop the others)
            logger.debug("lease renewal failed for %s: %s", entry.ref, e)
    return renewed


async def _run(pool: Any) -> None:
    while Config.LEASE_RENEW_S > 0:
        await asyncio.sleep(Config.LEASE_RENEW_S)
        try:
            await renew_all(pool)
        except Exception as e:  # noqa: BLE001  (server down: try again next round)
            logger.debug("lease renewal round failed: %s", e)

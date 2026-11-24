"""Memory facts for clients (plan §3.2): the snapshot and tier, builds in
flight, and every pool session with its expiry and attributed memory —
no lease ids, no advice (the MCP words the advice; owner decision D4)."""

import asyncio

from fastapi import APIRouter

from server.app.core.logging import get_logger

from ..deps import SessionManagerDep

router = APIRouter(tags=["memory"])
logger = get_logger(__name__)

_SESSION_FIELDS = (
    "session_id",
    "label",
    "task_group",
    "field",
    "status",
    "leased",
    "in_use",
    "idle_s",
    "idle_expires_at",
    "lease_expires_at",
    "expires_in_s",
    "rss_mb",
    "heap_mb",
    "mem_sampled_at",
    "mem_probe_error",
    "sledgehammer_mb",
    "sledgehammer_peak_mb",
    "sledgehammer_shared_with",
    "heap_in_use_mb",
)


@router.get("/api/v1/memory")
async def memory_status(session_manager: SessionManagerDep):
    """Memory facts: ``state`` (ok / warning / critical), ``memory`` (used,
    limit, available, pressure, source), ``builds_in_flight``,
    ``sledgehammer_inflight``, ``evictable`` / ``leased`` (capped, by RSS) and
    ``sessions`` (every pool session: expiry + rss_mb / heap_mb). The same
    facts a memory 503 carries, readable at any time."""
    facts = await asyncio.to_thread(session_manager.memory_facts)
    listing = await asyncio.to_thread(session_manager.list_sessions)
    facts["sessions"] = [{k: e.get(k) for k in _SESSION_FIELDS} for e in listing]
    logger.debug("memory status state=%s sessions=%s", facts.get("state"), len(listing))
    return facts

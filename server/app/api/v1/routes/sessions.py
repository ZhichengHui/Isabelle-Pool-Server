"""Session lifecycle: create, acquire, release, info, close, listings, history, stats."""
import asyncio
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from server.app.core import metrics
from server.app.core.config import Heap, Server
from server.app.core.logging import get_logger, logging_context

from server.app.dependencies import get_session_manager
from ..deps import (
    HeapPoolDep,
    LeasedSession,
    SessionManagerDep,
    require_admin_token,
    require_lease_id,
)
from ..schemas.API_models import (
    SessionAcquireRequest,
    SessionAcquireResponse,
    SessionCreateRequest,
    SessionResponse,
)
from ..serializers import session_status

router = APIRouter(tags=["sessions"])
logger = get_logger(__name__)


def _normalise_field(field: Optional[str]) -> Optional[str]:
    if field is None or str(field).strip() == "" or str(field).lower() in {"null", "none", "default"}:
        return None
    return field


def _resolve_heap_for_session(heap_pool, task_group: str, heap_session: Optional[str], project: Optional[str]):
    """Resolve + gate a heap for session creation (staleness included).

    Returns (theories, field, session_dirs, dependency_extra): the wrapper states
    the QUALIFIED heap theory names (load-bearing — the document's visible context
    comes from the wrapper), the session starts on field=<heap session name> with
    dirs=[root_dir]. HeapPoolError subclasses carry their HTTP status (mapped in
    main.py)."""
    entry = heap_pool.resolve_for_session(task_group, heap_session, project)
    theories = [
        f"{entry['session_name']}.{Path(f['path']).stem}"
        for f in entry.get("theory_files", [])
    ]
    dependency_extra = f"{task_group}:{entry['fingerprint']}"
    return theories, entry["session_name"], [entry["root_dir"]], dependency_extra


def _session_inputs(request, heap_pool):
    """Shared create/acquire preamble → (theories, field, task_group, session_dirs, dependency_extra)."""
    theories = request.theories if request.theories else None
    field = _normalise_field(request.field)
    task_group = request.task_group or Heap.DEFAULT_TASK_GROUP
    session_dirs = None
    dependency_extra = None
    if request.heap_session or request.project:
        heap_theories, field, session_dirs, dependency_extra = _resolve_heap_for_session(
            heap_pool, task_group, request.heap_session, request.project
        )
        # order kept: the later import wins a shared short name (D15)
        theories = list(dict.fromkeys((theories or []) + heap_theories))
    return theories, field, task_group, session_dirs, dependency_extra


@router.post("/api/v1/sessions", response_model=SessionResponse)
async def create_session(
    session_manager: SessionManagerDep,
    heap_pool: HeapPoolDep,
    request: Optional[SessionCreateRequest] = None,
):
    if request is None:
        request = SessionCreateRequest()
    theories, field, task_group, session_dirs, dependency_extra = _session_inputs(request, heap_pool)

    with logging_context(field=field or "default"):
        logger.info("creating session theories=%s task_group=%s", theories or [], task_group)
        session, lease_id = await session_manager.create_leased_session(
            theories=theories, field=field, task_group=task_group,
            session_dirs=session_dirs, dependency_extra=dependency_extra,
        )
        session.label = request.label

        logger.info("session created session_id=%s lease_id=%s label=%s task_group=%s", session.session_id, lease_id, request.label, task_group)
        return SessionResponse(
            session_id=str(session.session_id),
            created_at=session.created_at,
            theories=session.theories or [],
            status=session_status(session),
            lease_id=lease_id,
            label=session.label,
            task_group=task_group,
        )


@router.get("/api/v1/sessions")
async def list_sessions(session_manager: SessionManagerDep):
    """Public pool listing. Never carries lease ids: a lease token is an
    ownership proof for mutation endpoints, so publishing it would let any
    caller destroy any session (the lease leak). The full listing lives
    behind the token-gated admin endpoint below."""
    sessions = session_manager.list_sessions()
    logger.debug("listed %s sessions", len(sessions))
    return {"sessions": sessions} if sessions else {"sessions": []}


@router.get("/api/v1/admin/sessions")
async def list_sessions_admin(
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    session_manager=Depends(get_session_manager),
):
    """Admin pool listing WITH lease ids (for the admin console's force-close).
    Requires ISABELLE_ADMIN_TOKEN configured server-side and a matching
    X-Admin-Token header; 403 when unset or mismatched."""
    require_admin_token(x_admin_token)
    sessions = session_manager.list_sessions(include_lease=True)
    return {"sessions": sessions} if sessions else {"sessions": []}


@router.post("/api/v1/sessions/acquire", response_model=SessionAcquireResponse)
async def acquire_session(
    request: SessionAcquireRequest,
    session_manager: SessionManagerDep,
    heap_pool: HeapPoolDep,
):
    theories, field, task_group, session_dirs, dependency_extra = _session_inputs(request, heap_pool)

    with logging_context(field=field or "default"):
        logger.info(
            "acquire_session requested theories=%s reuse_dirty=%s task_group=%s",
            theories or [],
            request.reuse_dirty,
            task_group,
        )

        session, reused, lease_id = await session_manager.acquire_session(
            theories=theories,
            field=field,
            reuse_dirty=request.reuse_dirty,
            task_group=task_group,
            session_dirs=session_dirs,
            dependency_extra=dependency_extra,
        )
        # Label follows the CURRENT holder: applied on every acquire, fresh or
        # reused, so the admin console never shows a stale creator's label.
        session.label = request.label

        logger.info(
            "acquire_session result session_id=%s reused=%s lease_id=%s",
            session.session_id,
            reused,
            lease_id,
        )
        return SessionAcquireResponse(
            session_id=str(session.session_id),
            created_at=session.created_at,
            theories=session.theories or [],
            status=session_status(session),
            reused=reused,
            lease_id=lease_id,
            task_group=task_group,
        )


async def _cancel_fill_job(session_manager: Any, session_id: str) -> None:
    """Cancel + await the session's fill job (fill_sorries B2); no-op for managers without jobs."""
    cancel = getattr(session_manager, "cancel_job", None)
    if cancel is not None:
        await cancel(session_id)


@router.post("/api/v1/sessions/{session_id}/release")
async def release_session(
    session_id: str,
    session_manager: SessionManagerDep,
    x_lease_id: Optional[str] = Header(None, alias="X-Lease-Id"),
):
    """Release the exclusive lease on a session, returning it to the pool
    for reuse.  Unlike DELETE, the backend stays alive."""
    with logging_context(session_id=session_id):
        lease_id = require_lease_id(x_lease_id)
        # Validate the lease BEFORE touching the job (fill_sorries review 4c253fe B1):
        # a caller without the lease must not be able to cancel another client's job.
        session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)
        cancel = getattr(session_manager, "cancel_job", None)
        if cancel is not None:  # a fill job ends with the lease (fill_sorries B2)
            await cancel(session_id)
        logger.info("releasing session lease")
        session_manager.release_session(session_id, lease_id)
        logger.info("session lease released")
        return {"success": True, "session_id": session_id}


@router.get("/api/v1/sessions/{session_id}")
async def get_session_info(session: LeasedSession):
    logger.debug("fetching session info")
    return {
        "session_id": str(session.session_id),
        "created_at": session.created_at,
        "last_activity": session.last_activity,
        "status": session_status(session),
        "theories": session.theories,
        "loaded_theories": session.loaded_theories,
        "wrapper_theory": session.wrapper_theory,
        "dependency_key": session.dependency_key,
        "commands_executed": len(session.command_history),
        "checkpoints": len(session.checkpoints),
        "verified_theories": session.verified_theories if hasattr(session, "verified_theories") else [],
        "in_use": session.in_use,
        "active_requests": session.active_request_count,
        "label": session.label,
        "task_group": session.task_group,
    }


@router.delete("/api/v1/sessions/{session_id}")
async def close_session(
    session_id: str,
    x_lease_id: Optional[str] = Header(None, alias="X-Lease-Id"),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    session_manager=Depends(get_session_manager),
):
    with logging_context(session_id=session_id):
        lease_id = x_lease_id
        if not lease_id:
            # A session released back to the pool carries NO lease to present,
            # so it is uncloseable by lease check alone (the force-close gap
            # that made zombie sessions immortal). Closing one is an admin
            # action instead: X-Admin-Token against ISABELLE_ADMIN_TOKEN.
            if not (Server.ADMIN_TOKEN and x_admin_token == Server.ADMIN_TOKEN):
                raise HTTPException(
                    status_code=400,
                    detail="Missing X-Lease-Id header (or X-Admin-Token for unleased sessions)",
                )
            logger.info("force-closing unleased session via admin token")
            await _cancel_fill_job(
                session_manager, session_id
            )  # authorised: cancel BEFORE the backend stops (B2)
            await asyncio.to_thread(session_manager.close_session, session_id, require_lease=False)
        else:
            logger.info("closing session")
            # Validate the lease BEFORE the job is touched (review 4c253fe B1).
            session_manager.get_session(
                session_id, lease_id=lease_id, require_lease=True
            )
            await _cancel_fill_job(session_manager, session_id)
            await asyncio.to_thread(session_manager.close_session, session_id, lease_id=lease_id)
        # Destroy is unrecoverable for in-flight work: audit every one.
        metrics.sessions_force_closed.inc()
        logger.warning(
            "session force-closed via DELETE session_id=%s lease_prefix=%s",
            session_id, (lease_id or "admin")[:6],
        )
        return {"success": True}


@router.get("/api/v1/sessions/{session_id}/history")
async def get_command_history(session_id: str, session: LeasedSession, limit: int = 50):
    history = session.command_history[-limit:]
    logger.debug("returning command history entries=%s", len(history))
    return {
        "session_id": session_id,
        "total_commands": len(session.command_history),
        "history": history,
    }


@router.get("/api/v1/sessions/{session_id}/stats")
async def get_session_stats(session_id: str, session: LeasedSession):
    successful = sum(1 for cmd in session.command_history if cmd.get("success"))
    failed = len(session.command_history) - successful
    logger.debug("returning session stats")
    return {
        "session_id": session_id,
        "created_at": session.created_at,
        "duration": time.time() - session.created_at,
        "last_activity": session.last_activity,
        "total_commands": len(session.command_history),
        "successful_commands": successful,
        "failed_commands": failed,
        "success_rate": (successful / len(session.command_history)) if session.command_history else 0,
        "checkpoints_saved": len(session.checkpoints),
    }


@router.post("/api/v1/sessions/{session_id}/renew_lease")
async def renew_lease(session_id: str, session: LeasedSession):
    """Keep a lease alive without using the session: the abandoned-lease rule
    (``ISABELLE_MAX_LEASE_AGE``) counts from the later of the last use and the
    last renewal. A client that holds sessions it has not used for a while (warm
    spares) renews them; a client that dies stops renewing and is reaped."""
    session.lease_renewed_at = time.time()
    return {"success": True, "session_id": session_id,
            "lease_expires_in_s": Server.MAX_LEASE_AGE}

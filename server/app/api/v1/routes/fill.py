"""``fill_sorries``: submit a proof with ``sorry``s, poll the per-session job,
cancel it (plan 2026-10-18 §1.2, §3.1–3.2). The agent-facing wording lives in
the MCP (D-4 of the memory plan); these routes return facts."""

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from server.app.core.logging import get_logger
from server.app.services.memory_hooks import PROVER_DIED_TEXT, run_with_memory_hooks

from ..deps import LeasedSession, SessionManagerDep
from ..schemas.fill_models import FillSorriesRequest

router = APIRouter(tags=["fill"])
logger = get_logger(__name__)


def _in_flight() -> JSONResponse:
    """409 with a top-level `error` key, like the 503 bodies (plan §3.1 / review S3)."""
    return JSONResponse(
        status_code=409,
        content={
            "error": "fill_in_flight",
            "detail": "a fill_sorries job is already running on this session",
        },
    )


@router.post("/api/v1/sessions/{session_id}/fill_sorries", status_code=202)
async def fill_sorries(
    request: FillSorriesRequest,
    session: LeasedSession,
    session_manager: SessionManagerDep,
) -> Any:
    """Load/verify ``text``, locate its ``sorry`` commands, start the job. 202 with
    the initial state; 409 ``fill_in_flight`` while a job runs on this session."""
    old = session_manager.fill_job(session.session_id)
    if old is not None and old.status in ("queued", "running"):
        return _in_flight()

    def call() -> Any:
        if request.mode == "document":
            extra = {"master_dir": request.master_dir} if request.master_dir else {}
            return session.load_document(
                request.text, None, None, request.load_timeout, True, **extra
            )
        return session.verify_chunk(request.text, request.load_timeout)

    result, override = await asyncio.to_thread(
        run_with_memory_hooks, call, session, session_manager
    )
    if override:
        raise HTTPException(status_code=404, detail=PROVER_DIED_TEXT)
    if request.mode == "document":
        report = (session.last_chunk_report or {}).get("report") or {}
    else:
        report = (result or {}).get("report") or {}
    commands = report.get("commands") or []
    if not any(c.get("kind") == "sorry" for c in commands):
        return JSONResponse(
            status_code=200,
            content={
                "status": "done",
                "n": 0,
                "done": 0,
                "filled": 0,
                "results": [],
                "patched_text": request.text,
                "eta_s": {"worst": 0.0, "typical": 0.0},
                "load_success": bool(report.get("success")),
                "load_error": report.get("error"),
            },
        )
    try:
        job = session_manager.start_fill(
            session,
            text=request.text,
            mode=request.mode,
            commands=commands,
            timeout_s=request.timeout_s,
            max_total_s=request.max_total_s,
            provers=request.provers or "",
        )
    except ValueError:
        return _in_flight()
    logger.info(
        "fill_sorries started session_id=%s sorries=%s",
        session.session_id,
        len(job.targets),
    )
    body = session_manager.fill_status(session.session_id) or {}
    body["load_success"] = bool(report.get("success"))
    return body


@router.get("/api/v1/sessions/{session_id}/fill_sorries")
async def fill_status(
    session: LeasedSession, session_manager: SessionManagerDep
) -> Any:
    """Current job state (plan §3.2). Polling counts as activity so the idle
    sweep does not reap a session whose only traffic is the poll (review B2)."""
    session.update_activity()  # type: ignore[no-untyped-call]
    body = session_manager.fill_status(session.session_id)
    if body is None:  # top-level `error` like the 409/503 bodies (review aa96c6d S3)
        return JSONResponse(
            status_code=404,
            content={
                "error": "no_fill_job",
                "detail": "no fill_sorries job on this session",
            },
        )
    return body


@router.delete("/api/v1/sessions/{session_id}/fill_sorries")
async def fill_cancel(
    session: LeasedSession, session_manager: SessionManagerDep
) -> Dict[str, Any]:
    """Cancel the session\'s job; the current step ends within its own budget."""
    cancelled = await session_manager.cancel_job(session.session_id)
    body: Optional[Dict[str, Any]] = session_manager.fill_status(session.session_id)
    return {"cancelled": cancelled, **(body or {"status": "none"})}

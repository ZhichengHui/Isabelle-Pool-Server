"""Position-based (jEdit/LSP-style) read-only queries on the current
document: command at line, goals at line, hover, definition, the
overlay-driven sledgehammer at a line, and candidate proofs at a line
(attempt)."""
import asyncio
import time

from fastapi import APIRouter, Query

from server.app.core.config import Server
from server.app.core.logging import get_logger
from server.app.services.success_checker import SH_PROVED, classify_sledgehammer

from server.app.errors import SessionNotFound
from server.app.services.session_attempt import ATTEMPT_TIMEOUT_MAX_S, effective_timeout

from ..deps import LeasedSession, SessionManagerDep, sledgehammer_slot
from ..memory_hooks import PROVER_DIED_TEXT, run_with_memory_hooks, track_atps
from ..schemas.attempt_models import AttemptRequest, AttemptResponse
from ..schemas.API_models import (
    CommandAtLineResponse,
    DefinitionResponse,
    DefinitionTarget,
    GoalsResponse,
    HoverResponse,
    SledgehammerAtRequest,
    SledgehammerAtResponse,
)
from ..serializers import parse_command_range, parse_located_command, to_ascii

router = APIRouter(tags=["positional"])

SHOW_STATES_OFF_ERROR = (
    "goal states are not recorded: the server runs with ISABELLE_SHOW_STATES=false "
    "(set it to true and restart the server); use isabelle_proof_state for the state "
    "at the end of the file"
)
logger = get_logger(__name__)


@router.get("/api/v1/sessions/{session_id}/command_at_line", response_model=CommandAtLineResponse)
async def command_at_line(session: LeasedSession, line: int = Query(..., ge=1)):
    """Read-only jEdit-style query: the command containing `line` (1-based) of the
    current node. Snapshot-based — the proof script, rollback chain, and command
    history are untouched, and it works past a trailing theory `end`."""
    result = await asyncio.to_thread(session.command_at_line, line)
    return CommandAtLineResponse(
        found=bool(result.get("found", False)),
        kind=result.get("kind"),
        source=result.get("source"),
        range=parse_command_range(result.get("range")),
        error=result.get("error"),
    )


@router.get("/api/v1/sessions/{session_id}/goals", response_model=GoalsResponse)
async def goals_at_line(session: LeasedSession, line: int = Query(..., ge=1)):
    """Read-only jEdit-style query: rendered goal state before/after the command
    containing `line` (1-based). Snapshot-based like command_at_line. Requires
    show_states on (ISABELLE_SHOW_STATES, default true); with it off no state is
    recorded and the response says so in `error` instead of returning goal lists
    that are empty only because of the setting."""
    result = await asyncio.to_thread(session.goals_at_line, line)
    error = result.get("error")
    if error is None and not Server.SHOW_STATES:
        error = SHOW_STATES_OFF_ERROR
    return GoalsResponse(
        found=bool(result.get("found", False)),
        command=parse_located_command(result.get("command")),
        goals_before=[to_ascii(str(g)) for g in (result.get("goals_before") or [])],
        goals_after=[to_ascii(str(g)) for g in (result.get("goals_after") or [])],
        error=error,
    )


@router.get("/api/v1/sessions/{session_id}/hover", response_model=HoverResponse)
async def hover_at(session: LeasedSession, line: int = Query(..., ge=1), col: int = Query(..., ge=1)):
    """Hover info at a 1-based line/col (UTF-16 columns). Snapshot + Rendering —
    no evaluation, no edits; works at any document position."""
    result = await asyncio.to_thread(session.hover_at, line, col)
    return HoverResponse(
        found=bool(result.get("found", False)),
        range=parse_command_range(result.get("range")),
        contents=[to_ascii(str(c)) for c in (result.get("contents") or [])],
        error=result.get("error"),
    )


@router.get("/api/v1/sessions/{session_id}/definition", response_model=DefinitionResponse)
async def definition_at(session: LeasedSession, line: int = Query(..., ge=1), col: int = Query(..., ge=1)):
    """Go-to-definition at a 1-based line/col. Heap/source entities resolve to
    file positions; entry-document entities to in-node line ranges."""
    result = await asyncio.to_thread(session.definition_at, line, col)
    targets = [
        DefinitionTarget(**{k: v for k, v in t.items() if k in DefinitionTarget.model_fields})
        for t in (result.get("targets") or [])
        if isinstance(t, dict)
    ]
    return DefinitionResponse(
        found=bool(result.get("found", False)),
        targets=targets,
        error=result.get("error"),
    )


@router.post("/api/v1/sessions/{session_id}/sledgehammer_at", response_model=SledgehammerAtResponse)
async def sledgehammer_at(
    request: SledgehammerAtRequest,
    session: LeasedSession,
    session_manager: SessionManagerDep,
):
    """Position-explicit sledgehammer (overlay print op; no text edits). Shares
    the server-wide sledgehammer semaphore with the tip-based endpoint."""
    start = time.time()
    async with sledgehammer_slot(session_manager), track_atps(session_manager, session):
        result = await asyncio.to_thread(
            session.sledgehammer_at, request.line, request.subgoal, request.timeout_s)
    results = [to_ascii(str(r)) for r in (result.get("results") or [])]
    # `found` used to copy the backend's flag, which is true whenever the overlay
    # answered — including "No proof found". A proof is found only on "Try this:".
    outcome, method = classify_sledgehammer(results, error=result.get("error"))
    return SledgehammerAtResponse(
        found=outcome == SH_PROVED,
        results=results,
        error=result.get("error"),
        execution_time=time.time() - start,
        outcome=outcome,
        method=method,
    )


@router.post("/api/v1/sessions/{session_id}/attempt", response_model=AttemptResponse)
async def attempt(
    request: AttemptRequest,
    session: LeasedSession,
    session_manager: SessionManagerDep,
):
    """Try candidate proofs at a line, in this session (overlay queries; no text edits):
    each candidate continues from the state after the last command that ends before
    `line`, the candidates run in parallel (at most 8 at once), each within `timeout_s`
    (default 30, capped at 300). Candidates with ML commands are refused like
    documents are. See services/session_attempt.py."""
    start = time.time()
    timeout_s, capped = effective_timeout(request.timeout_s)
    answer, override = await asyncio.to_thread(
        run_with_memory_hooks,
        lambda: session.attempt(request.line, request.candidates, timeout_s),
        session,
        session_manager,
    )
    if override or answer is None:
        raise SessionNotFound(PROVER_DIED_TEXT)
    for cand in answer.get("candidates") or []:
        rep = cand.get("report")
        if rep:
            if rep.get("subgoals") is not None:
                rep["subgoals"] = [to_ascii(str(g)) for g in rep["subgoals"]]
            for cmd in rep.get("commands") or []:
                for m in cmd.get("messages") or []:
                    m["text"] = to_ascii(m.get("text"))
    note = (f"timeout {request.timeout_s} s is above the server cap; the candidates ran "
            f"with {ATTEMPT_TIMEOUT_MAX_S} s") if capped else None
    return AttemptResponse(
        fallback=answer.get("fallback"),
        host_line=answer.get("host_line"),
        timeout_s=timeout_s,
        timeout_capped=capped,
        note=note,
        candidates=answer.get("candidates") or [],
        execution_time=time.time() - start,
    )

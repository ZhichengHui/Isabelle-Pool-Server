"""MCP-side translation of the server's memory FACTS into agent ADVICE
(plan §2.C; owner decision D4 — the server never words advice).

Inputs are the server's structured payloads: the 503 body (``error`` key,
plan §3.1/§3.1a), ``GET /api/v1/memory`` and the ``X-Memory-State`` header.
Outputs name THIS server's tools (``ToolNames``) and the agent's own sessions
by what they are for — never a session id, a lease, or a server URL.

Advice rules (plan §2.C B2), in order:
1. the agent holds sessions it is not using (scratch, or idle for at least
   ``idle_s``) → release them, then retry after ``retry_after_s``;
2. nothing to release but the server lists idle sessions of others → retry
   after ``retry_after_s`` (the server reclaims those itself);
3. repeated failures, builds in flight, other task groups holding the memory,
   or external pressure → stop and tell the user: the agent cannot free that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .registry import KIND_SCRATCH, KIND_THEORY, Entry, SessionRegistry

MEMORY_PRESSURE = "memory_pressure"


@dataclass(frozen=True)
class ToolNames:
    """How THIS MCP server names the actions the advice refers to."""

    release_idle: str
    close: str
    list_sessions: str
    memory_status: str
    reopen: str


def parse_http_error(exc: BaseException) -> Dict[str, Any]:
    """Status, the server's ``error`` key, detail, the JSON body and the
    retry hint of an ``httpx.HTTPStatusError`` (anything else: status None)."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    body: Any = {}
    if resp is not None:
        try:
            body = resp.json()
        except Exception:  # pylint: disable=broad-exception-caught
            body = {}
    if not isinstance(body, dict):
        body = {"detail": body}
    retry: Any = body.get("retry_after_s")
    if retry is None and resp is not None:
        retry = resp.headers.get("Retry-After")
    try:
        retry_s: Optional[int] = int(retry) if retry is not None else None
    except (TypeError, ValueError):
        retry_s = None
    return {
        "status": status,
        "error": body.get("error"),
        "detail": body.get("detail") or str(exc),
        "body": body,
        "retry_after_s": retry_s,
    }


def others_summary(facts: Dict[str, Any], owned_ids: set[str]) -> Dict[str, Any]:
    """What the agent does NOT own, aggregated (per task group, builds,
    external pressure) — the material for the "tell the user" message.
    Rows without an attributed size yet (``rss_mb`` None: no sweep has
    sampled them) are counted as ``unmeasured``, never as 0 MB."""
    rows = list(facts.get("evictable") or []) + list(facts.get("leased") or [])
    others = [r for r in rows if str(r.get("session_id")) not in owned_ids]
    builds = facts.get("builds_in_flight") or {}
    measured = [r for r in others if r.get("rss_mb") is not None]
    return {
        "sessions": len(others),
        "rss_mb": round(sum(float(r.get("rss_mb") or 0.0) for r in measured), 1),
        "unmeasured": len(others) - len(measured),
        "sledgehammer_mb": round(
            sum(float(r.get("sledgehammer_mb") or 0.0) for r in others), 1
        ),
        "task_groups": sorted(
            {str(r["task_group"]) for r in others if r.get("task_group")}
        ),
        "builds_in_flight": int(sum(int(v) for v in builds.values())) if builds else 0,
        "external_pressure": facts.get("external_pressure"),
    }


def _releasable(owned: List[Entry], idle_s: float, now: Optional[float]) -> List[Entry]:
    """Sessions the agent can give up without losing work: scratch sessions
    always, bindings/batch items idle for ``idle_s``. NEVER the stepwise
    theory session — idle there means the model was thinking, and closing it
    discards the live proof state (review d2bc679 S2)."""
    return [
        e
        for e in owned
        if e.kind != KIND_THEORY and (e.kind == KIND_SCRATCH or e.idle_s(now) >= idle_s)
    ]


def _theory_note(owned: List[Entry], tools: ToolNames) -> Optional[str]:
    if any(e.kind == KIND_THEORY for e in owned):
        return (
            f"Your current theory session is yours to keep: finish it, or {tools.close} "
            f"if you no longer need it."
        )
    return None


def _others_text(others: Dict[str, Any]) -> str:
    parts = []
    if others["sessions"]:
        groups = (
            f" in task group(s) {', '.join(others['task_groups'])}"
            if others["task_groups"]
            else ""
        )
        unmeasured = int(others.get("unmeasured") or 0)
        if unmeasured and not others["rss_mb"]:
            amount = "memory not yet measured"
        elif unmeasured:
            amount = f">= {others['rss_mb']} MB measured, {unmeasured} not yet measured"
        else:
            amount = f"~{others['rss_mb']} MB"
        sh = float(others.get("sledgehammer_mb") or 0.0)
        if sh:
            amount += f" plus ~{sh} MB of running sledgehammers"
        parts.append(
            f"{others['sessions']} session(s) of other clients{groups} hold {amount}"
        )
    if others["builds_in_flight"]:
        parts.append(f"{others['builds_in_flight']} theory build(s) are running")
    if others["external_pressure"]:
        parts.append("memory pressure comes from outside the prover server")
    return "; ".join(parts) if parts else "the prover server is out of memory"


def advise(  # pylint: disable=too-many-arguments
    facts: Dict[str, Any],
    owned: List[Entry],
    *,
    tools: ToolNames,
    failures: int = 0,
    idle_s: float = 120.0,
    now: Optional[float] = None,
) -> List[str]:
    """Ordered advice lines for a memory refusal (see the module docstring)."""
    retry = int(facts.get("retry_after_s") or 5)
    releasable = _releasable(owned, idle_s, now)
    others = others_summary(facts, {e.session_id for e in owned})
    advice: List[str] = []
    if releasable:
        names = ", ".join(e.describe(now) for e in releasable[:5])
        advice.append(
            f"Release the {len(releasable)} session(s) you hold but are not using "
            f"({names}): call {tools.release_idle}, then retry in {retry} s."
        )
    elif facts.get("evictable"):
        advice.append(
            f"You hold nothing idle; the server will reclaim {len(facts['evictable'])} idle "
            f"session(s) of other clients itself — retry in {retry} s."
        )
    note = _theory_note(owned, tools)
    if note:
        advice.append(note)
    if not any(a.startswith(("Release", "You hold")) for a in advice):
        advice.append(
            f"Retry in {retry} s; if it keeps failing, tell the user the prover server "
            f"is out of memory ({tools.memory_status} shows the numbers)."
        )
    escalate = (
        failures >= 2
        or bool(others["external_pressure"])
        or (not releasable and (others["builds_in_flight"] or others["sessions"]))
    )
    if escalate:
        advice.append(
            "If it still fails, stop and tell the user: "
            + _others_text(others)
            + " — this agent cannot free that memory."
        )
    return advice


def translate_http_error(  # pylint: disable=too-many-arguments
    exc: BaseException,
    registry: SessionRegistry,
    owner: Any,
    *,
    tools: ToolNames,
    idle_s: float = 120.0,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The agent-facing result for a failed server call (plan §3.3)."""
    info = parse_http_error(exc)
    status, error, retry = info["status"], info["error"], info["retry_after_s"] or 5
    if status == 503 and error == MEMORY_PRESSURE:
        failures = registry.note_memory_failure(owner)
        facts = info["body"]
        registry.merge(
            list(facts.get("leased") or []) + list(facts.get("evictable") or [])
        )
        owned = registry.owned(owner)
        memory = facts.get("memory") or {}
        return {
            "error": MEMORY_PRESSURE,
            "state": facts.get("state"),
            "memory": {
                k: memory.get(k) for k in ("pressure_pct", "available_mb", "limit_mb")
            },
            "your_sessions": [e.to_agent(now) for e in owned],
            "others": others_summary(facts, {e.session_id for e in owned}),
            "advice": advise(
                facts, owned, tools=tools, failures=failures, idle_s=idle_s, now=now
            ),
            "retry_after_s": retry,
            "failures_in_a_row": failures,
        }
    if status == 503 and error == "gateway_unavailable":
        return {
            "error": "server_recovering",
            "retry_after_s": retry,
            "advice": [
                f"The prover server is restarting its gateway; retry in {retry} s."
            ],
        }
    if status == 503:
        return {
            "error": "pool_full",
            "detail": info["detail"],
            "retry_after_s": retry,
            "advice": [
                f"Every prover session is busy or held; release what you no longer "
                f"need ({tools.release_idle}) and retry in {retry} s."
            ],
        }
    if status == 409 and error == "image_not_current":
        return {
            "error": "image_not_current",
            "detail": info["detail"],
            "advice": [
                "The session image this file starts on is out of date (its sources changed "
                "after it was built), so no session can start on it until it is rebuilt."
            ],
        }
    if status == 409:
        return {
            "error": "busy",
            "advice": [
                "The session is still running a previous command; wait for it, "
                "then retry."
            ],
        }
    if status in (404, 410):
        return {
            "error": "session_gone",
            "detail": info["detail"],
            "advice": [
                "The session was closed or evicted (idle timeout, lease expiry, "
                f"memory, or a dead prover): {tools.reopen}."
            ],
        }
    return {
        "error": f"http_{status}" if status else type(exc).__name__,
        "detail": info["detail"],
    }


def reminder(  # pylint: disable=too-many-arguments
    state: Optional[str],
    owned: List[Entry],
    *,
    tools: ToolNames,
    idle_s: float = 120.0,
    now: Optional[float] = None,
    pressure_pct: Optional[float] = None,
) -> Optional[str]:
    """The reminder appended to tool results while ``X-Memory-State`` is
    warning / critical (owner decision D3: remind, never evict)."""
    if state not in ("warning", "critical"):
        return None
    pct = f", {pressure_pct}% used" if pressure_pct is not None else ""
    idle = _releasable(owned, idle_s, now)
    if idle:
        names = ", ".join(e.describe(now) for e in idle[:5])
        body = (
            f"you hold {len(idle)} idle session(s) ({names}). Release what you no longer "
            f"need: {tools.release_idle} or {tools.close}."
        )
    elif owned:
        body = (
            f"you hold {len(owned)} session(s), none releasable. Finish or close the "
            f"work you no longer need ({tools.close})."
        )
    else:
        body = "you hold no sessions; other clients or builds are using the memory."
    if state == "critical":
        body += " Release now or expect memory failures."
    return f"MEMORY REMINDER ({state}{pct}): {body}"


def with_reminder(out: Any, note: str) -> Any:
    """Attach the reminder to a tool result: JSON objects get a
    ``memory_reminder`` key, anything else gets the note appended."""
    if not isinstance(out, str):
        return out
    try:
        obj = json.loads(out)
    except ValueError:
        return out + "\n\n" + note
    if isinstance(obj, dict):
        obj["memory_reminder"] = note
        return json.dumps(obj, default=str)
    return out + "\n\n" + note

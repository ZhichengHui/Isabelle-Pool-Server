"""Candidate proofs tried in the file's own session (efficiency plan stage C).

``SessionAttemptMixin.attempt`` runs ``Backend_Attempts.attempt`` (Scala,
backend_attempts.scala): every candidate is an overlay query on the last command
that ends before ``line``, so it continues from that command's result state — no
scratch session, no re-check of the file before ``line``. ``attempt_reports`` turns
the backend's raw answer into one report per candidate in the shape of a
``load_document(report=True)`` report of "file text before ``line`` + candidate", the
report the scratch-session implementation produced, so the MCP judges both alike.

``cancel_attempts`` stops the running attempt from any thread (not queued behind it on
the session's worker); the memory protection uses it with reason ``"memory"``.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional

from server.app.core.logging import get_logger

logger = get_logger(__name__)

# Per-candidate time limit (s): default and server cap (plan C2). A larger request runs
# at the cap and its result says so.
ATTEMPT_TIMEOUT_DEFAULT_S = 30
ATTEMPT_TIMEOUT_MAX_S = 300
# Candidates running at once; more are queued. 0: as many as the session has worker threads
# (Isabelle option `threads`, ISABELLE_SESSION_THREADS, default 4), decided by the backend.
# Deviation from plan C3 (8): a candidate runs as a print function, which needs a worker of the
# session's farm, so with 4 workers 8 overlays run in two waves (prototype: 8 x 5 s took 10.2 s
# with threads=4, 7.2 s with threads=10). The thread count is a setting of the whole prover and
# raising it lets ordinary file checks use more threads too (peak memory, Bug 6); 4 covers the
# observed calls (334 multi_attempt calls: candidates p50 2, p99 5, max 10).
ATTEMPT_MAX_PARALLEL = int(os.getenv("ISABELLE_ATTEMPT_MAX_PARALLEL", "0"))
# What the waves are computed with when ATTEMPT_MAX_PARALLEL is 0 (the backend's default).
_SESSION_THREADS = int(os.getenv("ISABELLE_SESSION_THREADS", "4")) or 8
# Scala removes an overlay this long after the candidate's time limit (Backend_Attempts.GRACE_MS).
ATTEMPT_GRACE_S = 15


def effective_timeout(requested: Optional[float]) -> tuple[int, bool]:
    """(whole seconds the candidates run with, whether the request was capped)."""
    if requested is None or requested <= 0:
        return ATTEMPT_TIMEOUT_DEFAULT_S, False
    t = max(1, math.ceil(requested))
    return (ATTEMPT_TIMEOUT_MAX_S, True) if t > ATTEMPT_TIMEOUT_MAX_S else (t, False)


def backend_wait_s(n_candidates: int, timeout_s: int) -> float:
    """Upper bound of the backend call: the candidates run in waves (as many at once
    as run in parallel), each bounded by timeout + grace, plus the prefix wait and
    slack for the polling."""
    parallel = ATTEMPT_MAX_PARALLEL if ATTEMPT_MAX_PARALLEL > 0 else _SESSION_THREADS
    waves = max(1, math.ceil(n_candidates / parallel))
    return (waves + 1) * (timeout_s + ATTEMPT_GRACE_S) + 30.0


def _candidate_report(line: int, prefix: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    """The report of "file before ``line`` + candidate" from the backend's prefix and
    candidate parts. ``commands`` holds the prefix's failed/running commands and every
    command of the candidate (``node_line`` = file line)."""
    commands: List[Dict[str, Any]] = [
        {"node_line": c.get("node_line"), "line": c.get("node_line"), "kind": c.get("kind"),
         "status": c.get("status"), "messages": c.get("messages") or []}
        for c in prefix.get("failed") or []
    ]
    sorry_lines = list(prefix.get("sorry_lines") or [])
    cand_ok = True
    used_sorry = False
    for c in cand.get("commands") or []:
        node_line = line + int(c.get("line") or 1) - 1
        msg = c.get("messages") or ""
        commands.append({
            "node_line": node_line, "line": node_line, "kind": c.get("kind"),
            "status": c.get("status"),
            "messages": [{"sev": "error", "text": msg}] if msg else [],
        })
        if c.get("status") != "ok":
            cand_ok = False
        elif c.get("sorry"):
            used_sorry = True
            sorry_lines.append(node_line)
    error = cand.get("error")
    if error:  # the operation itself failed (not a command of the candidate)
        cand_ok = False
        commands.append({"node_line": line, "line": line, "kind": None, "status": "failed",
                         "messages": [{"sev": "error", "text": error}]})
    # a candidate the memory guard cancelled was stopped, not timed out (cancel_attempts)
    timed_out = ((bool(cand.get("timed_out")) and not cand.get("cancelled"))
                 or not prefix.get("settled", True))
    success = (not timed_out and bool(prefix.get("all_ok")) and cand_ok
               and bool(cand.get("finished")))
    proof_open = success and bool(cand.get("in_proof"))
    goals = [str(g).strip() for g in cand.get("goals") or []]  # as session.open_subgoals
    return {
        "success": success,
        "timed_out": timed_out,
        "proof_open": proof_open,
        "pending_qed": proof_open and not goals,
        "used_sorry": used_sorry,
        "sorry_lines": sorted(sorry_lines),
        "commands": commands,
        "subgoals": goals if proof_open else None,
    }


def attempt_reports(raw: Dict[str, Any], line: int, candidates: List[str]) -> Dict[str, Any]:
    """The endpoint's answer from the backend's JSON: ``fallback`` (whole call) or one
    entry per candidate: ``{candidate, fallback?, report, elapsed_s, cancelled}``."""
    if raw.get("fallback"):
        return {"fallback": raw["fallback"], "candidates": []}
    prefix = raw.get("prefix") or {}
    out = []
    for i, cand in enumerate(raw.get("candidates") or []):
        text = candidates[i] if i < len(candidates) else ""
        if cand.get("fallback"):
            out.append({"candidate": text, "fallback": cand["fallback"],
                        "command": cand.get("command")})
            continue
        out.append({
            "candidate": text,
            "fallback": None,
            "report": _candidate_report(line, prefix, cand),
            "elapsed_s": round(float(cand.get("elapsed_ms") or 0) / 1000.0, 3),
            "cancelled": cand.get("cancelled"),
        })
    return {"fallback": None, "host_line": raw.get("host_line"), "candidates": out}


class SessionAttemptMixin:  # pylint: disable=too-few-public-methods
    """``attempt`` / ``cancel_attempts`` of a session (see the module docstring)."""

    backend: Any
    session_id: Any

    def attempt(self, line: int, candidates: List[str], timeout_s: int) -> Dict[str, Any]:
        """Run the candidates at ``line`` (1-based) with ``timeout_s`` each; the
        answer of :func:`attempt_reports`. Busy/activity accounted like sledgehammer."""
        self.update_activity()  # type: ignore[attr-defined]
        self._acquire_request()  # type: ignore[attr-defined]
        try:
            logger.info("attempt line=%s candidates=%s timeout_s=%s", line, len(candidates), timeout_s)
            raw = self._call_backend(  # type: ignore[attr-defined]
                lambda: self.backend.raw.attempt(
                    int(line), json.dumps(list(candidates)), int(timeout_s), ATTEMPT_MAX_PARALLEL),
                timeout=backend_wait_s(len(candidates), timeout_s),
            )
            try:
                parsed = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            return attempt_reports(parsed, line, candidates)
        finally:
            self._release_request()  # type: ignore[attr-defined]

    def cancel_attempts(self, reason: str) -> int:
        """Cancel the running attempt of this session, if any (its candidates are
        reported with ``cancelled`` = ``reason``). Called directly on the gateway, NOT
        through the session's worker queue, which the attempt occupies. Returns the
        number of candidates cancelled (0: none running, or the backend is gone)."""
        try:
            return int(self.backend.raw.cancel_attempts(str(reason)))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("cancel_attempts failed session_id=%s", self.session_id)
            return 0

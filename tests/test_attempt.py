"""Candidate proofs in the file session (efficiency plan stage C), server side: the
backend's raw answer -> per-candidate reports (services/session_attempt.py), the
session method, and POST /api/v1/sessions/{id}/attempt. No Isabelle."""

from __future__ import annotations

import contextlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from server.app.dependencies import get_session_manager
from server.app.main import app
from server.app.services import session_attempt as sa

SID = "22222222-2222-2222-2222-222222222222"
LEASE = {"X-Lease-Id": "lease-1"}

PREFIX_OK = {"all_ok": True, "settled": True, "failed": [], "sorry_lines": []}


def _cand(commands, *, finished=True, timed_out=False, in_proof=False, goals=(),
          elapsed_ms=1200, cancelled=None, error=None, index=0):
    return {"index": index, "finished": finished, "timed_out": timed_out, "in_proof": in_proof,
            "goals": list(goals), "commands": commands, "elapsed_ms": elapsed_ms,
            "cancelled": cancelled, "error": error}


def _cmd(line, kind, status="ok", sorry=False, messages=""):
    return {"line": line, "kind": kind, "status": status, "sorry": sorry, "messages": messages}


def _report(cand, prefix=PREFIX_OK, line=10, text="x"):
    raw = {"fallback": None, "host_line": line - 1, "prefix": prefix, "candidates": [cand]}
    out = sa.attempt_reports(raw, line, [text])
    assert out["fallback"] is None
    return out["candidates"][0]


def test_a_closed_proof_is_a_success_without_open_goals():
    c = _report(_cand([_cmd(1, "lemma"), _cmd(2, "by")]))
    rep = c["report"]
    assert rep["success"] is True and rep["proof_open"] is False and rep["pending_qed"] is False
    assert rep["subgoals"] is None and c["elapsed_s"] == 1.2 and c["cancelled"] is None
    assert [x["node_line"] for x in rep["commands"]] == [10, 11]  # file lines


def test_open_goals_and_pending_qed():
    rep = _report(_cand([_cmd(1, "apply")], in_proof=True, goals=["1. A", "2. B\n   C"]))["report"]
    assert rep["proof_open"] is True and rep["pending_qed"] is False
    assert rep["subgoals"] == ["1. A", "2. B\n   C"]
    rep = _report(_cand([_cmd(1, "apply")], in_proof=True, goals=[]))["report"]
    assert rep["proof_open"] is True and rep["pending_qed"] is True


def test_a_failed_command_fails_the_candidate_and_carries_its_message():
    rep = _report(_cand([_cmd(1, "apply", "failed", messages="Failed to apply proof method"),
                         _cmd(2, "done", "failed", messages="Failed to finish proof")],
                        in_proof=True, goals=["1. A"]))["report"]
    assert rep["success"] is False and rep["proof_open"] is False and rep["subgoals"] is None
    assert rep["commands"][0]["messages"] == [{"sev": "error", "text": "Failed to apply proof method"}]


def test_sorry_lines_join_the_prefix_and_the_candidate():
    prefix = dict(PREFIX_OK, sorry_lines=[4])
    rep = _report(_cand([_cmd(1, "lemma"), _cmd(2, "sorry", sorry=True)]), prefix=prefix)["report"]
    assert rep["sorry_lines"] == [4, 11] and rep["used_sorry"] is True and rep["success"] is True


def test_a_failed_prefix_is_listed_and_fails_the_whole_text():
    prefix = {"all_ok": False, "settled": True, "sorry_lines": [],
              "failed": [{"node_line": 3, "kind": "by", "status": "failed",
                          "messages": [{"sev": "error", "text": "boom"}]}]}
    rep = _report(_cand([_cmd(1, "lemma"), _cmd(1, "by")]), prefix=prefix)["report"]
    assert rep["success"] is False
    assert rep["commands"][0] == {"node_line": 3, "line": 3, "kind": "by", "status": "failed",
                                  "messages": [{"sev": "error", "text": "boom"}]}


def test_time_limit_and_unsettled_prefix_are_timeouts():
    rep = _report(_cand([_cmd(1, "lemma"), _cmd(2, "apply", "running")], timed_out=True))["report"]
    assert rep["timed_out"] is True and rep["success"] is False
    prefix = dict(PREFIX_OK, all_ok=False, settled=False)
    rep = _report(_cand([_cmd(1, "lemma")]), prefix=prefix)["report"]
    assert rep["timed_out"] is True and rep["success"] is False


def test_unfinished_cancelled_and_operation_errors_are_not_successes():
    c = _report(_cand([_cmd(1, "lemma")], finished=False, timed_out=True, cancelled="memory"))
    assert c["cancelled"] == "memory" and c["report"]["success"] is False
    rep = _report(_cand([], error="Bad arguments"))["report"]
    assert rep["success"] is False and rep["commands"][-1]["status"] == "failed"


def test_fallbacks_pass_through():
    assert sa.attempt_reports({"fallback": "multiline_command", "candidates": []}, 5, ["a"]) == \
        {"fallback": "multiline_command", "candidates": []}
    raw = {"fallback": None, "prefix": PREFIX_OK,
           "candidates": [{"index": 0, "fallback": "candidate_command", "command": "end"}]}
    c = sa.attempt_reports(raw, 5, ["lemma x: True by simp\nend"])["candidates"][0]
    assert c["fallback"] == "candidate_command" and c["command"] == "end" and "report" not in c


def test_effective_timeout_default_and_cap():
    assert sa.effective_timeout(None) == (30, False)
    assert sa.effective_timeout(0) == (30, False)
    assert sa.effective_timeout(12.2) == (13, False)
    assert sa.effective_timeout(300) == (300, False)
    assert sa.effective_timeout(1000) == (300, True)


def test_backend_wait_covers_the_waves():
    # 4 at once by default: 5 candidates are 2 waves
    assert sa.backend_wait_s(4, 30) < sa.backend_wait_s(5, 30)
    assert sa.backend_wait_s(5, 30) >= 2 * (30 + sa.ATTEMPT_GRACE_S)


class _Raw:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def attempt(self, line, candidates_json, timeout_s, max_parallel):
        self.calls.append((line, candidates_json, timeout_s, max_parallel))
        return json.dumps(self.answer)

    def cancel_attempts(self, reason):
        self.calls.append(("cancel", reason))
        return 3


class _Backend:
    def __init__(self, raw):
        self.raw = raw


class _MixinSession(sa.SessionAttemptMixin):
    def __init__(self, raw):
        self.backend = _Backend(raw)
        self.session_id = "s"
        self.requests = 0

    def update_activity(self):
        pass

    def _acquire_request(self):
        self.requests += 1

    def _release_request(self):
        self.requests -= 1

    def _call_backend(self, fn, timeout=None):
        assert timeout and timeout > 0
        return fn()


def test_session_attempt_sends_json_and_lets_the_backend_choose_parallelism():
    raw = _Raw({"fallback": None, "prefix": PREFIX_OK,
                "candidates": [_cand([_cmd(1, "by")])]})
    s = _MixinSession(raw)
    out = s.attempt(7, ['by "a\\tb"'], 30)
    line, cands, timeout_s, parallel = raw.calls[0]
    assert (line, json.loads(cands), timeout_s, parallel) == (7, ['by "a\\tb"'], 30, 0)
    assert out["candidates"][0]["report"]["success"] is True and s.requests == 0
    assert s.cancel_attempts("memory") == 3 and raw.calls[-1] == ("cancel", "memory")


def test_cancel_attempts_never_raises():
    class Broken:
        def cancel_attempts(self, reason):
            raise RuntimeError("gateway gone")

    assert _MixinSession(Broken()).cancel_attempts("memory") == 0


# ---------------------------------------------------------------- the route

class _Session:
    def __init__(self, answer):
        self.session_id = uuid.UUID(SID)
        self.field, self.leased, self.in_use, self.ml_pid = "HOL", True, False, None
        self.answer = answer
        self.calls = []

    def update_activity(self):
        pass

    def attempt(self, line, candidates, timeout_s):
        self.calls.append((line, candidates, timeout_s))
        return self.answer

    def prover_alive(self):
        return None

    def check_prover_dead(self):
        return False


class _Manager:
    memory_management_enabled = False

    def __init__(self, session):
        self.session = session

    def get_session(self, session_id, lease_id=None, require_lease=True):
        return self.session

    def discard_dead(self, sid, reason="prover_died"):
        return True


@contextlib.asynccontextmanager
async def _no_lifespan(_app):
    yield


@pytest.fixture()
def api():
    saved = app.router.lifespan_context
    app.router.lifespan_context = _no_lifespan

    def _make(session):
        app.dependency_overrides[get_session_manager] = lambda: _Manager(session)
        return TestClient(app, raise_server_exceptions=False)

    yield _make
    app.dependency_overrides.clear()
    app.router.lifespan_context = saved


ANSWER = {"fallback": None, "host_line": 4, "candidates": [{
    "candidate": "apply simp", "fallback": None, "elapsed_s": 0.5, "cancelled": None,
    "report": {"success": True, "timed_out": False, "proof_open": True, "pending_qed": False,
               "used_sorry": False, "sorry_lines": [], "commands": [], "subgoals": ["1. A"]}}]}


def test_route_runs_the_candidates_with_the_default_limit(api):
    session = _Session(ANSWER)
    with api(session) as client:
        r = client.post(f"/api/v1/sessions/{SID}/attempt", headers=LEASE,
                        json={"line": 5, "candidates": ["apply simp"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert session.calls == [(5, ["apply simp"], 30)]
    assert body["timeout_s"] == 30 and body["timeout_capped"] is False and body["note"] is None
    assert body["candidates"][0]["report"]["subgoals"] == ["1. A"]


def test_route_caps_the_limit_and_says_so(api):
    session = _Session(ANSWER)
    with api(session) as client:
        r = client.post(f"/api/v1/sessions/{SID}/attempt", headers=LEASE,
                        json={"line": 5, "candidates": ["apply simp"], "timeout_s": 900})
    body = r.json()
    assert session.calls[0][2] == 300
    assert body["timeout_s"] == 300 and body["timeout_capped"] is True and "300" in body["note"]


def test_route_refuses_ml_in_a_candidate(api):
    session = _Session(ANSWER)
    with api(session) as client:
        r = client.post(f"/api/v1/sessions/{SID}/attempt", headers=LEASE,
                        json={"line": 5, "candidates": ['ML \\<open>OS.Process.system "ls"\\<close>']})
    assert r.status_code == 422 and session.calls == []

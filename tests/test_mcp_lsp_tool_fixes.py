"""Tool fixes of the LSP MCP (isabelle_multi_attempt, isabelle_diagnostic_messages) and of the
goals route, without a backend: a fake client returns the reports the Scala side produces.

The Scala side of the same fixes (line resolution of isabelle_goal, `sorry_lines`, the output
of a transient query) is covered against a live server in tests/test_tool_fixes_live.py."""
from __future__ import annotations

import asyncio
import json

import pytest

from mcp_servers.lsp.config import Config
from tests.test_mcp_lsp_server import FakeClient

# 1 theory ... begin / 2 lemma a / 3 sorry-free filler / 4 lemma b / 5 end   (file: 5 lines)
TEXT = "theory Fan imports Main begin\nlemma a: True\n  by simp\nlemma b: True\n  by simp\nend\n"
N_LINES = len(TEXT.splitlines())  # 6


class GoalFakeClient(FakeClient):
    """FakeClient that also answers get_proof_state."""

    def __init__(self):
        super().__init__()
        self.state = {"subgoals": ["1. A \\<Longrightarrow> A"], "proof_finished": False}
        self.state_calls = 0

    async def get_proof_state(self, session_id, lease_id=None):
        self.state_calls += 1
        return self.state


def _fresh(monkeypatch):
    pytest.importorskip("mcp")
    from mcp_servers.lsp import app as lsp_app
    from mcp_servers.lsp import server as lsp_server

    monkeypatch.setattr(Config, "SCRATCH_SPARE", 0)
    pool = lsp_server.pool
    fake = GoalFakeClient()
    for name, value in [("_bindings", {}), ("_scratch", {}), ("_scratch_counts", {}),
                        ("_spare_pending", {}), ("_client", fake)]:
        monkeypatch.setattr(pool, name, value)
    return lsp_app, fake


def _file(tmp_path):
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)
    return f


def _attempt(lsp_app, f, fake, report, line=4, candidates=("lemma c: True\n  by simp",)):
    """open (the file's own load gets an ok report), then one multi_attempt whose scratch load
    answers `report`."""
    fake.next_reports = [{"success": True, "commands": []}, report]

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        return json.loads(await lsp_app.isabelle_multi_attempt(
            file_path=str(f), line=line, candidates=list(candidates)))

    return asyncio.run(run())


def _failed_cmd(node_line, messages, status="failed", kind="apply"):
    return {"i": 0, "line": node_line, "node_line": node_line, "kind": kind,
            "status": status, "messages": messages}


# ------------------------------------------------------------ isabelle_multi_attempt


def test_failure_messages_are_complete(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    long_error = "Failed to apply proof method:\ngoal (1 subgoal):\n 1. " + "x ∧ " * 200 + "False"
    assert len(long_error) > 600
    report = {"success": False, "proof_open": False, "pending_qed": False, "timed_out": False,
              "commands": [_failed_cmd(4, [
                  {"sev": "warning", "text": "Needed to apply full-blown clarsimp"},
                  {"sev": "error", "text": long_error}])]}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    (entry,) = cand["failed"]
    assert entry["messages"] == long_error  # not cut, and without the warning
    assert entry["candidate_line"] == 1


def test_open_proof_returns_the_remaining_goals(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": True, "proof_open": True, "pending_qed": False, "timed_out": False,
              "commands": []}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert cand["proof_open"] is True
    assert cand["subgoals"] == ["1. A \\<Longrightarrow> A"]
    assert fake.state_calls == 1


def test_subgoals_are_lines_and_the_result_is_indented_readable_json(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    fake.state = {"subgoals": ["1. \\<And>x. P x \\<Longrightarrow>\n      Q x", "2. R"],
                  "proof_finished": False}
    report = {"success": True, "proof_open": True, "pending_qed": False, "timed_out": False,
              "commands": []}
    f = _file(tmp_path)
    fake.next_reports = [{"success": True, "commands": []}, report]

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        return await lsp_app.isabelle_multi_attempt(
            file_path=str(f), line=4, candidates=["lemma c: True\n  by simp"])

    raw = asyncio.run(run())
    assert raw.count("\n") > 10  # one field or line per row, not one compact line
    assert '"      Q x"' in raw  # a goal's continuation line is a row of its own
    assert json.loads(raw)[0]["subgoals"] == [
        "1. \\<And>x. P x \\<Longrightarrow>", "      Q x", "2. R"]


def test_unicode_in_messages_is_not_escaped(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": False, "proof_open": False, "pending_qed": False, "commands": [
        _failed_cmd(4, [{"sev": "error", "text": "goal: A \u2227 B"}])]}
    f = _file(tmp_path)
    fake.next_reports = [{"success": True, "commands": []}, report]

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        return await lsp_app.isabelle_multi_attempt(
            file_path=str(f), line=4, candidates=["lemma c: True\n  by simp"])

    raw = asyncio.run(run())
    assert "A \u2227 B" in raw and "\\u2227" not in raw


# --------------------------------------------------------------- the verdict


def _verdict_of(lsp_app, fake, tmp_path, report, candidates=("lemma c: True\n  by simp",)):
    return [c["verdict"] for c in _attempt(lsp_app, _file(tmp_path), fake, report,
                                           candidates=candidates)]


def test_verdict_ok_for_a_clean_candidate_with_or_without_open_goals(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    clean = {"success": True, "proof_open": False, "pending_qed": False, "commands": []}
    assert _verdict_of(lsp_app, fake, tmp_path, clean) == ["ok"]
    opened = {"success": True, "proof_open": True, "pending_qed": False, "commands": []}
    assert _verdict_of(lsp_app, fake, tmp_path, opened) == ["ok"]


def test_verdict_failed_when_the_candidate_has_an_error(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": False, "proof_open": False, "pending_qed": False,
              "commands": [_failed_cmd(4, [{"sev": "error", "text": "e"}])]}
    assert _verdict_of(lsp_app, fake, tmp_path, report) == ["failed"]


def test_verdict_failed_when_the_check_fails_without_a_failed_command(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": False, "proof_open": False, "pending_qed": False, "commands": []}
    assert _verdict_of(lsp_app, fake, tmp_path, report) == ["failed"]


def test_verdict_prefix_failed_is_not_a_pass_although_failed_is_empty(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": False, "proof_open": False, "pending_qed": False,
              "commands": [_failed_cmd(2, [{"sev": "error", "text": "prefix broke"}], kind="lemma")]}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert cand["failed"] == [] and cand["success"] is False  # what the old pass rule read as ok
    assert cand["verdict"] == "prefix_failed"


def test_verdict_prefix_failed_wins_over_the_candidates_own_error(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": False, "proof_open": False, "pending_qed": False, "commands": [
        _failed_cmd(2, [{"sev": "error", "text": "p"}], kind="lemma"),
        _failed_cmd(5, [{"sev": "error", "text": "c"}], kind="by")]}
    assert _verdict_of(lsp_app, fake, tmp_path, report) == ["prefix_failed"]


def test_verdict_timed_out(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": False, "proof_open": False, "pending_qed": False, "timed_out": True,
              "commands": [_failed_cmd(5, [], status="running", kind="by")]}
    assert _verdict_of(lsp_app, fake, tmp_path, report) == ["timed_out"]


def test_verdict_used_sorry_only_for_the_candidates_own_sorry(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    own = {"success": True, "proof_open": False, "pending_qed": False, "sorry_lines": [5],
           "commands": []}
    assert _verdict_of(lsp_app, fake, tmp_path, own) == ["used_sorry"]
    prefix = {"success": True, "proof_open": False, "pending_qed": False, "sorry_lines": [3],
              "commands": []}
    assert _verdict_of(lsp_app, fake, tmp_path, prefix) == ["ok"]


def test_verdict_error_when_the_candidate_cannot_run(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("scratch session died")

    f = _file(tmp_path)

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        monkeypatch.setattr(fake, "load_document", boom)
        return json.loads(await lsp_app.isabelle_multi_attempt(
            file_path=str(f), line=4, candidates=["by simp"]))

    (cand,) = asyncio.run(run())
    assert cand["verdict"] == "error" and "scratch session died" in cand["error"]


def test_tool_descriptions_state_line_and_verdict(monkeypatch):
    pytest.importorskip("mcp")
    from mcp_servers.lsp import app as lsp_app

    doc = lsp_app.isabelle_multi_attempt.__doc__
    assert "REPLACES" in doc and "every line after it" in doc
    assert "verdict" in doc and "prefix_failed" in doc
    assert "qed" in lsp_app.isabelle_goal.__doc__ and "sorry" in lsp_app.isabelle_goal.__doc__
    assert "thm_oracles" in lsp_app.isabelle_query.__doc__


def test_pending_qed_returns_the_goals_too(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    fake.state = {"subgoals": [], "proof_finished": True}
    report = {"success": True, "proof_open": True, "pending_qed": True, "timed_out": False,
              "commands": []}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert cand["pending_qed"] is True and cand["subgoals"] == []


def test_closed_or_failed_candidate_has_no_goal_probe(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": True, "proof_open": False, "pending_qed": False, "commands": []}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert "subgoals" not in cand and fake.state_calls == 0


def test_used_sorry_is_the_candidates_own(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    # sorry at file line 3 (before the candidate at line 4): prefix only
    report = {"success": True, "proof_open": False, "pending_qed": False, "used_sorry": True,
              "sorry_lines": [3], "commands": []}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert cand["used_sorry"] is False and cand["prefix_used_sorry"] is True


def test_candidate_sorry_is_reported_and_prefix_key_absent(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": True, "proof_open": False, "pending_qed": False, "used_sorry": True,
              "sorry_lines": [5], "commands": []}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert cand["used_sorry"] is True and "prefix_used_sorry" not in cand


def test_both_sorry_kinds_at_once(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": True, "proof_open": False, "pending_qed": False,
              "sorry_lines": [3, 4, 7], "commands": []}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert cand["used_sorry"] is True and cand["prefix_used_sorry"] is True


def test_failed_command_before_the_candidate_is_a_prefix_failure(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": False, "proof_open": False, "pending_qed": False, "commands": [
        _failed_cmd(2, [{"sev": "error", "text": "prefix broke"}], kind="lemma"),
        _failed_cmd(5, [{"sev": "error", "text": "candidate broke"}], kind="by")]}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert [e["candidate_line"] for e in cand["failed"]] == [2]  # file line 5, candidate at 4
    assert cand["failed"][0]["messages"] == "candidate broke"
    (pre,) = cand["prefix_failed"]
    assert pre["line"] == 2 and pre["messages"] == "prefix broke"


def test_candidate_lines_are_never_below_one(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    report = {"success": False, "proof_open": False, "pending_qed": False, "commands": [
        _failed_cmd(1, [{"sev": "error", "text": "e"}]), _failed_cmd(4, [{"sev": "error", "text": "e"}]),
        _failed_cmd(6, [{"sev": "error", "text": "e"}])]}
    (cand,) = _attempt(lsp_app, _file(tmp_path), fake, report)
    assert all(e["candidate_line"] >= 1 for e in cand["failed"])


@pytest.mark.parametrize("line", [0, -3, N_LINES + 2, 1000])
def test_line_out_of_range_is_an_error(tmp_path, monkeypatch, line):
    lsp_app, fake = _fresh(monkeypatch)
    f = _file(tmp_path)

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        loads = len(fake.loads)
        out = json.loads(await lsp_app.isabelle_multi_attempt(
            file_path=str(f), line=line, candidates=["lemma c: True\n  by simp"]))
        return out, len(fake.loads) - loads

    out, extra_loads = asyncio.run(run())
    assert out["error"] == "line_out_of_range"
    assert f"between 1 and {N_LINES + 1}" in out["detail"] and str(line) in out["detail"]
    assert extra_loads == 0  # no scratch session was used


@pytest.mark.parametrize("line", [1, 4, N_LINES, N_LINES + 1])
def test_line_in_range_runs_and_the_last_value_appends(tmp_path, monkeypatch, line):
    lsp_app, fake = _fresh(monkeypatch)
    f = _file(tmp_path)
    report = {"success": True, "proof_open": False, "pending_qed": False, "commands": []}
    out = _attempt(lsp_app, f, fake, report, line=line)
    assert "error" not in out[0]
    scratch_text = fake.loads[-1]["text"]
    expected_prefix = "\n".join(TEXT.splitlines()[: line - 1]) + "\n"
    assert scratch_text == expected_prefix + "lemma c: True\n  by simp"
    if line == N_LINES + 1:
        assert scratch_text.startswith(TEXT.rstrip("\n"))  # the whole file, then the candidate


# ------------------------------------------------- isabelle_diagnostic_messages


def _diag(lsp_app, f, fake, report):
    fake.next_reports = [report]

    async def run():
        return json.loads(await lsp_app.isabelle_diagnostic_messages(file_path=str(f)))

    return asyncio.run(run())


def test_diagnostics_used_sorry_covers_the_whole_file(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    # an incremental check of the tail: used_sorry (tail only) is false, the file has a sorry
    out = _diag(lsp_app, _file(tmp_path), fake,
                {"success": True, "proof_open": False, "used_sorry": False, "sorry_lines": [3],
                 "commands": []})
    assert out["used_sorry"] is True


def test_diagnostics_used_sorry_false_without_executed_sorry(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    # the backend's tail flag says true (a sorry text in the tail), none was executed
    out = _diag(lsp_app, _file(tmp_path), fake,
                {"success": False, "proof_open": False, "used_sorry": True, "sorry_lines": [],
                 "commands": []})
    assert out["used_sorry"] is False


def test_diagnostics_used_sorry_falls_back_for_an_older_backend(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    out = _diag(lsp_app, _file(tmp_path), fake,
                {"success": True, "proof_open": False, "used_sorry": True, "commands": []})
    assert out["used_sorry"] is True


# ------------------------------------------------------------- isabelle_goal route


class _FakeGoalSession:
    def __init__(self, result):
        self.result = result

    def goals_at_line(self, line):
        return self.result


def _goals(result, show_states, monkeypatch):
    from server.app.api.v1.routes import positional
    from server.app.core.config import Server

    monkeypatch.setattr(Server, "SHOW_STATES", show_states)
    return asyncio.run(positional.goals_at_line(_FakeGoalSession(result), 3))


def test_goals_route_says_when_states_are_not_recorded(monkeypatch):
    from server.app.api.v1.routes.positional import SHOW_STATES_OFF_ERROR

    out = _goals({"found": True, "goals_before": [], "goals_after": []}, False, monkeypatch)
    assert out.error == SHOW_STATES_OFF_ERROR and "ISABELLE_SHOW_STATES" in out.error
    assert out.found is True and out.goals_before == [] and out.goals_after == []


def test_goals_route_has_no_error_with_states_on(monkeypatch):
    out = _goals({"found": True, "goals_before": ["proof (prove)"], "goals_after": []}, True, monkeypatch)
    assert out.error is None and out.goals_before == ["proof (prove)"]


def test_goals_route_keeps_a_backend_error(monkeypatch):
    out = _goals({"found": False, "error": "no command at line 3"}, False, monkeypatch)
    assert out.error == "no command at line 3"

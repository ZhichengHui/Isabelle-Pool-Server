"""isabelle_multi_attempt in the file's own session (efficiency plan stage C), MCP side: the
server's attempt answer -> the tool's per-candidate entries, the scratch-session fallbacks,
cancelled_memory, the timeout note, and the same verdicts and goals as the scratch-session
implementation for the cases recorded live (tests/data/attempt_semantics.json). No backend."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.app.services.session_attempt import attempt_reports
from tests.test_mcp_lsp_server import FakeClient

TEXT = "theory Fan imports Main begin\nlemma a: True\n  by simp\nlemma b: True\n  by simp\nend\n"
DATA = Path(__file__).parent / "data" / "attempt_semantics.json"


class GoalFakeClient(FakeClient):
    async def get_proof_state(self, session_id, lease_id=None):
        return {"subgoals": ["1. True"], "proof_finished": False}


def _fresh(monkeypatch):
    pytest.importorskip("mcp")
    from mcp_servers.lsp import app as lsp_app
    from mcp_servers.lsp import server as lsp_server

    pool = lsp_server.pool
    fake = GoalFakeClient()
    for name, value in [("_bindings", {}), ("_scratch", {}), ("_scratch_counts", {}),
                        ("_client", fake)]:
        monkeypatch.setattr(pool, name, value)
    return lsp_app, fake


def _run(lsp_app, f, line, candidates, timeout=None):
    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        return json.loads(await lsp_app.isabelle_multi_attempt(
            file_path=str(f), line=line, candidates=list(candidates), timeout=timeout))
    return asyncio.run(run())


def _answer(line, candidates, raw):
    """The route's answer for a backend answer `raw` (what the server sends the MCP)."""
    ans = attempt_reports(raw, line, list(candidates))
    ans.setdefault("timeout_s", 30)
    return ans


def _cand(commands, **kw):
    base = {"finished": True, "timed_out": False, "in_proof": False, "goals": [],
            "elapsed_ms": 800, "cancelled": None, "error": None}
    base.update(kw)
    return {"commands": commands, **base}


PREFIX_OK = {"all_ok": True, "settled": True, "failed": [], "sorry_lines": []}


def test_candidates_run_in_the_file_session_not_on_scratch_sessions(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)
    cands = ["lemma c: True\n  by simp", "lemma c: True\n  apply (rule TrueI)\n  sorry"]
    raw = {"fallback": None, "host_line": 3, "prefix": PREFIX_OK, "candidates": [
        _cand([{"line": 1, "kind": "lemma", "status": "ok", "sorry": False, "messages": ""},
               {"line": 2, "kind": "by", "status": "ok", "sorry": False, "messages": ""}]),
        _cand([{"line": 1, "kind": "lemma", "status": "ok", "sorry": False, "messages": ""},
               {"line": 2, "kind": "apply", "status": "ok", "sorry": False, "messages": ""},
               {"line": 3, "kind": "sorry", "status": "ok", "sorry": True, "messages": ""}],
              elapsed_ms=1500)]}
    fake.attempt_answers = [_answer(4, cands, raw)]
    out = _run(lsp_app, f, 4, cands)
    assert [r["verdict"] for r in out] == ["ok", "used_sorry"]
    assert [r["elapsed_s"] for r in out] == [0.8, 1.5]
    assert all(r["cancelled_memory"] is False and "checked_on" not in r for r in out)
    assert fake.attempts[0]["candidates"] == cands and fake.attempts[0]["timeout_s"] == 30
    assert len(fake.loads) == 1  # only the file's own load: no scratch session was used
    assert fake.acquired and len(fake.acquired) == 1


def test_a_candidate_with_end_goes_to_a_scratch_session(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)
    cands = ["lemma c: True\n  by simp", "lemma c: True\n  by simp\nend"]
    raw = {"fallback": None, "host_line": 3, "prefix": PREFIX_OK, "candidates": [
        _cand([{"line": 1, "kind": "lemma", "status": "ok", "sorry": False, "messages": ""},
               {"line": 2, "kind": "by", "status": "ok", "sorry": False, "messages": ""}]),
        {"index": 1, "fallback": "candidate_command", "command": "end"}]}
    fake.attempt_answers = [_answer(4, cands, raw)]
    out = _run(lsp_app, f, 4, cands)
    assert out[0]["verdict"] == "ok" and "checked_on" not in out[0]
    assert out[1]["verdict"] == "ok"
    assert out[1]["checked_on"] == "scratch session (candidate_command: end)"
    scratch = [x for x in fake.loads[1:]]
    assert len(scratch) == 1 and scratch[0]["text"].endswith("lemma c: True\n  by simp\nend")
    assert scratch[0]["text"].startswith("theory Fan imports Main begin\nlemma a: True\n  by simp\n")


@pytest.mark.parametrize("reason", ["multiline_command", "no_host", "host_end"])
def test_whole_call_fallbacks_use_scratch_sessions(tmp_path, monkeypatch, reason):
    lsp_app, fake = _fresh(monkeypatch)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)
    fake.attempt_answers = [{"fallback": reason, "candidates": [], "timeout_s": 30}]
    out = _run(lsp_app, f, 3, ["  by auto"])
    assert out[0]["checked_on"] == f"scratch session ({reason})"
    assert len(fake.loads) == 2  # the file + one scratch load
    assert fake.loads[1]["text"] == "theory Fan imports Main begin\nlemma a: True\n  by auto"


def test_cancelled_by_memory_protection(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)
    raw = {"fallback": None, "host_line": 3, "prefix": PREFIX_OK, "candidates": [
        _cand([{"line": 1, "kind": "lemma", "status": "ok", "sorry": False, "messages": ""},
               {"line": 2, "kind": "by", "status": "running", "sorry": False, "messages": ""}],
              finished=False, timed_out=True, cancelled="memory")]}
    fake.attempt_answers = [_answer(4, ["lemma c: True\n  by auto"], raw)]
    out = _run(lsp_app, f, 4, ["lemma c: True\n  by auto"])
    assert out[0]["cancelled_memory"] is True and out[0]["verdict"] == "error"
    assert "memory" in out[0]["error"]


def test_a_capped_timeout_is_noted(tmp_path, monkeypatch):
    lsp_app, fake = _fresh(monkeypatch)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)
    raw = {"fallback": None, "host_line": 3, "prefix": PREFIX_OK, "candidates": [
        _cand([{"line": 1, "kind": "by", "status": "ok", "sorry": False, "messages": ""}])]}
    ans = _answer(4, ["  by simp"], raw)
    ans.update(timeout_s=300, timeout_capped=True,
               note="timeout 1000.0 s is above the server cap; the candidates ran with 300 s")
    fake.attempt_answers = [ans]
    out = _run(lsp_app, f, 4, ["  by simp"], timeout=1000)
    assert fake.attempts[0]["timeout_s"] == 1000
    assert "300" in out[0]["timeout_note"]


# ------------------------------------- same verdicts and goals as the scratch implementation

def _cases():
    if not DATA.exists():
        return []
    return json.loads(DATA.read_text())["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_same_verdict_and_goals_as_the_scratch_implementation(tmp_path, monkeypatch, case):
    """Recorded live (claude-work/2026-11-24-impl-search-in-session/semantics.py) on the same
    file: `old` is what the scratch-session implementation (main) returned, `answer` what the
    new server answered. The MCP must turn `answer` into the same verdicts and goals."""
    lsp_app, fake = _fresh(monkeypatch)
    f = tmp_path / case["file_name"]
    f.write_text(case["text"])
    fake.attempt_answers = [case["answer"]]
    out = _run(lsp_app, f, case["line"], case["candidates"], timeout=case.get("timeout"))
    got = [(r["verdict"], r.get("subgoals")) for r in out]
    want = [(r["verdict"], r.get("subgoals")) for r in case["old"]]
    if case["answer"].get("fallback"):
        # the fallback reruns the scratch path on the fake, which answers its default report
        assert all(r["checked_on"].startswith("scratch session") for r in out)
    else:
        assert got == want

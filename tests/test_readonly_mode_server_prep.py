"""Tests for the read-only-mode server preparation
(LSP read-only mode research, internal working notes; plan: server prep for dual MCP support).

Covers the pieces testable without a running Isabelle backend:
- DocumentLoadRequest schema validation (empty text, imports without thy_name)
- Session.load_document: backend reset, theory-name derivation, bookkeeping reset
- Session.verify_chunk retains last_chunk_report; load_document clears it
- Per-command `range` in chunk reports: backend-JSON pass-through and the
  router's defensive CommandStatus mapping (present / absent / malformed)
- goals_at_line / command_at_line read-only line queries: Session-level parsing
  and pass-through of the backend JSON, response-model defaults
"""
from __future__ import annotations

import concurrent.futures
import uuid

import pytest
from pydantic import ValidationError

from server.app.api.v1.serializers import parse_command_range as _parse_command_range
from server.app.api.v1.serializers import to_ascii as _ascii
from server.app.core.config import Server
from server.app.api.v1.schemas.API_models import (
    CommandAtLineResponse,
    CommandRange,
    CommandStatus,
    DefinitionResponse,
    DefinitionTarget,
    DocumentLoadRequest,
    GoalsResponse,
    HoverResponse,
    LocatedCommand,
    Position,
    SledgehammerAtRequest,
)
from server.app.errors import SessionError
from server.app.services.session import _Isabelle_Session


class _FakeRaw:
    """Minimal stand-in for the Py4J ReplBackend proxy."""

    def __init__(self):
        self.reset_calls = 0
        self.entered = []
        self.steps = []
        self.probe_states = []

    def reset(self):
        self.reset_calls += 1
        return None

    def enter_thy(self, name):
        self.entered.append(name)
        return None

    def step(self, text, budget_ms=None):
        self.steps.append(text)
        return None  # success checkers degrade None -> no error output

    def verify_chunk(self, chunk, budget_ms):
        return (
            '{"timed_out": false, "success": true, "proof_open": false, '
            '"pending_qed": false, "used_sorry": false, "elapsed_ms": 1, '
            '"commands": [{"i": 0, "line": 1, "kind": "theorem", "status": "ok", '
            '"range": {"start": {"line": 1, "col": 1}, "end": {"line": 1, "col": 20}}, '
            '"messages": []}]}'
        )

    def step_chunk_report(self, text, budget_ms, probe_state):
        self.probe_states.append(probe_state)
        return (
            '{"timed_out": false, "success": false, "proof_open": false, '
            '"pending_qed": false, "used_sorry": false, "elapsed_ms": 1, '
            '"commands": ['
            '{"i": 0, "line": 1, "kind": "theory", "status": "ok", "messages": []}, '
            '{"i": 1, "line": 2, "kind": "lemma", "status": "failed", "messages": '
            '[{"sev": "error", "text": "Failed to finish proof"}]}'
            "]}"
        )

    def goals_at_line(self, line):
        return (
            '{"found": true, "command": {"kind": "apply", "source": "apply simp", '
            '"range": {"start": {"line": 3, "col": 1}, "end": {"line": 3, "col": 11}}}, '
            '"goals_before": ["A \u2227 B"], "goals_after": ["B \u2227 A"]}'
        )

    def hover_at(self, line, col):
        return (
            '{"found": true, '
            '"range": {"start": {"line": 2, "col": 15}, "end": {"line": 2, "col": 17}}, '
            '"contents": ["constant \\"List.list.hd\\"", ":: nat list \u21d2 nat"]}'
        )

    def definition_at(self, line, col):
        return (
            '{"found": true, "targets": ['
            '{"kind": "file", "file": "/opt/isabelle/src/HOL/List.thy", "line": 13, '
            '"col": 1, "end_line": 13, "end_col": 3}, '
            '{"kind": "node", "node": "Draft.HoverU", "start_line": 2, "end_line": 2}]}'
        )

    def sledgehammer_at(self, line, subgoal, timeout_s):
        return (
            '{"found": true, "results": ["simp found a proof...", '
            '"simp: Try this: by simp (1 ms)"]}'
        )


class _FakeBackend:
    """Executes submitted jobs inline, like a healthy ThreadedBackend."""

    def __init__(self):
        self.raw = _FakeRaw()

    def submit(self, fn):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except Exception as e:  # pragma: no cover - defensive
            fut.set_exception(e)
        return fut


def _make_session() -> _Isabelle_Session:
    return _Isabelle_Session(
        session_id=uuid.uuid4(),
        session_theories=[],
        session_field="HOL",
        backend=_FakeBackend(),
    )


# ------------------------------------------------------- schema validation


def test_document_load_rejects_empty_text():
    with pytest.raises(ValidationError):
        DocumentLoadRequest(text="")


def test_document_load_requires_thy_name_with_imports():
    with pytest.raises(ValidationError):
        DocumentLoadRequest(text="lemma True by simp", imports=["Main"])
    # ... and accepts it when thy_name is given
    req = DocumentLoadRequest(
        text="lemma True by simp", imports=["Main"], thy_name="Scratch"
    )
    assert req.thy_name == "Scratch"


def test_document_load_full_file_mode_ok():
    req = DocumentLoadRequest(text="theory Foo imports Main begin\nend")
    assert req.thy_name is None and req.imports is None


# ------------------------------------------------------- load_document


def test_load_document_resets_backend_and_derives_theory_name():
    session = _make_session()
    result = session.load_document(
        "theory Foo imports Main begin\nlemma True by simp\nend"
    )
    assert session.backend.raw.reset_calls == 0  # pristine backend: no reset (session_reset.py)
    assert session.backend.raw.entered == ["Foo"]
    assert session.entered_thy == "Foo"
    assert result.success
    assert session.command_history[-1]["type"] == "document_load"


def test_load_document_clears_bookkeeping():
    session = _make_session()
    session.command_history.append({"type": "small_step"})
    session.checkpoints[1] = 0.0
    session.verified_theories.append("Old")
    session.last_chunk_report = {"report": {}}
    session.load_document("theory Foo imports Main begin\nend", timeout=5.0)
    assert session.checkpoints == {}
    assert session.verified_theories == []
    assert session.last_chunk_report is None
    # history contains only the document_load marker
    assert [h["type"] for h in session.command_history] == ["document_load"]


def test_load_document_without_header_or_name_errors():
    session = _make_session()
    with pytest.raises(SessionError):
        session.load_document("lemma True by simp")


def test_load_document_with_imports_builds_header_server_side():
    session = _make_session()
    result = session.load_document(
        "lemma True by simp", thy_name="Scratch", imports=["Main"]
    )
    assert result.success
    assert session.entered_thy == "Scratch"
    # header stepped first, then the body
    assert session.backend.raw.steps[0].startswith("theory Scratch imports Main begin")
    assert session.backend.raw.steps[1] == "lemma True by simp"


# ------------------------------------------------------- last_chunk_report


def test_verify_chunk_retains_last_report():
    session = _make_session()
    session.entered_thy = "Foo"
    out = session.verify_chunk("lemma True by simp", timeout=5.0)
    assert session.last_chunk_report is not None
    assert session.last_chunk_report["report"]["success"] is True
    assert session.last_chunk_report["execution_time"] == out["execution_time"]
    assert session.last_chunk_report["timestamp"] > 0


def test_load_document_clears_last_report():
    session = _make_session()
    session.verify_chunk("lemma True by simp", timeout=5.0)
    assert session.last_chunk_report is not None
    session.load_document("theory Foo imports Main begin\nend")
    assert session.last_chunk_report is None


# ------------------------------------------------------- load_document report=True


def test_load_document_report_stores_last_chunk_report():
    session = _make_session()
    result = session.load_document(
        "theory Foo imports Main begin\nlemma False by auto", report=True
    )
    assert not result.success
    assert result.error == "line 2: Failed to finish proof"
    assert session.last_chunk_report is not None
    assert session.last_chunk_report["report"]["success"] is False
    cmds = session.last_chunk_report["report"]["commands"]
    assert [c["status"] for c in cmds] == ["ok", "failed"]


def test_load_document_report_skips_state_probe_after_theory_end():
    session = _make_session()
    session.load_document("theory Foo imports Main begin\nend", report=True)
    # probe_state=False: probing past a trailing `end` would hang the ML channel
    assert session.backend.raw.probe_states == [False]


def test_load_document_report_probes_state_without_theory_end():
    session = _make_session()
    session.load_document("theory Foo imports Main begin\nlemma True by simp", report=True)
    assert session.backend.raw.probe_states == [True]


def test_load_document_without_report_leaves_last_report_none():
    session = _make_session()
    session.load_document("theory Foo imports Main begin\nlemma True by simp")
    assert session.last_chunk_report is None
    assert session.backend.raw.probe_states == []


# ------------------------------------------------------- per-command range


def test_verify_chunk_report_preserves_command_range():
    # The backend JSON's per-command `range` passes through json.loads untouched.
    session = _make_session()
    session.verify_chunk("lemma True by simp", timeout=5.0)
    cmd = session.last_chunk_report["report"]["commands"][0]
    assert cmd["range"] == {
        "start": {"line": 1, "col": 1},
        "end": {"line": 1, "col": 20},
    }


def test_command_range_mapped_when_present():
    rng = _parse_command_range(
        {"start": {"line": 3, "col": 1}, "end": {"line": 3, "col": 22}}
    )
    assert rng == CommandRange(
        start=Position(line=3, col=1), end=Position(line=3, col=22)
    )
    cmd = CommandStatus(index=1, line=3, kind="lemma", status="failed", range=rng)
    assert cmd.range.start.line == 3
    assert cmd.range.end.col == 22


def test_command_range_absent_or_malformed_tolerated():
    assert _parse_command_range(None) is None
    assert _parse_command_range("bogus") is None
    assert _parse_command_range({"start": {"line": 1, "col": 1}}) is None
    assert _parse_command_range({"start": {"line": "x", "col": 1},
                                 "end": {"line": 1, "col": 2}}) is None
    # CommandStatus itself defaults to range=None when the backend omits it.
    cmd = CommandStatus(index=0, line=1, kind="theory", status="ok")
    assert cmd.range is None


# ------------------------------------------------------- line-based read-only queries


def test_goals_at_line_parses_and_passes_through():
    session = _make_session()
    result = session.goals_at_line(3)
    assert result["found"] is True
    assert result["command"]["kind"] == "apply"
    assert result["command"]["range"]["start"] == {"line": 3, "col": 1}
    assert result["goals_before"] == ["A \u2227 B"]
    assert result["goals_after"] == ["B \u2227 A"]


def test_goals_at_line_tolerates_junk_backend_reply():
    session = _make_session()
    session.backend.raw.goals_at_line = lambda line: "not json"
    result = session.goals_at_line(3)
    assert result["found"] is False
    assert "error" in result


def test_line_query_response_schemas():
    rng = CommandRange(start=Position(line=3, col=1), end=Position(line=3, col=11))
    cmd = LocatedCommand(kind="apply", source="apply simp", range=rng)
    goals = GoalsResponse(
        found=True, command=cmd, goals_before=["A \u2227 B"], goals_after=["B \u2227 A"]
    )
    assert goals.command.range.end.col == 11
    # found:false replies carry only an error; everything else defaults out
    miss = CommandAtLineResponse(found=False, error="no command at line 99")
    assert miss.kind is None and miss.range is None
    goals_miss = GoalsResponse(found=False, error="no theory entered")
    assert goals_miss.command is None
    assert goals_miss.goals_before == [] and goals_miss.goals_after == []


# ------------------------------------------------------- hover / definition / sledgehammer_at


def test_hover_at_parses_and_passes_through():
    session = _make_session()
    result = session.hover_at(2, 15)
    assert result["found"] is True
    assert result["range"]["start"] == {"line": 2, "col": 15}
    assert result["contents"][0] == 'constant "List.list.hd"'


def test_definition_at_parses_and_passes_through():
    session = _make_session()
    result = session.definition_at(2, 15)
    assert result["found"] is True
    kinds = [t["kind"] for t in result["targets"]]
    assert kinds == ["file", "node"]
    assert result["targets"][0]["file"].endswith("List.thy")
    assert result["targets"][1]["start_line"] == 2


def test_sledgehammer_at_parses_and_passes_through():
    session = _make_session()
    result = session.sledgehammer_at(3, subgoal=1, timeout_s=10)
    assert result["found"] is True
    assert any("Try this" in r for r in result["results"])


def test_position_queries_tolerate_junk_backend_reply():
    session = _make_session()
    session.backend.raw.hover_at = lambda line, col: "not json"
    session.backend.raw.sledgehammer_at = lambda line, subgoal, timeout_s: None
    assert session.hover_at(2, 15)["found"] is False
    assert "error" in session.hover_at(2, 15)
    assert session.sledgehammer_at(3)["found"] is False


def test_position_query_schemas():
    h = HoverResponse(found=True, contents=['constant "List.list.hd"'])
    assert h.range is None and h.error is None
    t = DefinitionTarget(kind="file", file="/opt/isabelle/src/HOL/List.thy", line=13)
    d = DefinitionResponse(found=True, targets=[t])
    assert d.targets[0].col is None
    with pytest.raises(ValidationError):
        SledgehammerAtRequest(line=0)
    with pytest.raises(ValidationError):
        SledgehammerAtRequest(line=3, subgoal=0)
    ok = SledgehammerAtRequest(line=3)
    assert ok.subgoal == 1 and ok.timeout_s == 30


# --- _ascii: rendered-output normalisation (ISABELLE_ASCII_OUTPUT) -----------


def test_ascii_passthrough_when_disabled(monkeypatch):
    monkeypatch.setattr(Server, "ASCII_OUTPUT", False)
    raw = "1. A ⟹ B ⟹ A ∧ B"
    assert _ascii(raw) == raw


def test_ascii_converts_when_enabled(monkeypatch):
    monkeypatch.setattr(Server, "ASCII_OUTPUT", True)
    monkeypatch.setattr(
        "server.app.api.v1.serializers.normalise_for_isabelle",
        lambda t: t.replace("⟹", "\\<Longrightarrow>").replace("∧", "\\<and>"),
    )
    assert _ascii("1. A ⟹ B ⟹ A ∧ B") == "1. A \\<Longrightarrow> B \\<Longrightarrow> A \\<and> B"
    assert _ascii(None) is None


def test_ascii_falls_back_to_raw_without_symbol_table(monkeypatch):
    monkeypatch.setattr(Server, "ASCII_OUTPUT", True)

    def _no_table(text):
        raise FileNotFoundError("no symbols file")

    monkeypatch.setattr("server.app.api.v1.serializers.normalise_for_isabelle", _no_table)
    raw = "1. A ⟹ B"
    assert _ascii(raw) == raw

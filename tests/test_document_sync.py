"""Tests for the incremental document-sync fast path of load_document
(Phase B1: spliff-diffed PIDE replace edit instead of reset + re-elaboration).

Covers the Python-side gate and bookkeeping without a running Isabelle
backend (fake Py4J proxy, same convention as test_readonly_mode_server_prep):
- first load / report=False / imports mode always take the reset path
- a same-theory full-file re-load with report=True takes the sync path
- theory-name change in the header falls back to the reset path (gate)
- the backend's "fallback" marker falls back to the reset path (wire contract)
- sync invalidates checkpoints and updates _loaded_text / last_chunk_report
- sync report error extraction uses the backend's (absolute) line numbers
"""
from __future__ import annotations

import concurrent.futures
import uuid

from server.app.services.session import _Isabelle_Session


_DOC_V1 = "theory Foo imports Main begin\nlemma True by simp\nend"
_DOC_V2 = "theory Foo imports Main begin\nlemma True by simp\nlemma True by simp\nend"
_DOC_OTHER_THEORY = "theory Bar imports Main begin\nend"

_OK_REPORT = (
    '{"timed_out": false, "success": true, "proof_open": false, '
    '"pending_qed": false, "used_sorry": false, "elapsed_ms": 1, '
    '"commands": [{"i": 0, "line": 1, "kind": "theory", "status": "ok", '
    '"messages": []}]}'
)

_SYNC_OK_REPORT = (
    '{"timed_out": false, "success": true, "proof_open": false, '
    '"pending_qed": false, "used_sorry": false, "elapsed_ms": 1, '
    '"line_semantics": "absolute", '
    '"commands": [{"i": 3, "line": 3, "node_line": 3, "kind": "lemma", '
    '"status": "ok", "messages": []}]}'
)

_SYNC_FAILED_REPORT = (
    '{"timed_out": false, "success": false, "proof_open": false, '
    '"pending_qed": false, "used_sorry": false, "elapsed_ms": 1, '
    '"line_semantics": "absolute", '
    '"commands": [{"i": 3, "line": 3, "node_line": 3, "kind": "lemma", '
    '"status": "failed", "messages": '
    '[{"sev": "error", "text": "Failed to finish proof"}]}]}'
)

_SYNC_FALLBACK_REPORT = (
    '{"fallback": "header_changed", "timed_out": false, "success": false, '
    '"proof_open": false, "pending_qed": false, "used_sorry": false, '
    '"elapsed_ms": 0, "commands": []}'
)


class _FakeRaw:
    """Minimal stand-in for the Py4J ReplBackend proxy."""

    def __init__(self):
        self.reset_calls = 0
        self.entered = []
        self.steps = []
        self.step_probe_states = []
        self.sync_calls = []
        self.sync_reply = _SYNC_OK_REPORT

    def reset(self):
        self.reset_calls += 1
        return None

    def enter_thy(self, name):
        self.entered.append(name)
        return None

    def step(self, text, budget_ms=None):
        self.steps.append(text)
        return None  # success checkers degrade None -> no error output

    def step_chunk_report(self, text, budget_ms, probe_state):
        self.step_probe_states.append(probe_state)
        return _OK_REPORT

    def sync_document(self, text, budget_ms, probe_state):
        self.sync_calls.append((text, budget_ms, probe_state))
        return self.sync_reply


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


# ------------------------------------------------------- path selection


def test_first_load_uses_reset_path_even_with_report():
    session = _make_session()
    result = session.load_document(_DOC_V1, report=True)
    assert result.success
    assert session.backend.raw.reset_calls == 0  # a pristine backend is not reset (session_reset.py)
    assert session.backend.raw.sync_calls == []
    assert session.command_history[-1]["type"] == "document_load"
    assert session._loaded_text == _DOC_V1


def test_reload_same_theory_uses_sync_path():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    result = session.load_document(_DOC_V2, report=True)
    assert result.success
    # no second reset: ONE PIDE replace edit instead of a fresh elaboration
    assert session.backend.raw.reset_calls == 0
    assert len(session.backend.raw.sync_calls) == 1
    text, budget_ms, _ = session.backend.raw.sync_calls[0]
    assert text == _DOC_V2 and budget_ms > 0
    assert session.command_history[-1]["type"] == "document_sync"
    assert session._loaded_text == _DOC_V2
    assert session.last_chunk_report["report"]["line_semantics"] == "absolute"


def test_theory_name_change_uses_reset_path():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    session.load_document(_DOC_OTHER_THEORY, report=True)
    assert session.backend.raw.sync_calls == []
    assert session.backend.raw.reset_calls == 1
    assert session.backend.raw.entered == ["Foo", "Bar"]
    assert session.command_history[-1]["type"] == "document_load"
    assert session._loaded_text == _DOC_OTHER_THEORY


def test_backend_fallback_marker_falls_back_to_reset():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    # Gate passes (same theory name) but the backend declines the incremental
    # edit (e.g. the diff touches the `theory ... begin` span).
    session.backend.raw.sync_reply = _SYNC_FALLBACK_REPORT
    result = session.load_document(_DOC_V2, report=True)
    assert result.success
    assert len(session.backend.raw.sync_calls) == 1  # attempted ...
    assert session.backend.raw.reset_calls == 1      # ... then reset anyway
    assert session.command_history[-1]["type"] == "document_load"
    assert session._loaded_text == _DOC_V2


def test_report_false_never_syncs():
    session = _make_session()
    session.load_document(_DOC_V1)
    session.load_document(_DOC_V2)
    assert session.backend.raw.sync_calls == []
    assert session.backend.raw.reset_calls == 1
    # ... but _loaded_text is still tracked for later report=True syncs
    session.load_document(_DOC_V2, report=True)
    assert len(session.backend.raw.sync_calls) == 1
    assert session.backend.raw.reset_calls == 1


def test_imports_mode_never_syncs():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    session.load_document("lemma True by simp", thy_name="Foo",
                          imports=["Main"], report=True)
    assert session.backend.raw.sync_calls == []
    assert session.backend.raw.reset_calls == 1


# ------------------------------------------------------- sync bookkeeping


def test_sync_invalidates_checkpoints():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    session.checkpoints[3] = 123.0
    session.load_document(_DOC_V2, report=True)
    assert session.checkpoints == {}


def test_sync_keeps_command_history_and_theory():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    session.load_document(_DOC_V2, report=True)
    # unlike the reset path, sync does NOT clear history or re-enter the theory
    assert [h["type"] for h in session.command_history] == [
        "document_load", "document_sync",
    ]
    assert session.backend.raw.entered == ["Foo"]
    assert session.entered_thy == "Foo"


def test_sync_report_error_extraction_uses_backend_lines():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    session.backend.raw.sync_reply = _SYNC_FAILED_REPORT
    result = session.load_document(_DOC_V2, report=True)
    assert not result.success
    # the sync report's `line` is ABSOLUTE (line_semantics: absolute) and is
    # passed through to the error message unchanged
    assert result.error == "line 3: Failed to finish proof"
    assert session.last_chunk_report["report"]["success"] is False


def test_sync_probe_state_skipped_after_theory_end():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    session.load_document(_DOC_V2, report=True)  # ends with theory `end`
    assert session.backend.raw.sync_calls[0][2] is False


def test_sync_probe_state_without_theory_end():
    session = _make_session()
    open_v1 = "theory Foo imports Main begin\nlemma True by simp"
    open_v2 = "theory Foo imports Main begin\nlemma True by simp\nlemma True by simp"
    session.load_document(open_v1, report=True)
    session.load_document(open_v2, report=True)
    assert session.backend.raw.sync_calls[0][2] is True


def test_unparseable_sync_reply_is_a_failure_not_a_fallback():
    session = _make_session()
    session.load_document(_DOC_V1, report=True)
    session.backend.raw.sync_reply = "not json"
    result = session.load_document(_DOC_V2, report=True)
    # junk is NOT a fallback marker: the load "succeeded" as a sync with a
    # failing report, no reset happened
    assert not result.success
    assert result.error == "unparseable backend report"
    assert session.backend.raw.reset_calls == 0
    assert session.command_history[-1]["type"] == "document_sync"

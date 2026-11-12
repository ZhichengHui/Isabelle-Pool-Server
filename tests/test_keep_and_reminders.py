"""A first load that outlives its budget is KEPT (file bindings) and long waits are reminded in
tiers (tool results). No backend: fake raw backend / fake client."""
from __future__ import annotations

import asyncio
import json

import pytest

from mcp_servers.lsp.config import Config
from mcp_servers.lsp.reminders import add_reminder, reminder
from tests.test_document_sync import _DOC_V1, _make_session
from tests.test_mcp_lsp_server import _pool_with_fake


# ---- server: keep_on_timeout picks the backend entry that keeps the edit

def test_keep_on_timeout_uses_the_keeping_backend_entry():
    s = _make_session()
    raw = s.backend.raw
    raw.kept = []
    raw.step_chunk_report_kept = lambda t, b, p: raw.kept.append(t) or '{"success": true, "commands": []}'
    s.load_document(_DOC_V1, report=True, keep_on_timeout=True)
    assert raw.kept == [_DOC_V1]
    assert raw.step_probe_states == []  # the discarding entry was not used


def test_default_still_uses_the_discarding_entry():
    s = _make_session()
    s.load_document(_DOC_V1, report=True)
    assert len(s.backend.raw.step_probe_states) == 1


# ---- MCP: the file binding asks for it, scratch sessions do not

def test_file_binding_load_keeps_on_timeout(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")
    pool = _pool_with_fake()

    async def run():
        b = await pool.get_binding(str(f))
        await pool.sync(b)

    asyncio.run(run())
    assert pool._client.loads[0]["keep_on_timeout"] is True


# ---- reminders

@pytest.fixture
def tiers(monkeypatch):
    monkeypatch.setattr(Config, "REMINDER_S", "30,120,300,600")


def test_no_reminder_before_the_first_threshold(tiers):
    assert reminder(29, "x") is None


def test_levels_and_next_time(tiers):
    r1, r2, r4 = reminder(31, "x"), reminder(130, "x"), reminder(700, "x")
    assert (r1["level"], r1["of"], r1["next_reminder_at_s"]) == (1, 4, 120)
    assert (r2["level"], r2["next_reminder_at_s"]) == (2, 300)
    assert r4["level"] == 4 and r4["next_reminder_at_s"] is None
    assert "Reminder 1/4" in r1["message"] and "does not terminate" in r4["message"]
    assert "Reminder 1/4" not in r4["message"]


def test_off_when_empty(monkeypatch):
    monkeypatch.setattr(Config, "REMINDER_S", "")
    assert reminder(10_000, "x") is None


def test_add_reminder_only_on_timeout(tiers):
    out = {}
    add_reminder(out, False, 180, "this candidate")
    assert out == {}
    add_reminder(out, True, 180, "this candidate")
    assert "this candidate has run 180 s" in out["reminder"]


def test_still_running_carries_a_reminder_per_command(tiers, tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from mcp_servers.lsp import server as lsp_server

    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nend\n")
    pool = lsp_server.pool
    binding = type("B", (), {})()
    monkeypatch.setattr(pool, "peek_binding", lambda p: binding)
    monkeypatch.setattr(pool, "running_commands", lambda b: [
        {"line": 204, "kind": "apply", "running_for_s": 130},
        {"line": 9, "kind": "by", "running_for_s": 5}])
    out = json.loads(lsp_server._attach_still_running(json.dumps({"success": False}), str(f)))
    assert "Reminder 2/4" in out["reminder"] and "line 204" in out["reminder"]
    assert "reminder" in out["still_running"][0] and "reminder" not in out["still_running"][1]

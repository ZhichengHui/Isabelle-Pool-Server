"""fill_sorries stage 2 (plan 2026-10-18 §1.5, §3.3): the LSP MCP's
isabelle_fill_sorries / isabelle_fill_status / isabelle_fill_cancel against a
fake client — submit/poll/return, the patience partial return, status and
cancel, the two fill-specific HTTP errors, the guard's memory translation, and
the registry touch on each poll."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_mcp_lsp_server import FakeClient

pytest.importorskip("mcp")

ROW = {
    "index": 3,
    "node_line": 2,
    "line": 2,
    "host_kind": "lemma",
    "subgoals": 1,
    "outcome": "proved",
    "method": "by simp",
    "reason": None,
    "per_subgoal": [{"i": 1, "outcome": "proved", "method": "by simp", "ms": 900}],
}
UNSOLVED = {**ROW, "index": 5, "line": 3, "outcome": "none", "method": None}


def _body(status, results=(ROW, UNSOLVED), **kw):
    body = {
        "status": status,
        "n": len(results),
        "done": len(results),
        "filled": 1,
        "current": 1,
        "queue_position": -1,
        "parked": False,
        "parked_reason": None,
        "parked_since": None,
        "elapsed_s": 12.0,
        "eta_s": {"worst": 60.0, "typical": 6.0},
        "results": list(results),
        "patched_text": "theory T imports Main begin\nlemma a: True by simp\nlemma b: False sorry\nend\n",
        "error": None,
        "sledgehammer_peak_mb": 512.0,
    }
    body.update(kw)
    return body


class FillFakeClient(FakeClient):
    """FakeClient + the three fill endpoints with a programmable status sequence."""

    def __init__(self):
        super().__init__()
        self.submits = []
        self.status_calls = 0
        self.statuses = []  # bodies returned by fill_status in order (last one repeats)
        self.submit_response = _body("queued")
        self.submit_error = None  # an httpx.HTTPStatusError to raise on submit
        self.status_error = None
        self.cancelled = []

    async def fill_sorries(
        self,
        session_id,
        text,
        *,
        mode="document",
        timeout_s=30,
        max_total_s=600.0,
        provers=None,
        load_timeout=120.0,
        lease_id=None,
        master_dir=None,
    ):
        if self.submit_error is not None:
            raise self.submit_error
        self.submits.append(
            {
                "session_id": session_id,
                "text": text,
                "mode": mode,
                "timeout_s": timeout_s,
                "max_total_s": max_total_s,
            }
        )
        return self.submit_response

    async def fill_status(self, session_id, *, lease_id=None):
        self.status_calls += 1
        if self.status_error is not None:
            raise self.status_error
        if not self.statuses:
            return _body("done")
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    async def fill_cancel(self, session_id, *, lease_id=None):
        self.cancelled.append(session_id)
        return {
            "cancelled": True,
            **_body("cancelled", results=(ROW, {**UNSOLVED, "outcome": "skipped"})),
        }


def _http_error(status, body):
    resp = httpx.Response(
        status, json=body, request=httpx.Request("POST", "http://test/fill")
    )
    return httpx.HTTPStatusError(str(status), request=resp.request, response=resp)


def _app(tmp_path, monkeypatch):
    import types

    from mcp_servers.lsp import (
        app as lsp_app,  # pylint: disable=import-outside-toplevel
    )
    from mcp_servers.lsp import fill_tools  # pylint: disable=import-outside-toplevel
    from mcp_servers.lsp import (
        server as lsp_server,  # pylint: disable=import-outside-toplevel
    )
    from mcp_servers.lsp.config import Config  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(Config, "FILL_POLL_S", 0.01)
    fake = FillFakeClient()
    lsp_server.pool._client = fake  # pylint: disable=protected-access
    f = tmp_path / "Fill.thy"
    f.write_text(
        "theory Fill imports Main begin\nlemma a: True sorry\nlemma b: False sorry\nend\n"
    )
    app = types.SimpleNamespace(
        isabelle_open=lsp_app.isabelle_open,
        isabelle_fill_sorries=fill_tools.isabelle_fill_sorries,
        isabelle_fill_status=fill_tools.isabelle_fill_status,
        isabelle_fill_cancel=fill_tools.isabelle_fill_cancel,
    )
    return app, fake, str(f)


def _call(coro):
    return json.loads(asyncio.run(coro))


def test_submit_poll_and_return_patched_text(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    fake.statuses = [_body("running"), _body("running"), _body("done")]
    out = _call(app.isabelle_fill_sorries(file_path=path, timeout_s=20))
    assert fake.submits[0]["mode"] == "document" and fake.submits[0]["timeout_s"] == 20
    assert fake.submits[0]["text"].startswith(
        "theory Fill"
    )  # the file's text, synced first
    assert out["status"] == "done" and out["continuing"] is False
    assert out["filled"] == 1 and out["n"] == 2 and "patched_text" in out
    assert [r["line"] for r in out["results"]] == [2, 3] and "index" not in out[
        "results"
    ][0]
    assert any("isabelle_diagnostic_messages" in a for a in out["advice"])
    assert any("[3]" in a for a in out["advice"])  # the unsolved line is named
    assert fake.status_calls == 3


def test_patience_returns_partial_state_continuing(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    fake.statuses = [_body("running", queue_position=1, parked=False)]
    out = _call(app.isabelle_fill_sorries(file_path=path, patience_s=0.05))
    assert out["continuing"] is True and out["status"] == "running"
    assert any("isabelle_fill_status" in a and "KEEPS" not in a for a in out["advice"])
    assert any("isabelle_fill_cancel" in a for a in out["advice"])
    assert out["eta_s"] == {"worst": 60.0, "typical": 6.0}


def test_parked_job_is_explained(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    fake.statuses = [_body("running", parked=True, parked_reason="critical")]
    out = _call(app.isabelle_fill_sorries(file_path=path, patience_s=0.03))
    assert out["parked"] is True and any(
        "parked" in a and "critical" in a for a in out["advice"]
    )


def test_status_and_cancel_tools(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    _call(app.isabelle_open(file_path=path))
    fake.statuses = [_body("running")]
    st = _call(app.isabelle_fill_status(file_path=path))
    assert st["continuing"] is True and fake.status_calls == 1
    fake.statuses = [_body("done")]
    st = _call(app.isabelle_fill_status(file_path=path))
    assert st["continuing"] is False and st["filled"] == 1
    cn = _call(app.isabelle_fill_cancel(file_path=path))
    assert (
        cn["cancelled"] is True
        and cn["status"] == "cancelled"
        and cn["continuing"] is False
    )
    assert cn["results"][1]["outcome"] == "skipped" and fake.cancelled == [
        fake.acquired and "s1"
    ]


def test_no_fill_job_404_does_not_rebind(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    _call(app.isabelle_open(file_path=path))
    n_acquired = len(fake.acquired)
    fake.status_error = _http_error(404, {"error": "no_fill_job", "detail": "no job"})
    out = _call(app.isabelle_fill_status(file_path=path))
    assert out["error"] == "no_fill_job" and out["advice"]
    assert len(fake.acquired) == n_acquired  # no rebind: the session is fine


def test_fill_in_flight_409(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    fake.submit_error = _http_error(
        409, {"error": "fill_in_flight", "detail": "running"}
    )
    out = _call(app.isabelle_fill_sorries(file_path=path))
    assert (
        out["error"] == "fill_in_flight" and "isabelle_fill_status" in out["advice"][0]
    )


def test_memory_503_translated_by_the_guard(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    fake.submit_error = _http_error(
        503,
        {
            "error": "memory_pressure",
            "state": "critical",
            "retry_after_s": 5,
            "memory": {
                "pressure_pct": 91.0,
                "available_mb": 800.0,
                "limit_mb": 14336.0,
            },
            "evictable": [],
            "leased": [],
            "builds_in_flight": {"bigstep": 0, "heap": 0},
        },
    )
    out = _call(app.isabelle_fill_sorries(file_path=path))
    assert (
        out["error"] == "memory_pressure"
        and out["advice"]
        and "HTTPStatusError" not in json.dumps(out)
    )


def test_no_sorry_returns_at_once(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    fake.submit_response = _body("done", results=(), filled=0, n=0)
    out = _call(app.isabelle_fill_sorries(file_path=path))
    assert out["n"] == 0 and out["continuing"] is False and fake.status_calls == 0
    assert any("no `sorry`" in a for a in out["advice"])


def test_polls_touch_the_registry(tmp_path, monkeypatch):
    app, fake, path = _app(tmp_path, monkeypatch)
    from mcp_servers.lsp import (
        server as lsp_server,  # pylint: disable=import-outside-toplevel
    )

    _call(app.isabelle_open(file_path=path))
    (entry,) = lsp_server.pool.registry.all()
    before = entry.last_used_at
    fake.statuses = [_body("running"), _body("done")]
    asyncio.run(asyncio.sleep(0.01))
    _call(app.isabelle_fill_sorries(file_path=path))
    assert entry.last_used_at > before


def test_status_and_cancel_on_an_unopened_file_never_open_a_session(
    tmp_path, monkeypatch
):
    """Review aa96c6d S1: after an MCP restart / on a mistyped path the status
    and cancel tools answer no_fill_job without acquiring a session."""
    app, fake, path = _app(tmp_path, monkeypatch)
    st = _call(app.isabelle_fill_status(file_path=path))
    cn = _call(app.isabelle_fill_cancel(file_path=str(tmp_path / "Nope.thy")))
    assert st["error"] == "no_fill_job" and cn["error"] == "no_fill_job"
    assert fake.acquired == [] and fake.status_calls == 0 and fake.cancelled == []


def test_advice_names_the_parked_budget_exhaustion():
    """Review ed2d751 B1: a job that ended on max_total_s while parked says so."""
    from mcp_servers.lsp.fill_tools import shape

    body = _body(
        "done",
        results=(
            ROW,
            {
                **UNSOLVED,
                "outcome": "skipped",
                "reason": "max_total_s while parked (critical)",
            },
        ),
    )
    out = shape(body, continuing=False)
    assert out["continuing"] is False
    assert any(
        "max_total_s" in a and "parked (critical)" in a and "[3]" in a
        for a in out["advice"]
    )

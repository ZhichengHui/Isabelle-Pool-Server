"""While the server builds the image a file's session needs, a tool on the file reports the build
(a normal result, no "retry"); the server's other session-start answers are worded honestly."""
import asyncio
import json

import httpx

from mcp_servers.common.guard import make_guard
from mcp_servers.common.memory_advice import ToolNames
from mcp_servers.common.registry import SessionRegistry
from mcp_servers.lsp.heap_notes import HeapBuilding, building_result, join_notes, raise_if_building

BUILDING = {"error": "building_import_heap", "status": "building", "session": "IPS_abc",
            "sessions_built": ["S_ih"], "sessions_running": ["IPS_abc"], "elapsed_s": 340}


def test_the_building_result_says_what_and_how_far_without_retry():
    r = building_result(BUILDING)
    assert r["status"] == "building_import_heap" and r["session"] == "IPS_abc"
    m = r["message"]
    assert "building a heap for this file's imports (IPS_abc)" in m
    assert "now building IPS_abc" in m and "done: S_ih" in m and "340 s so far" in m
    assert "retry" not in m.lower()


def test_raise_if_building():
    raise_if_building({"session_id": "s", "lease_id": "l"})  # a session: nothing
    try:
        raise_if_building(BUILDING)
    except HeapBuilding as e:
        assert e.agent_result["session"] == "IPS_abc"
    else:
        raise AssertionError("a 202 must raise HeapBuilding")


def test_join_notes():
    assert join_notes(None, None) is None
    assert join_notes("a", None, "b") == "a b"


def _guard():
    async def get_client():
        class C:
            last_memory_state = "ok"
        return C()

    tools = ToolNames(release_idle="r()", close="c()", list_sessions="l()",
                      memory_status="m()", reopen="reopen")
    return make_guard(registry=SessionRegistry(), tools=tools, get_client=get_client,
                      owner_of=lambda a, k: None)


def test_the_guard_returns_a_build_in_progress_as_the_result():
    @_guard()
    async def tool() -> str:
        raise HeapBuilding(BUILDING)

    out = json.loads(asyncio.run(tool()))
    assert out["status"] == "building_import_heap" and "error" not in out


def test_other_failures_are_not_swallowed():
    @_guard()
    async def tool() -> str:
        raise ValueError("boom")

    try:
        asyncio.run(tool())
    except ValueError:
        pass
    else:
        raise AssertionError("a failure must propagate as before")


def _status_error(status, body):
    resp = httpx.Response(status, json=body, request=httpx.Request("POST", "http://gym/x"))
    return httpx.HTTPStatusError(str(status), request=resp.request, response=resp)


def _translated(status, body):
    @_guard()
    async def tool() -> str:
        raise _status_error(status, body)

    return json.loads(asyncio.run(tool()))


def test_session_start_answers_are_translated_honestly():
    out = _translated(504, {"error": "session_start_timeout", "detail": "took longer than 600s"})
    assert out["error"] == "session_start_timeout" and "gateway" not in out["advice"][0]
    out = _translated(422, {"error": "import_heap_failed", "session": "IPS_abc",
                            "log_tail": "*** Undefined fact: b2"})
    assert out["error"] == "import_heap_failed" and out["log_tail"].endswith("b2")
    out = _translated(409, {"error": "image_not_current", "detail": "Session image X ..."})
    assert out["error"] == "image_not_current" and "out of date" in out["advice"][0]


# ----- the pool: a 202 on opening a file, and on a scratch session -----

def _pool(monkeypatch, tmp_path, answer):
    from mcp_servers.lsp import pool as pool_mod

    (tmp_path / "A.thy").write_text("theory A imports Main begin end")

    class Client:
        async def acquire_session(self, **kw):
            return answer

        async def create_session(self, **kw):
            return answer

    async def ctx(c, text, canon, heap_session):
        return ["Main"], "HOL", ()

    p = pool_mod.LspPool()

    async def client():
        return Client()

    monkeypatch.setattr(p, "client", client)
    monkeypatch.setattr(pool_mod, "file_context", ctx)
    monkeypatch.setattr(pool_mod.lease_renewal, "ensure", lambda pool: None)
    monkeypatch.setattr(pool_mod.idle_release, "ensure", lambda pool: None)
    return p, str(tmp_path / "A.thy")


def test_opening_a_file_while_its_heap_builds_binds_nothing(monkeypatch, tmp_path):
    p, f = _pool(monkeypatch, tmp_path, BUILDING)
    try:
        asyncio.run(p.get_binding(f))
    except HeapBuilding as e:
        assert e.agent_result["session"] == "IPS_abc"
    else:
        raise AssertionError("must report the build")
    assert p.peek_binding(f) is None  # the next call asks the server again


def test_a_scratch_session_while_the_heap_builds_gives_its_slot_back(monkeypatch, tmp_path):
    p, _ = _pool(monkeypatch, tmp_path, BUILDING)
    key = p.scratch_key("g", None, ["Main"], "HOL")
    try:
        asyncio.run(p.acquire_scratch(key))
    except HeapBuilding:
        pass
    else:
        raise AssertionError("must report the build")
    assert p._scratch_counts.get(key, 0) == 0

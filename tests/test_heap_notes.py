"""The heap note: a file whose imports load from source because no usable heap holds them is told
why (no heap / an out-of-date heap) and what to build, on success and in the failure result."""
import asyncio
import json

import httpx

from mcp_servers.common.guard import make_guard
from mcp_servers.common.memory_advice import ToolNames
from mcp_servers.common.registry import SessionRegistry
from mcp_servers.lsp.heap_notes import heap_note


def _project(tmp_path):
    (tmp_path / "proj").mkdir(exist_ok=True)
    (tmp_path / "proj" / "ROOT").write_text('session Sort = HOL +\n  theories B\n')
    return [str(tmp_path / "proj")]


def test_no_note_when_nothing_loads_from_source(tmp_path):
    assert heap_note([], [], _project(tmp_path), "g") is None
    assert heap_note([], ["Sort"], _project(tmp_path), "g") is None


def test_no_heap_of_an_own_session_says_build_it(tmp_path):
    dirs = _project(tmp_path)
    note = heap_note(["Sort.B"], [], dirs, "g")
    assert note.startswith("No heap contains the theories this file imports")
    assert "Sort.B" in note and "loaded from source" in note
    assert f'isabelle_build_heap(task_group="g", project="{dirs[0]}", session_name="Sort")' in note
    assert "out of date" not in note


def test_an_out_of_date_own_heap_says_so_and_rebuild_it(tmp_path):
    dirs = _project(tmp_path)
    note = heap_note(["Sort.B"], ["Sort"], dirs, "g")
    assert note.startswith("The heap Sort contains the theories this file imports, but it is out of date")
    assert "source theories changed after it was built" in note
    assert "cannot be used" in note and "loaded from source" in note
    assert f'Rebuild it with isabelle_build_heap(task_group="g", project="{dirs[0]}"' in note
    assert "each time you change those theories" in note


def test_an_out_of_date_heap_of_another_session_is_reported_not_rebuilt(tmp_path):
    note = heap_note(["Lib.X"], ["Lib"], _project(tmp_path), "g")
    assert "out of date" in note and "do not rebuild it" in note and "tell the user" in note
    assert "isabelle_build_heap" not in note


def test_no_note_for_theories_outside_the_project_without_a_stale_heap(tmp_path):
    assert heap_note(["HOL-Library.Multiset"], [], _project(tmp_path), "g") is None
    assert heap_note(["Sort.B"], [], [], "g") is None  # no project directories known


def test_file_context_note_words_the_servers_facts(tmp_path, monkeypatch):
    from mcp_servers.lsp import header_context as hc

    dirs = _project(tmp_path)
    monkeypatch.setattr(hc, "project_context", lambda imports, f: ("HOL", tuple(dirs)))

    class Client:
        def __init__(self, answer):
            self.answer = answer

        async def parse_theory_header(self, text):
            return {"imports": ["Sort.B"], "suggested_field": None}

        async def select_image(self, imports, field, dirs):
            return self.answer

    f = str(tmp_path / "proj" / "A.thy")
    _, field, _, note = asyncio.run(hc.file_context_note(
        Client({"field": "HOL", "from_source": ["Sort.B"], "stale": ["Sort"]}), "", f,
        task_group="g"))
    assert field == "HOL" and "out of date" in note
    _, field, _, note = asyncio.run(hc.file_context_note(
        Client({"field": "Sort", "from_source": [], "stale": []}), "", f))
    assert field == "Sort" and note is None
    _, _, _, note = asyncio.run(hc.file_context_note(Client({"field": "HOL"}), "", f))
    assert note is None  # an older server: no facts, no note


def _guard():
    async def get_client():
        class C:
            last_memory_state = "ok"
        return C()

    tools = ToolNames(release_idle="r()", close="c()", list_sessions="l()",
                      memory_status="m()", reopen="reopen")
    return make_guard(registry=SessionRegistry(), tools=tools, get_client=get_client,
                      owner_of=lambda a, k: None)


def test_a_failed_open_puts_the_note_before_the_retry_advice():
    resp = httpx.Response(503, json={"error": "gateway_unavailable"}, headers={"Retry-After": "10"},
                          request=httpx.Request("POST", "http://gym/x"))
    err = httpx.HTTPStatusError("503", request=resp.request, response=resp)
    err.agent_note = "The heap Sort ... is out of date"

    @_guard()
    async def tool() -> str:
        raise err

    out = json.loads(asyncio.run(tool()))
    assert out["error"] == "server_recovering"
    assert out["heap_note"] == err.agent_note
    assert out["advice"][0] == err.agent_note and "retry" in out["advice"][1]


def test_a_failure_without_http_status_carries_the_note_too():
    err = httpx.ReadTimeout("timed out")
    err.agent_note = "No heap contains ..."

    @_guard()
    async def tool() -> str:
        raise err

    out = json.loads(asyncio.run(tool()))
    assert out["heap_note"] == err.agent_note and "ReadTimeout" in out["error"]


def test_other_failures_are_not_swallowed():
    @_guard()
    async def tool() -> str:
        raise ValueError("boom")

    try:
        asyncio.run(tool())
    except ValueError:
        pass
    else:
        raise AssertionError("a failure without a note must propagate as before")


def test_a_successful_open_shows_the_note_once(tmp_path, monkeypatch):
    from mcp_servers.lsp import server
    from mcp_servers.lsp.pool import FileBinding

    b = FileBinding(file_path="/x/A.thy", session_id="s", lease_id="l", task_group="g",
                    heap_note="No heap contains ...")
    monkeypatch.setattr(server.pool, "peek_binding", lambda p: b)
    first = json.loads(server._attach_still_running(json.dumps({"ok": True}), "/x/A.thy"))
    second = json.loads(server._attach_still_running(json.dumps({"ok": True}), "/x/A.thy"))
    assert first["heap_note"] == "No heap contains ..." and "heap_note" not in second

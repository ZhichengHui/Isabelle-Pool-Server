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


def test_a_failure_without_http_status_stays_a_failure_and_carries_the_note():
    err = httpx.ReadTimeout("timed out")
    err.agent_note = "No heap contains ..."

    @_guard()
    async def tool() -> str:
        raise err

    try:
        asyncio.run(tool())
    except RuntimeError as e:  # a tool error for the client (isError), not a normal result
        assert "ReadTimeout: timed out" in str(e) and "No heap contains ..." in str(e)
        assert e.__cause__ is err
    else:
        raise AssertionError("the failure must propagate")


def test_an_out_of_date_start_image_is_not_reported_as_busy():
    resp = httpx.Response(409, json={"error": "image_not_current", "detail": "Session image X ..."},
                          request=httpx.Request("POST", "http://gym/x"))

    @_guard()
    async def tool() -> str:
        raise httpx.HTTPStatusError("409", request=resp.request, response=resp)

    out = json.loads(asyncio.run(tool()))
    assert out["error"] == "image_not_current" and "out of date" in out["advice"][0]


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


# ----- theories of the file's own directory, imported by bare name -----

from mcp_servers.lsp.heap_notes import join_notes, sibling_note  # noqa: E402


def test_sibling_note_gives_the_steps_with_the_real_names():
    note = sibling_note(["/w/proj/B", "/w/proj/C"], "AutoCorrode", "g", 312.4, failed=False)
    assert "by bare name (B, C)" in note and "this open took 312 s" in note
    # the layout verified live (2026-11-23): theories in their own subdirectory, the file outside
    # it (in the same directory the heap-session wrapper imports the file itself: cyclic dependency)
    assert "move B, C (and the theories of /w/proj they import) into a new subdirectory " \
           "/w/proj/<Name>" in note
    assert 'ROOT file there with `session <Name> = "AutoCorrode" + theories B C`' in note
    assert '`"<Name>.B"` instead of `B`' in note
    assert 'isabelle_build_heap(task_group="g", project="/w/proj/<Name>", session_name="<Name>")' \
           in note
    assert 'isabelle_open(file_path, task_group="g", heap_session="<Name>")' in note
    assert "Keep this file outside that subdirectory" in note
    assert "other heaps of this task group" in note  # two heaps of one name: neither resolves


def test_sibling_note_on_failure_and_without_siblings():
    assert "this open failed after 600 s" in sibling_note(["/w/B"], "HOL", "g", 600, failed=True)
    assert sibling_note([], "HOL", "g", 600, failed=True) is None


def test_join_notes():
    assert join_notes(None, None) is None
    assert join_notes("a", None, "b") == "a b"


def _pool_with(monkeypatch, tmp_path, acquire, note=None):
    """An LspPool whose client answers `acquire`, for a file importing its sibling B."""
    from mcp_servers.lsp import pool as pool_mod

    (tmp_path / "A.thy").write_text('theory A imports B begin end')
    (tmp_path / "B.thy").write_text('theory B imports Main begin end')
    sib = str(tmp_path / "B")

    async def ctx(c, text, canon, heap_session, group):
        return [sib], "HOL", (), note

    class Client:
        acquire_session = acquire

    p = pool_mod.LspPool()

    async def client():
        return Client()

    monkeypatch.setattr(p, "client", client)
    monkeypatch.setattr(pool_mod, "file_context_note", ctx)
    monkeypatch.setattr(pool_mod.lease_renewal, "ensure", lambda pool: None)
    return p, str(tmp_path / "A.thy")


def test_a_slow_open_with_siblings_gets_the_sibling_note(monkeypatch, tmp_path):
    from mcp_servers.lsp import pool as pool_mod

    async def acquire(self, **kw):
        return {"session_id": "s", "lease_id": "l"}

    p, f = _pool_with(monkeypatch, tmp_path, acquire)
    monkeypatch.setattr(pool_mod.Config, "SIBLING_NOTE_S", 0.0)
    b = asyncio.run(p._create_binding(f, None, None, None))
    assert "by bare name (B)" in b.heap_note and "this open took" in b.heap_note
    monkeypatch.setattr(pool_mod.Config, "SIBLING_NOTE_S", 3600.0)
    b = asyncio.run(p._create_binding(f, None, None, None))
    assert b.heap_note is None  # a fast open: no note


def test_a_failed_open_with_siblings_carries_both_notes(monkeypatch, tmp_path):
    async def acquire(self, **kw):
        raise httpx.ReadTimeout("timed out")

    p, f = _pool_with(monkeypatch, tmp_path, acquire, note="No heap contains X.")
    try:
        asyncio.run(p._create_binding(f, None, None, None))
    except httpx.ReadTimeout as e:
        assert e.agent_note.startswith("No heap contains X. This file imports theories")
        assert "this open failed after" in e.agent_note
    else:
        raise AssertionError("the failure must propagate")


def test_the_proposed_heap_itself_out_of_date_says_the_session_cannot_start(tmp_path):
    dirs = _project(tmp_path)
    note = heap_note(["Sort.B"], ["Sort"], dirs, "g", field_stale=True)
    assert "it is out of date" in note and "cannot start until it is rebuilt" in note
    assert "loaded from source" not in note
    assert "Rebuild it with isabelle_build_heap(" in note


def test_own_and_other_out_of_date_heaps_get_their_own_advice(tmp_path):
    dirs = _project(tmp_path)
    note = heap_note(["Sort.B"], ["Sort", "Shared", "Lib2"], dirs, "g")
    assert note.startswith("The heaps Sort, Shared, Lib2 contain")
    assert "they are out of date: their source theories changed after they were built" in note
    assert 'Rebuild it with isabelle_build_heap(task_group="g"' in note
    assert "Shared, Lib2 are not sessions of this project: do not rebuild them; " \
           "tell the user that they are out of date." in note


def _status_error(status, body):
    resp = httpx.Response(status, json=body, request=httpx.Request("POST", "http://gym/x"))
    return httpx.HTTPStatusError(str(status), request=resp.request, response=resp)


def test_a_quick_refusal_for_another_reason_carries_no_note(monkeypatch, tmp_path):
    async def acquire(self, **kw):
        raise _status_error(503, {"error": "pool_full"})

    p, f = _pool_with(monkeypatch, tmp_path, acquire, note="No heap contains X.")
    try:
        asyncio.run(p._create_binding(f, None, None, None))
    except httpx.HTTPStatusError as e:
        assert not getattr(e, "agent_note", None)
    else:
        raise AssertionError("the failure must propagate")


def test_a_refused_out_of_date_image_carries_the_heap_note_only(monkeypatch, tmp_path):
    async def acquire(self, **kw):
        raise _status_error(409, {"error": "image_not_current", "detail": "..."})

    p, f = _pool_with(monkeypatch, tmp_path, acquire, note="The heap Sort ... rebuilt.")
    try:
        asyncio.run(p._create_binding(f, None, None, None))
    except httpx.HTTPStatusError as e:
        assert e.agent_note == "The heap Sort ... rebuilt."  # no sibling note: it was quick
    else:
        raise AssertionError("the failure must propagate")


def test_the_sibling_note_names_the_heap_as_parent(monkeypatch, tmp_path):
    from mcp_servers.lsp import pool as pool_mod

    async def acquire(self, **kw):
        return {"session_id": "s", "lease_id": "l"}

    p, f = _pool_with(monkeypatch, tmp_path, acquire)
    monkeypatch.setattr(pool_mod.Config, "SIBLING_NOTE_S", 0.0)
    b = asyncio.run(p._create_binding(f, None, "MyHeap", None))
    assert 'session <Name> = "MyHeap" + theories B' in b.heap_note

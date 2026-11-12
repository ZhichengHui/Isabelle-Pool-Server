"""An unqualified import of a sibling theory (same directory, same session) resolves as
`isabelle build` does: against the directory of the importing file.

The server places a document in its own repl directory, so a bare `imports A` used to be looked
up in server/repl/thys/ ("Cannot load theory file"). The LSP MCP now (a) names such a sibling by
its absolute path when it builds the session's wrapper theory (acquire / scratch create) and
(b) tells the server the file's directory on every load_document (master_dir).
No backend: fake client, as in test_mcp_lsp_server.py. The Scala side is covered by
tests/test_master_dir_live.py."""
from __future__ import annotations

import asyncio
import json

from mcp_servers.lsp.pool import resolve_sibling_imports
from tests.test_mcp_lsp_server import _pool_with_fake
from tests.test_mcp_lsp_tool_fixes import _fresh

A = "theory A imports Main begin\nlemma a: True by simp\nend\n"
B = "theory B imports A begin\nlemma b: True by (rule a)\nlemma c: True\n  by (rule a)\nend\n"


def _top(tmp_path):
    (tmp_path / "A.thy").write_text(A)
    b = tmp_path / "B.thy"
    b.write_text(B)
    return b


def test_open_names_the_sibling_by_path_and_load_carries_master_dir(tmp_path):
    b = _top(tmp_path)
    pool = _pool_with_fake()

    async def run():
        binding = await pool.get_binding(str(b))
        await pool.sync(binding)

    asyncio.run(run())
    d = str(tmp_path.resolve())
    assert pool._client.acquired[0]["theories"] == [d + "/A"]
    assert pool._client.loads[0]["master_dir"] == d


def test_rebind_after_eviction_keeps_master_dir(tmp_path):
    b = _top(tmp_path)
    pool = _pool_with_fake()

    async def run():
        binding = await pool.get_binding(str(b))
        pool._client.fail_next_load_404 = True
        await pool.sync(binding)

    asyncio.run(run())
    assert pool._client.loads[-1]["master_dir"] == str(tmp_path.resolve())
    assert pool._client.acquired[-1]["theories"] == [str(tmp_path.resolve()) + "/A"]


def test_resolution_leaves_everything_else_alone(tmp_path):
    (tmp_path / "A.thy").write_text(A)
    d = str(tmp_path.resolve())
    imports = ["Main", "A", "Ghost", "HOL-Library.Multiset", "BVSmt_Smt.A", "~~/src/HOL/Foo"]
    # only the bare name with a file next to the importer changes
    assert resolve_sibling_imports(imports, d) == [
        "Main", d + "/A", "Ghost", "HOL-Library.Multiset", "BVSmt_Smt.A", "~~/src/HOL/Foo"]
    assert resolve_sibling_imports(imports, None) == imports


def test_no_sibling_no_change(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")
    pool = _pool_with_fake()
    asyncio.run(pool.get_binding(str(f)))
    assert pool._client.acquired[0]["theories"] == ["Main"]


def test_multi_attempt_scratch_session_uses_the_same_resolution(tmp_path, monkeypatch):
    b = _top(tmp_path)
    lsp_app, fake = _fresh(monkeypatch)
    fake.next_reports = [{"success": True, "commands": []}, {"success": True, "commands": []}]

    async def run():
        await lsp_app.isabelle_open(file_path=str(b))
        return json.loads(await lsp_app.isabelle_multi_attempt(
            file_path=str(b), line=3, candidates=["lemma c: True\n  by (rule a)"]))

    asyncio.run(run())
    d = str(tmp_path.resolve())
    assert fake.created and all(c["theories"] == [d + "/A"] for c in fake.created)
    assert len(fake.loads) == 2 and all(l["master_dir"] == d for l in fake.loads)


def test_siblings_imports_choose_the_image(tmp_path):
    """B -> C -> A, A imports "HOL-Library.Multiset": the image comes from A's import too, the
    wrapper still imports only C."""
    from mcp_servers.lsp.pool import header_fields
    from tests.test_mcp_lsp_server import FakeClient

    (tmp_path / "A.thy").write_text('theory A imports "HOL-Library.Multiset" begin end\n')
    (tmp_path / "C.thy").write_text("theory C imports A begin end\n")
    text = "theory B imports C begin end\n"
    d = str(tmp_path.resolve())
    imports, suggested = asyncio.run(header_fields(FakeClient(), text, d))
    assert imports == [d + "/C"]
    assert suggested == "HOL-Library"
    _, none = asyncio.run(header_fields(FakeClient(), text))  # no master dir: no lookup
    assert none is None

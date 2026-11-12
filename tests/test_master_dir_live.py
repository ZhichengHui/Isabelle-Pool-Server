"""Live regression test: a document that imports a sibling theory by its bare name loads like
`isabelle build` would load it (opt-in, needs a server with the HOL heap):

    ISABELLE_LIVE_TESTS=1 pytest tests/test_master_dir_live.py
    (ISABELLE_LIVE_URL, default http://localhost:8000)

The server and the test must see the same disk (the sibling is read from it)."""
from __future__ import annotations

import asyncio
import os

import pytest

URL = os.environ.get("ISABELLE_LIVE_URL", "http://localhost:8000")


def _server_up() -> bool:
    try:
        import httpx  # pylint: disable=import-outside-toplevel

        return httpx.get(f"{URL}/readyz", timeout=5).status_code == 200
    except Exception:  # pylint: disable=broad-exception-caught
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("ISABELLE_LIVE_TESTS") != "1" or not _server_up(),
    reason="live test: set ISABELLE_LIVE_TESTS=1 with an Isabelle Pool Server (HOL heap)",
)

A = 'theory A imports Main begin\nlemma a: "True" by simp\nend\n'
B = 'theory B imports A begin\nlemma b: "True" by (rule a)\nend\n'


def test_sibling_import_by_bare_name(tmp_path):
    from client import PoolAsyncClient  # pylint: disable=import-outside-toplevel
    from mcp_servers.lsp.pool import resolve_sibling_imports  # pylint: disable=import-outside-toplevel

    d = str(tmp_path.resolve())
    (tmp_path / "A.thy").write_text(A)

    async def run():
        c = PoolAsyncClient(URL)
        resp = await c.acquire_session(theories=resolve_sibling_imports(["A"], d), field="HOL")
        sid, lease = resp["session_id"], resp["lease_id"]
        try:
            return await c.load_document(sid, B, report=True, master_dir=d, lease_id=lease)
        finally:
            await c.close_session(sid, lease_id=lease)

    result = asyncio.run(run())
    errors = [m for cmd in result["report"]["commands"] for m in cmd.get("messages", [])
              if m.get("sev") == "error"]
    assert result["success"] and not errors, (result.get("error"), errors)

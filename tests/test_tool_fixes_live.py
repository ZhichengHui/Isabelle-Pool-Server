"""Live regression tests for the Scala side of the tool fixes (line resolution of
goals_at_line/command_at_line, `sorry_lines` of the report, the output of a transient query):
they need a running server with the HOL heap, so they are opt-in:

    ISABELLE_LIVE_TESTS=1 pytest tests/test_tool_fixes_live.py
    (ISABELLE_LIVE_URL, default http://localhost:8000)

The goal tests need the server to record proof states (ISABELLE_SHOW_STATES=true, the default)
and skip themselves otherwise. One session, closed at the end."""
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

# line numbers are used below: keep the layout
GOAL_THY = """theory Goal_Live
  imports Main
begin

lemma foo: "A \\<and> B \\<longrightarrow> B \\<and> A"
proof (rule impI, goal_cases)
  case 1
  show ?case
    apply (rule conjI)
    sorry
qed

end
"""

SORRY_THY = """theory Sorry_Live
  imports Main
begin

lemma first: "A \\<longrightarrow> A"
  sorry

lemma second: "(n::nat) + 0 = n"
  by simp

end
"""

SORRY_THY_TAIL_EDITED = SORRY_THY.replace("  by simp\n", "  by simp \n")

QUERY_THY = """theory Query_Live
  imports Main
begin

lemma foo: "(x::nat) + 0 = x"
  by (simp add: add_0_right add_0_right)

lemma tip: "A \\<longrightarrow> A"
  apply (rule impI)
"""

LOCALE_CLOSED = """theory Locale_Closed
  imports Main
begin

locale lc = fixes k :: nat assumes kpos: "k > 0"
begin

definition dd where "dd = k + 1"

lemma dd_pos: "dd > 0" by (simp add: dd_def)

end

end
"""

LOCALE_OPEN = LOCALE_CLOSED.replace("end\n\nend\n", "")


async def _with_session(body):
    from client.async_client import PoolAsyncClient  # pylint: disable=import-outside-toplevel

    async with PoolAsyncClient(URL, timeout=900) as c:
        s = await c.acquire_session(theories=["Main"], field="HOL", reuse_dirty=False,
                                    label="live-tool-fixes")
        try:
            return await body(c, s["session_id"], s["lease_id"])
        finally:
            await c.close_session(s["session_id"], lease_id=s["lease_id"])


def _run(body):
    return asyncio.run(_with_session(body))


def test_command_at_line_finds_the_indented_command_on_the_line():
    async def body(c, sid, lease):
        await c.load_document(sid, GOAL_THY, report=True, lease_id=lease)
        out = {}
        for line in range(5, 12):
            out[line] = await c.command_at_line(sid, line, lease_id=lease)
        return out

    out = _run(body)
    # every line that starts a command resolves to that command, indented or not
    expect = {5: "lemma", 6: "proof", 7: "case", 8: "show", 9: "apply", 10: "sorry", 11: "qed"}
    for line, kind in expect.items():
        assert out[line]["kind"] == kind, (line, out[line])
        assert out[line]["range"]["start"]["line"] == line, (line, out[line])


def test_goals_at_the_sorry_line_are_the_goals_the_apply_left():
    async def body(c, sid, lease):
        await c.load_document(sid, GOAL_THY, report=True, lease_id=lease)
        return await c.goals_at_line(sid, 10, lease_id=lease)

    out = _run(body)
    if out.get("error") and "ISABELLE_SHOW_STATES" in out["error"]:
        pytest.skip("the server does not record proof states")
    assert out["command"]["kind"] == "sorry" and out["command"]["range"]["start"]["line"] == 10
    assert len(out["goals_before"]) == 1 and "2 subgoals" in out["goals_before"][0]
    assert out["goals_after"] and "No subgoals" in out["goals_after"][0]


def test_report_lists_executed_sorry_lines_of_the_whole_file():
    async def body(c, sid, lease):
        first = await c.load_document(sid, SORRY_THY, report=True, lease_id=lease)
        second = await c.load_document(sid, SORRY_THY_TAIL_EDITED, report=True, lease_id=lease)
        return first["report"], second["report"]

    first, second = _run(body)
    assert first["sorry_lines"] == [6]
    # the second load re-processed only the tail: used_sorry (tail) is false, sorry_lines still
    # names the sorry of the untouched part
    assert second["sorry_lines"] == [6]


def test_sorry_in_a_comment_and_a_text_is_not_executed():
    thy = SORRY_THY.replace("  sorry\n", "  by simp  (* sorry *)\n").replace(
        "begin\n", "begin\n\ntext \\<open>sorry\\<close>\n", 1)

    async def body(c, sid, lease):
        return (await c.load_document(sid, thy, report=True, lease_id=lease))["report"]

    assert _run(body)["sorry_lines"] == []


def test_query_returns_only_its_own_output():
    async def body(c, sid, lease):
        await c.load_document(sid, QUERY_THY, report=True, lease_id=lease)
        ok = await c.diagnostic(sid, "thm foo", lease_id=lease)
        bad = await c.diagnostic(sid, "thm no_such_fact", lease_id=lease)
        return ok, bad

    ok, bad = _run(body)
    # the file has a warning (duplicate simp rule) and an open proof: neither is part of the answer
    assert ok["success"] is True and not ok["error"]
    assert ok["output"].strip() == "?x + 0 = ?x"
    assert bad["success"] is False
    assert "no_such_fact" in bad["error"] and "duplicate rewrite rule" not in bad["error"]


def test_query_outside_a_closed_context_needs_the_qualified_name_and_inside_an_open_one_does_not():
    async def body(c, sid, lease):
        await c.load_document(sid, LOCALE_CLOSED, report=True, lease_id=lease)
        closed = [await c.diagnostic(sid, q, lease_id=lease) for q in ("thm dd_pos", "thm lc.dd_pos")]
        await c.load_document(sid, LOCALE_OPEN, report=True, lease_id=lease)
        opened = await c.diagnostic(sid, "thm dd_pos", lease_id=lease)
        return closed, opened

    (unqualified, qualified), opened = _run(body)
    assert unqualified["success"] is False and "Undefined fact" in unqualified["error"]
    assert qualified["success"] is True
    assert opened["success"] is True

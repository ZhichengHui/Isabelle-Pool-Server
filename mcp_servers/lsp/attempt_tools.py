"""isabelle_multi_attempt of the LSP MCP server (efficiency plan stage C).

The candidates run IN THE FILE'S OWN SESSION (``POST /api/v1/sessions/{id}/attempt``):
each is an overlay query on the last command that ends before ``line`` and continues
from its result state, so no new session is started and the file text before ``line``
is not checked again. The server runs as many candidates at once as the session has
worker threads (4 by default) and queues the rest.

Fallback (a fresh scratch session, as before 2026-11-24; no warm spares any more) only when
the server cannot run a candidate in the file session:
- ``multiline_command``: ``line`` is inside a command that starts before it;
- ``no_host`` / ``host_end``: no command ends before ``line`` (it is in the header), or
  that command is the theory's ``end``;
- ``candidate_command``: the candidate contains ``end``, ``theory`` or a command that
  loads files (``ML_file`` and the like).

Both paths give the same per-candidate result (``candidate_result``) from a report of
"file text before ``line`` + candidate" in the shape of a load_document report.
Registered on the shared ``mcp`` from server.py when app.py imports this module."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx

from ..common import dump_json as _j
from ..common import translate_http_error
from .config import Config
from .pool import attempt_prefix, file_context, master_dir_of
from .reminders import add_reminder
from .server import TOOLS, _bound_synced, pool, synced_tool
from .verdict import candidate_verdict


def candidate_result(candidate: str, line: int, rep: Dict[str, Any],
                     subgoals: Optional[List[str]], budget: float) -> Dict[str, Any]:
    """The tool's entry for one candidate from the report of "text before ``line`` +
    candidate" (``commands`` with ``node_line``/``line``, ``sorry_lines`` of the whole text)."""
    bad, prefix_bad = [], []
    for cmd in rep.get("commands", []) or []:
        if cmd.get("status") in ("failed", "running"):
            msgs = cmd.get("messages") or []
            errors = [m for m in msgs if m.get("sev") == "error"] or msgs
            entry = {
                "kind": cmd.get("kind"),
                "status": cmd.get("status"),
                "messages": "\n".join(m.get("text", "") or "" for m in errors),
            }
            file_line = cmd.get("node_line") or cmd.get("line") or 0
            if file_line >= line:
                bad.append({"candidate_line": file_line - (line - 1), **entry})
            else:
                prefix_bad.append({"line": file_line, **entry})
    sorry_lines = rep.get("sorry_lines")
    if sorry_lines is None:  # a backend without the whole-text list
        used_sorry, prefix_used_sorry = rep.get("used_sorry"), False
    else:
        used_sorry = any(n >= line for n in sorry_lines)
        prefix_used_sorry = any(n < line for n in sorry_lines)
    result: Dict[str, Any] = {
        "candidate": candidate,
        "verdict": candidate_verdict(rep, bad, prefix_bad, used_sorry),
        "success": rep.get("success"),
        "proof_open": rep.get("proof_open"),
        "pending_qed": rep.get("pending_qed"),
        "used_sorry": used_sorry,
        "timed_out": rep.get("timed_out"),
        "failed": bad,
    }
    add_reminder(result, rep.get("timed_out"), budget, "this candidate")
    if prefix_used_sorry:
        result["prefix_used_sorry"] = True
    if prefix_bad:
        result["prefix_failed"] = prefix_bad
    if subgoals is not None:
        result["subgoals"] = subgoals
    return result


def _split_goals(goals: Optional[List[Any]]) -> Optional[List[str]]:
    """One array element per line of the goals (as Isabelle prints them)."""
    if goals is None:
        return None
    return [ln for g in goals for ln in str(g).splitlines()]


def _http_error(candidate: str, e: httpx.HTTPStatusError) -> Dict[str, Any]:
    # A memory 503 becomes the structured candidate: error=memory_pressure + advice (review
    # d2bc679 B1). owner=None: this server is single-agent, so "owned" means every entry.
    return {
        "candidate": candidate, "verdict": "error", "elapsed_s": None, "cancelled_memory": False,
        **translate_http_error(e, pool.registry, None, tools=TOOLS, idle_s=Config.ADVICE_IDLE_S),
    }


def _from_session(entry: Dict[str, Any], line: int, budget: float) -> Dict[str, Any]:
    """The tool's entry for a candidate the file session ran."""
    rep = entry.get("report") or {}
    cancelled = entry.get("cancelled")
    result = candidate_result(entry.get("candidate", ""), line, rep,
                              _split_goals(rep.get("subgoals")), budget)
    result["elapsed_s"] = entry.get("elapsed_s")
    result["cancelled_memory"] = cancelled == "memory"
    if cancelled:
        result["verdict"] = "error"
        result["error"] = ("cancelled by the server's memory guard: while it ran, the "
                           "session's ML heap in use exceeded 75% of its limit; use a step that "
                           "needs less memory, do not retry it unchanged" if cancelled == "memory"
                           else f"cancelled: {cancelled}")
    return result


async def _on_scratch(binding: Any, text: str, line: int, candidates: List[str],
                      budget: float) -> List[Dict[str, Any]]:
    """The fallback: each candidate on a fresh scratch session ("text before ``line`` +
    candidate" loaded as a whole document), at most MAX_PARALLEL at once."""
    prefix = attempt_prefix(text, line)
    c = await pool.client()
    master_dir = master_dir_of(binding.file_path)
    imports, field, session_dirs = await file_context(c, text, binding.file_path,
                                                      binding.heap_session)
    key = pool.scratch_key(binding.task_group, binding.heap_session, imports, field,
                           session_dirs, binding.import_digest)
    sem = asyncio.Semaphore(max(1, min(Config.MAX_PARALLEL, len(candidates))))

    async def one(candidate: str) -> Dict[str, Any]:
        async with sem:
            loop = asyncio.get_running_loop()
            start = loop.time()
            try:
                # The bracket ALWAYS returns the scratch slot (also on cancellation)
                # — see LspPool.scratch_session (Bug 17 / audit MCP-1).
                async with pool.scratch_session(key) as (sid, lease):
                    rep_result = await c.load_document(
                        sid, prefix + candidate, report=True,
                        timeout=budget, lease_id=lease, master_dir=master_dir)
                    rep = rep_result.get("report") or {}
                    subgoals = None
                    if rep.get("proof_open") or rep.get("pending_qed"):
                        try:
                            state = await c.get_proof_state(sid, lease_id=lease)
                            subgoals = _split_goals(state.get("subgoals") or [])
                        except Exception:  # noqa: BLE001  (the goals are an extra, not the verdict)
                            subgoals = None
                result = candidate_result(candidate, line, rep, subgoals, budget)
                result["elapsed_s"] = round(loop.time() - start, 3)
                result["cancelled_memory"] = False
                return result
            except httpx.HTTPStatusError as e:
                return _http_error(candidate, e)
            except Exception as e:  # noqa: BLE001
                return {"candidate": candidate, "verdict": "error", "elapsed_s": None,
                        "cancelled_memory": False, "error": f"{type(e).__name__}: {e}"}

    return list(await asyncio.gather(*(one(cand) for cand in candidates)))


@synced_tool()
async def isabelle_multi_attempt(
    file_path: str, line: int, candidates: List[str], timeout: Optional[float] = None,
) -> str:
    """Try several proof-step candidates at a position, in the file's own session — the
    file and its proof state are never modified.

    `line` (1-based) is the first line of the file text that the candidate REPLACES: the
    candidate takes the place of line `line` and of every line after it, and the rest of the
    file is not part of the check, so the candidate must hold all the text from `line` on
    that the check needs. To test a step in place of a `sorry`, give the `sorry`'s line and
    the step; to test the next step of an open proof, give the line after its last command.
    The candidate continues from the state after the last command that ends before `line`
    (the file before `line` is not checked again): a `qed` that follows `line` in the file
    is not there, so a candidate that closes the goals leaves the proof open (`pending_qed`).
    `line` is 1 to the number of lines of the file plus 1; the number of lines plus 1
    appends the candidate after the end of the file (after a final `end` a candidate has no
    theory to run in), any other value is an error. Candidates run in parallel (4 at once by
    default, the rest queued), each with its own time limit `timeout` (seconds, default 30,
    at most 300: a larger value runs at 300 and the result says so). A candidate at a line
    inside a command, in the theory header, or containing `end`, `theory` or a file-loading
    command is checked on a fresh scratch session instead (slower; `checked_on` says so).
    The result is indented JSON, one entry per candidate. Per candidate:
    - verdict: the field that says whether the candidate can be used. `ok`: it ran without
      error or timeout and without a `sorry` of its own (goals may remain, see
      proof_open/subgoals); `failed`: an error in the candidate, or the check did not
      succeed; `timed_out`; `used_sorry`; `prefix_failed`: a command before `line` failed,
      so the candidate's result is not reliable: fix the prefix first; `error`: the
      candidate could not be run (see error);
    - success / proof_open / pending_qed / timed_out for the whole text (file part
      before `line` plus candidate); proof_open: goals remain after the candidate;
      pending_qed: its goals are closed and only the closing `qed`/`done` is missing;
    - used_sorry: a `sorry` (or `oops`) of the CANDIDATE was executed;
      prefix_used_sorry: one of the file text before `line` was (the key is there
      only when it is true);
    - failed: the failed or still-running commands of the candidate with their
      complete error messages (warnings are not included); `candidate_line` 1 is
      file line `line`. A failed or running command before `line` is listed in
      prefix_failed with its file `line`;
    - subgoals (only when proof_open or pending_qed): the goals the candidate
      leaves, so the next step can be chosen without writing the candidate into the
      file: the goals as Isabelle prints them, one array element per line (`1. …`,
      `2. …`; continuation lines are indented);
    - elapsed_s: the candidate's own running time; cancelled_memory: true when the
      server's memory protection stopped it (verdict `error`). MEMORY: a
    candidate result with error memory_pressure, or a memory_reminder on the
    answer, means the prover server is short of memory — call
    isabelle_release_idle(), then retry; if the advice says the memory is
    held by others, stop and tell the user.
    """
    binding, _ = await _bound_synced(file_path)
    text = binding.cached_text or ""
    n_lines = len(text.splitlines())
    if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= n_lines + 1:
        return _j({
            "error": "line_out_of_range",
            "detail": f"line must be between 1 and {n_lines + 1}: the file has {n_lines} lines "
                      f"and line {n_lines + 1} appends the candidate at its end (got {line!r})",
        })
    budget = float(timeout or Config.ATTEMPT_TIMEOUT)
    results: List[Optional[Dict[str, Any]]] = [None] * len(candidates)
    scratch: Dict[int, str] = {}  # candidate index -> why it goes to a scratch session
    note = None
    try:
        ans = await pool.call(binding, lambda c, sid: c.attempt(
            sid, line, candidates, timeout_s=budget, lease_id=binding.lease_id))
    except httpx.HTTPStatusError as e:
        return json.dumps([_http_error(cand, e) for cand in candidates],
                          indent=1, ensure_ascii=False, default=str)
    if ans.get("timeout_capped"):
        note = ans.get("note")
        budget = float(ans.get("timeout_s") or budget)
    if ans.get("fallback"):
        scratch = {i: ans["fallback"] for i in range(len(candidates))}
    else:
        for i, entry in enumerate(ans.get("candidates") or []):
            if i >= len(candidates):
                break
            if entry.get("fallback"):
                scratch[i] = f"{entry['fallback']}: {entry.get('command')}"
            else:
                results[i] = _from_session(entry, line, budget)
    if scratch:
        order = sorted(scratch)
        done = await _on_scratch(binding, text, line, [candidates[i] for i in order], budget)
        for i, result in zip(order, done):
            result["checked_on"] = f"scratch session ({scratch[i]})"
            results[i] = result
    out = [r if r is not None else {"candidate": candidates[i], "verdict": "error",
                                    "error": "no result from the server", "elapsed_s": None,
                                    "cancelled_memory": False}
           for i, r in enumerate(results)]
    if note:
        for r in out:
            r["timeout_note"] = note
    return json.dumps(out, indent=1, ensure_ascii=False, default=str)

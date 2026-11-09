"""Isabelle Pool Server LSP-like MCP server (file-sync workflow).

Tools wrap PoolAsyncClient only — no direct HTTP, no server-core edits.
State is keyed by canonical FILE PATH (the agent never sees session ids).

SYNC MODEL (copied buffer): the MCP never writes files. Every file-scoped tool
re-reads the file from disk first; if it changed on disk since the last sync,
the new text is pushed to the file's session via load_document(report=true)
before answering. Execution tools (multi_attempt/run_code) run on WARM SCRATCH
sessions and never touch the file session.

POSITIONS: all lines/columns are 1-based; columns are UTF-16 units (LSP
convention, same as editors and the server's report ranges).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx

from ..common import dump_json as _j
from ..common import translate_http_error
from .config import Config
from .pool import attempt_prefix, field_for, header_fields, header_text
from .verdict import candidate_verdict

from . import scratch_spare
from . import fill_tools  # noqa: F401  (registers the fill_sorries tools on `mcp`)
from . import memory_tools  # noqa: F401  (registers the memory tools on `mcp`)
from .server import ALWAYS_LOAD, TOOLS, _attach_still_running, _bound_synced, mcp, pool, synced_tool, tool  # noqa: F401


# ------------------------------------------------------------------ lifecycle


@tool(meta=ALWAYS_LOAD)
async def isabelle_open(
    file_path: str, task_group: Optional[str] = None,
    heap_session: Optional[str] = None, label: Optional[str] = None,
) -> str:
    """Bind a .thy file to a proof session (auto-created on first tool use with
    defaults — call explicitly to choose a task group / heap).

    task_group: heap-pool group (default 'default'). heap_session: name of a
    READY heap (build one first with isabelle_build_heap) whose theories the
    file imports qualified (e.g. `imports "MySession.Bar"`). The file is synced
    immediately; the response carries the initial report summary.
    """
    binding = await pool.get_binding(file_path, task_group, heap_session, label)
    reloaded = await pool.sync(binding)
    scratch_spare.schedule_for_binding(pool, binding)
    rep = binding.last_report or {}
    return _attach_still_running(_j({
        "opened": binding.file_path,
        "task_group": binding.task_group,
        "heap_session": binding.heap_session,
        "synced": reloaded,
        "success": rep.get("success"),
        "proof_open": rep.get("proof_open"),
        "commands": len(rep.get("commands", []) or []),
        "error": rep.get("error"),
    }), file_path)


@tool()
async def isabelle_close(file_path: str, destroy: bool = False) -> str:
    """Release the file's session and unbind. With destroy=true (or the
    ISABELLE_MCP_LSP_CLOSE_DESTROYS env), the session is torn down immediately
    instead of released warm back to the pool — the sanctioned way to free a
    multi-GB session (do this when a MEMORY REMINDER or memory_pressure advice
    asks you to free memory). Reopening the file afterwards rebinds transparently."""
    destroyed = destroy or Config.CLOSE_DESTROYS
    closed = await pool.close_binding(file_path, destroy=destroyed)
    return _j({"closed": closed, "destroyed": destroyed, "file": file_path})


@tool()
async def isabelle_sync(file_path: str) -> str:
    """Force a disk → session re-sync check. Normally implicit: every file-scoped
    tool syncs before answering. Returns whether a reload happened and the
    resulting report summary."""
    binding = await pool.get_binding(file_path)
    reloaded = await pool.sync(binding)
    rep = binding.last_report or {}
    return _attach_still_running(_j({
        "synced": reloaded,
        "success": rep.get("success"),
        "proof_open": rep.get("proof_open"),
        "error": rep.get("error"),
        "stuck_line": rep.get("timed_out") and rep.get("commands") and
            next((c.get("line") for c in rep.get("commands", []) if c.get("status") == "running"), None),
    }), file_path)


# ------------------------------------------------------------- read-only tools

def _file_used_sorry(rep: Dict[str, Any]) -> Optional[bool]:
    """Whether the file's last report shows an executed `sorry`/`oops`: the backend lists their
    lines for the WHOLE file (`sorry_lines`), whereas `used_sorry` covers only the commands the
    last (possibly incremental) check re-processed."""
    lines = rep.get("sorry_lines")
    return rep.get("used_sorry") if lines is None else bool(lines)


@synced_tool(meta=ALWAYS_LOAD)
async def isabelle_diagnostic_messages(file_path: str, severity: Optional[str] = None) -> str:
    """Per-command diagnostics of the file as the prover sees it: for every
    command with an error/warning message — its line, column range, kind,
    status (ok/failed/running/unprocessed), severity, and text.

    severity: 'error' | 'warning' | None (both).

    used_sorry is true iff a `sorry` (or `oops`) command of the file was executed — it
    covers the whole file, also when the last edit re-checked only the end of it. A
    `sorry` in a comment, or in a proof that was not reached, does not count. Automation
    that emits long warnings (such as `crush_base`) makes the full list long: pass
    severity='error' to read only the errors. """
    binding, _ = await _bound_synced(file_path)
    rep = binding.last_report or {}
    out = []
    for cmd in rep.get("commands", []) or []:
        rng = cmd.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        for msg in cmd.get("messages") or []:
            sev = msg.get("sev")
            if severity and sev != severity:
                continue
            out.append({
                "line": cmd.get("line"),
                "node_line": cmd.get("node_line"),
                "col": start.get("col"),
                "end_line": end.get("line"),
                "end_col": end.get("col"),
                "kind": cmd.get("kind"),
                "status": cmd.get("status"),
                "severity": sev,
                "text": msg.get("text"),
            })
    result: Dict[str, Any] = {
        "messages": out,
        "success": rep.get("success"),
        "proof_open": rep.get("proof_open"),
        "used_sorry": _file_used_sorry(rep),
    }
    if not rep:
        result["note"] = "no report yet — call isabelle_sync first"
    return _j(result)


@synced_tool(meta=ALWAYS_LOAD)
async def isabelle_goal(file_path: str, line: int) -> str:
    """Goal state before/after the command containing `line` (1-based):
    {found, command, goals_before, goals_after}. The right call before writing
    the next proof step.

    The command is the one that STARTS at the first non-blank character of the line (an
    indented `apply` is found on its own line); a line inside a multi-line command, a blank
    line or a comment maps to the command before it, and `command.range` says which one it
    is. goals_before is the proof state after the previous command, goals_after the state
    after this one — each one text with all subgoals, empty outside a proof and for a
    command that failed. If the server does not record proof states (it was started with
    ISABELLE_SHOW_STATES=false) the result carries an error saying so instead of empty
    goals.

    To see the goals a step has to prove, ask the line of that step (an `apply`, a `by`, a
    `sorry`): its goals_before are the goals still open. Do not ask the `qed` or `done` that
    ends the proof: after a `sorry` no goals are left there, and the goals_before of a `qed`
    are the state of the proof block (the `this:` facts and the goal it closes). """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.goals_at_line(sid, line, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_command_at_line(file_path: str, line: int) -> str:
    """The command containing `line`: {found, kind, source, range} — jEdit cursor
    semantics (comment/blank lines map to the preceding command). """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.command_at_line(sid, line, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_proof_state(file_path: str) -> str:
    """Tip proof state of the synced file: subgoals, proof_finished, pending_qed.
    """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.get_proof_state(sid, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_source(file_path: str) -> str:
    """The theory source as the prover sees it (post-sync). """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.get_source(sid, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_query(file_path: str, command: str) -> str:
    """Run ONE read-only Isabelle query command against the file's synced state
    and return its output. Allowed command families (enforced server-side):
    thm / term / prop / typ / prf / full_prf, find_theorems / find_consts /
    find_*, and any print_* inspector (print_theorems, print_facts,
    print_statement, print_simpset, ...). Code-executing / IO commands (ML,
    setup, *_file, ...) are rejected. Transient: the proof script is untouched.
    Only the query's own output is returned, and `success` and `error` concern the
    query alone: messages of the other commands of the file (warnings, output) are not
    included.

    The query runs at the end of the file, after its commands: before the file's final
    `end`, or appended after the last command when the file does not end with `end`. A
    `context ... begin ... end` or locale block that is already closed at that point is
    outside the query, so name its facts and constants as the theory knows them, qualified
    by the locale or named context (`thm my_locale.my_def`, `thm_oracles my_locale.my_lemma`;
    the short name gives `Undefined fact`); a block still open at the end of the file is
    inside it. While a command of the file is still running (`still_running`), the
    query waits for it.
    """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.diagnostic(sid, command, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_local_facts(file_path: str) -> str:
    """Facts visible in the current proof context at the file tip
    (empty outside a proof). """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.get_local_facts(sid, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_global_facts(file_path: str, limit: int = 100) -> str:
    """Theory-level facts visible at the file tip, sorted by name, capped at
    `limit`. """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.get_global_facts(sid, limit=limit, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_hover_info(file_path: str, line: int, column: int) -> str:
    """Hover info for the symbol at (line, column): entity kind + type/statement,
    e.g. `constant "List.list.hd" :: nat list ⇒ nat`, `fact "My.thy.lemma"`,
    `command "lemma"`. {found, range, contents}. """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.hover_at(sid, line, column, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_definition(file_path: str, line: int, column: int) -> str:
    """Go-to-definition for the symbol at (line, column). Targets are file
    positions (kind=file) for heap/distribution entities — the file may live in
    the container (e.g. /opt/isabelle/src/HOL/...) or the project dir — or
    in-node line ranges (kind=node) for entities defined in this file.
    """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.definition_at(sid, line, column, lease_id=binding.lease_id)))


@synced_tool()
async def isabelle_sledgehammer(
    file_path: str, line: Optional[int] = None, subgoal: int = 1, timeout_s: int = 30,
) -> str:
    """Sledgehammer the open goal — at the file tip (no `line`) or at a specific
    1-based `line` (positioned; optional 1-based `subgoal`). Returns proof-method
    suggestions; paste a suggestion verbatim into the file (or test it first
    with isabelle_multi_attempt). {found, results} or {found:false, error:'no
    open goal at line N'}. The server bounds concurrent sledgehammers globally.
    """
    binding, _ = await _bound_synced(file_path)
    if line is None:
        return _j(await pool.call(
            binding, lambda c, sid: c.sledgehammer(sid, timeout_s=timeout_s, lease_id=binding.lease_id)))
    return _j(await pool.call(
        binding,
        lambda c, sid: c.sledgehammer_at(sid, line, subgoal=subgoal, timeout_s=timeout_s,
                                         lease_id=binding.lease_id)))


# ------------------------------------------------------- checkpoints / history

@synced_tool()
async def isabelle_checkpoint(file_path: str) -> str:
    """Save a checkpoint of the file session's proof state; returns
    {checkpoint_id, timestamp}. """
    binding, _ = await _bound_synced(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.save_checkpoint(sid, lease_id=binding.lease_id)))


@tool()
async def isabelle_restore(file_path: str, checkpoint_id: int) -> str:
    """Restore a previously saved checkpoint on the file session. """
    binding = await pool.get_binding(file_path)
    return _j(await pool.call(
        binding,
        lambda c, sid: c.restore_checkpoint(sid, checkpoint_id, lease_id=binding.lease_id)))


@tool()
async def isabelle_rollback(file_path: str) -> str:
    """Roll back the most recent edit on the file session. NOTE: a failed
    load/attempt never persists on the file session — rollback is only for
    retracting state created by checkpoints/exploration past the file tip.
    """
    binding = await pool.get_binding(file_path)
    try:
        return _j(await pool.call(
            binding, lambda c, sid: c.rollback(sid, lease_id=binding.lease_id)))
    except Exception as e:  # noqa: BLE001
        return _j({"success": False, "error": str(e)})


@tool()
async def isabelle_history(file_path: str) -> str:
    """Command history of the file session (commands executed since creation)."""
    binding = await pool.get_binding(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.get_history(sid, lease_id=binding.lease_id)))


@tool()
async def isabelle_last_report(file_path: str) -> str:
    """The retained report of the most recent sync/verification of this file
    (per-command status with ranges). """
    binding = await pool.get_binding(file_path)
    return _j(await pool.call(
        binding, lambda c, sid: c.get_last_report(sid, lease_id=binding.lease_id)))


# ------------------------------------------------------------ scratch execution

@synced_tool()
async def isabelle_multi_attempt(
    file_path: str, line: int, candidates: List[str], timeout: Optional[float] = None,
) -> str:
    """Try several proof-step candidates at a position, each on an ISOLATED warm
    scratch session — the file and its session are never modified.

    `line` (1-based) is the first line of the file text that the candidate REPLACES: the
    candidate takes the place of line `line` and of every line after it, and the rest of the
    file is not part of the check, so the candidate must hold all the text from `line` on
    that the check needs. To test a step in place of a `sorry`, give the `sorry`'s line and
    the step; to test the next step of an open proof, give the line after its last command.
    The tool verifies the file's text BEFORE `line` with the candidate appended (per-command
    report): a `qed` that follows `line` in the file is not there, so a candidate that closes
    the goals leaves the proof open (`pending_qed`). `line` is 1 to the number of lines of
    the file plus 1; the number of lines plus 1 appends the candidate after the end of the
    file (after a final `end` a candidate has no theory to run in), any other value is an
    error. Candidates run concurrently (bounded by ISABELLE_MCP_LSP_MAX_PARALLEL and the
    scratch pool size). The result is indented JSON, one entry per candidate. Per candidate:
    - verdict: the field that says whether the candidate can be used. `ok`: it ran without
      error or timeout and without a `sorry` of its own (goals may remain, see
      proof_open/subgoals); `failed`: an error in the candidate, or the check did not
      succeed; `timed_out`; `used_sorry`; `prefix_failed`: a command before `line` failed,
      so the candidate's result is not reliable: fix the prefix first; `error`: the
      candidate could not be run (see error);
    - success / proof_open / pending_qed / timed_out for the whole text (file part
      before `line` plus candidate);
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
      `2. …`; continuation lines are indented). MEMORY: a
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
    prefix = attempt_prefix(text, line)
    c = await pool.client()
    imports, suggested = await header_fields(c, text)
    key = pool.scratch_key(
        binding.task_group, binding.heap_session, imports, field_for(suggested))
    budget = timeout or Config.ATTEMPT_TIMEOUT
    cap = max(1, min(Config.MAX_PARALLEL, len(candidates)))
    sem = asyncio.Semaphore(cap)

    async def try_one(candidate: str) -> Dict[str, Any]:
        async with sem:
            try:
                # The bracket ALWAYS returns the scratch slot (also on cancellation)
                # — see LspPool.scratch_session (Bug 17 / audit MCP-1).
                async with pool.scratch_session(key) as (sid, lease):
                    rep_result = await c.load_document(
                        sid, prefix + candidate, report=True,
                        timeout=budget, lease_id=lease)
                    rep = rep_result.get("report") or {}
                    subgoals = None
                    if rep.get("proof_open") or rep.get("pending_qed"):
                        try:
                            state = await c.get_proof_state(sid, lease_id=lease)
                            subgoals = [ln for g in state.get("subgoals") or [] for ln in str(g).splitlines()]
                        except Exception:  # noqa: BLE001  (the goals are an extra, not the verdict)
                            subgoals = None
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
                result = {
                    "candidate": candidate,
                    "verdict": candidate_verdict(rep, bad, prefix_bad, used_sorry),
                    "success": rep.get("success"),
                    "proof_open": rep.get("proof_open"),
                    "pending_qed": rep.get("pending_qed"),
                    "used_sorry": used_sorry,
                    "timed_out": rep.get("timed_out"),
                    "failed": bad,
                }
                if prefix_used_sorry:
                    result["prefix_used_sorry"] = True
                if prefix_bad:
                    result["prefix_failed"] = prefix_bad
                if subgoals is not None:
                    result["subgoals"] = subgoals
                return result
            except httpx.HTTPStatusError as e:
                # A memory 503 (the scratch create is the gated call) becomes the
                # structured candidate: error=memory_pressure + advice (review d2bc679 B1).
                # owner=None: this server is single-agent, so "owned" means every entry;
                # revisit here if per-connection ownership (the stepwise model) arrives.
                return {
                    "candidate": candidate,
                    "verdict": "error",
                    **translate_http_error(
                        e, pool.registry, None, tools=TOOLS, idle_s=Config.ADVICE_IDLE_S
                    ),
                }
            except Exception as e:  # noqa: BLE001
                return {"candidate": candidate, "verdict": "error",
                        "error": f"{type(e).__name__}: {e}"}

    results = await asyncio.gather(*(try_one(cand) for cand in candidates))
    scratch_spare.ensure(pool, key, text)  # top the warm, loaded spares up again
    return json.dumps(list(results), indent=1, ensure_ascii=False, default=str)


@tool()
async def isabelle_run_code(
    chunk: str, imports: Optional[List[str]] = None,
    task_group: Optional[str] = None, heap_session: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """Run an independent Isar snippet on a warm scratch session (never touches
    any file session). A chunk starting with `theory` is treated as a full .thy
    source; anything else runs as the body of a scratch theory importing
    `imports` (default ["Main"]). heap_session runs it in that heap's import
    context. Returns {success, output, error, failed commands}."""
    c = await pool.client()
    if imports:
        # An explicit list goes through the same server parse as a header, so
        # the session image is chosen by one definition (suggested_field).
        _, suggested = await header_fields(c, header_text(imports))
    else:
        imports, suggested = await header_fields(c, chunk)
        imports = imports or ["Main"]
    key = pool.scratch_key(task_group, heap_session, imports, field_for(suggested))
    budget = timeout or Config.ATTEMPT_TIMEOUT
    try:
        # The bracket ALWAYS returns the scratch slot (also on cancellation).
        async with pool.scratch_session(key) as (sid, lease):
            if chunk.lstrip().startswith("theory"):
                result = await c.load_document(
                    sid, chunk, report=True, timeout=budget, lease_id=lease)
            else:
                result = await c.load_document(
                    sid, chunk, thy_name="Scratch", imports=imports or ["Main"],
                    report=True, timeout=budget, lease_id=lease)
        rep = result.get("report") or {}
        bad = [
            {"line": cmd.get("line"), "kind": cmd.get("kind"), "status": cmd.get("status"),
             "messages": "; ".join((m.get("text", "") or "")[:200]
                                   for m in (cmd.get("messages") or []))}
            for cmd in rep.get("commands", []) or []
            if cmd.get("status") in ("failed", "running")
        ]
        return _j({
            "success": result.get("success"),
            "output": result.get("output"),
            "error": result.get("error"),
            "proof_open": rep.get("proof_open"),
            "used_sorry": rep.get("used_sorry"),
            "failed": bad,
        })
    except (
        httpx.HTTPStatusError
    ) as e:  # owner=None: single-agent server (see multi_attempt)
        return _j(
            {
                "success": False,
                **translate_http_error(
                    e, pool.registry, None, tools=TOOLS, idle_s=Config.ADVICE_IDLE_S
                ),
            }
        )
    except Exception as e:  # noqa: BLE001
        return _j({"success": False, "error": f"{type(e).__name__}: {e}"})


# ------------------------------------------------------------------ heap tools


@tool()
async def isabelle_build_heap(
    task_group: str, project: str, session_name: Optional[str] = None,
) -> str:
    """Build (or rebuild) the verified heap for a project dir (its top-level
    .thy files) under a task group — `isabelle build -b`; build passing IS the
    verification gate. Long-running. Then open files with
    isabelle_open(file_path, task_group=..., heap_session=<name>)."""
    c = await pool.client()
    try:
        return _j(await c.heap_build(task_group, project, session_name=session_name))
    except Exception as e:  # noqa: BLE001
        status = getattr(getattr(e, "response", None), "status_code", None)
        detail = ""
        try:
            detail = e.response.json().get("detail", "")  # type: ignore[union-attr]
        except Exception:
            detail = str(e)
        return _j({"error": detail, "status_code": status})


@tool()
async def isabelle_heap_status(task_group: Optional[str] = None, project: Optional[str] = None) -> str:
    """Heap status. With project (+task_group): the full manifest (theory
    files with sha256/mtime, ROOT text, fingerprint, status, build log tail).
    Otherwise: the pool listing (one group, or all groups) PLUS
    `available_heaps` — every base session image on disk (HOL-Analysis,
    distribution heaps, pool-built), so you can see what sessions can start
    from before naming heap_session/field anywhere."""
    c = await pool.client()
    if project is not None and task_group is not None:
        return _j(await c.get_heap(task_group, project))
    listing = await c.list_heaps(task_group)
    try:
        listing["available_heaps"] = (await c.list_available_heaps()).get("heaps", [])
    except Exception:  # noqa: BLE001 -- pool listing alone is still useful
        listing["available_heaps"] = None
    return _j(listing)


def main() -> None:
    if Config.TRANSPORT == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

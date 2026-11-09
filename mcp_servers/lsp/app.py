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

@synced_tool(meta=ALWAYS_LOAD)
async def isabelle_diagnostic_messages(file_path: str, severity: Optional[str] = None) -> str:
    """Per-command diagnostics of the file as the prover sees it: for every
    command with an error/warning message — its line, column range, kind,
    status (ok/failed/running/unprocessed), severity, and text.

    severity: 'error' | 'warning' | None (both). """
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
        "used_sorry": rep.get("used_sorry"),
    }
    if not rep:
        result["note"] = "no report yet — call isabelle_sync first"
    return _j(result)


@synced_tool(meta=ALWAYS_LOAD)
async def isabelle_goal(file_path: str, line: int) -> str:
    """Goal state before/after the command containing `line`:
    {found, command, goals_before, goals_after}. The right call before writing
    the next proof step. """
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
    The query runs at the end of the file, after its commands: while a command of
    the file is still running (`still_running`), the query waits for it.
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

    Takes the file's text BEFORE `line` (1-based), appends each candidate, and
    verifies it (per-command report). Candidates run concurrently (bounded by
    ISABELLE_MCP_LSP_MAX_PARALLEL and the scratch pool size). Per candidate:
    success / proof_open / pending_qed / used_sorry / timed_out and the failed
    or still-running commands with messages. Lines in `failed` are
    candidate-relative (candidate line 1 = file line `line`). MEMORY: a
    candidate result with error memory_pressure, or a memory_reminder on the
    answer, means the prover server is short of memory — call
    isabelle_release_idle(), then retry; if the advice says the memory is
    held by others, stop and tell the user.
    """
    binding, _ = await _bound_synced(file_path)
    text = binding.cached_text or ""
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
                bad = []
                for cmd in rep.get("commands", []) or []:
                    if cmd.get("status") in ("failed", "running"):
                        msgs = "; ".join(
                            (m.get("text", "") or "")[:200] for m in (cmd.get("messages") or []))
                        bad.append({
                            "candidate_line": (cmd.get("line") or 0) - (line - 1),
                            "kind": cmd.get("kind"),
                            "status": cmd.get("status"),
                            "messages": msgs,
                        })
                return {
                    "candidate": candidate,
                    "success": rep.get("success"),
                    "proof_open": rep.get("proof_open"),
                    "pending_qed": rep.get("pending_qed"),
                    "used_sorry": rep.get("used_sorry"),
                    "timed_out": rep.get("timed_out"),
                    "failed": bad,
                }
            except httpx.HTTPStatusError as e:
                # A memory 503 (the scratch create is the gated call) becomes the
                # structured candidate: error=memory_pressure + advice (review d2bc679 B1).
                # owner=None: this server is single-agent, so "owned" means every entry;
                # revisit here if per-connection ownership (the stepwise model) arrives.
                return {
                    "candidate": candidate,
                    **translate_http_error(
                        e, pool.registry, None, tools=TOOLS, idle_s=Config.ADVICE_IDLE_S
                    ),
                }
            except Exception as e:  # noqa: BLE001
                return {"candidate": candidate, "error": f"{type(e).__name__}: {e}"}

    results = await asyncio.gather(*(try_one(cand) for cand in candidates))
    scratch_spare.ensure(pool, key, text)  # top the warm, loaded spares up again
    return _j(list(results))


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

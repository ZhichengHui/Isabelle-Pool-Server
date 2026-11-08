"""The LSP MCP server object and the plumbing every tool module shares:
``mcp``, ``pool``, the memory-protocol tool names, the per-tool guard and the
``tool`` / ``synced_tool`` decorators. Tool modules (app.py, memory_tools.py)
import from here; nothing here imports them (split out of app.py at the
source-size gate — review d2bc679 S3).
"""

from __future__ import annotations

import functools
import json
from typing import Any, Awaitable, Callable, TypeVar

from mcp.server.fastmcp import FastMCP

from ..common import ToolNames, dump_json, make_guard
from .config import Config
from .pool import LspPool
from . import scratch_spare

_INSTRUCTIONS = f"""\
Isabelle Pool Server: checks Isabelle/HOL theory (.thy) files on warm Isabelle sessions. Use these \
tools to check proofs, read goals and find proofs while you edit a theory file. The file stays on \
disk: edit it with your own file tools; every tool that takes file_path re-reads the file and \
re-checks it before answering.

Key capabilities:
- Incremental checking: isabelle_open binds a file once; after an edit, isabelle_diagnostic_messages, \
isabelle_goal and isabelle_proof_state re-check only from the first changed command on, which is much \
faster than a batch build of the whole theory.
- Trying proofs without editing the file: isabelle_multi_attempt checks several candidate proofs at \
one line in parallel, each on its own scratch session with its own time limit.
- Finding proofs: isabelle_sledgehammer runs sledgehammer on one goal; isabelle_fill_sorries runs it \
on every sorry of the file in the background.
- Blocked checks: a check waits at most {Config.LOAD_TIMEOUT:.0f} s; if commands are still running \
then, the result carries still_running with their lines.
- Inspection: isabelle_query (thm, term, find_theorems, ...), local and global facts, definitions, \
hover information; checkpoints; memory tools (isabelle_memory_status, isabelle_release_idle)."""

mcp = FastMCP("isabelle-pool-server-lsp", instructions=_INSTRUCTIONS, host=Config.HOST, port=Config.PORT)
pool = LspPool()

# `_meta` of the tools that Claude Code should load upfront even when it defers MCP tools behind
# tool search: the ones used on nearly every turn of editing a theory.
ALWAYS_LOAD = {"anthropic/alwaysLoad": True}

# Memory protocol (plan §2.C, owner decisions D3/D4): the server reports FACTS
# (503 bodies with an `error` key, the X-Memory-State header, /api/v1/memory);
# this server words the advice and names its own tools. Every tool is wrapped:
# an HTTP error becomes structured JSON with advice; while the server is at
# warning/critical a MEMORY REMINDER is attached to results and the idle warm
# scratch sessions are closed first (the MCP frees its own before asking).
TOOLS = ToolNames(
    release_idle="isabelle_release_idle()",
    close="isabelle_close(file_path, destroy=true)",
    list_sessions="isabelle_list_sessions()",
    memory_status="isabelle_memory_status()",
    reopen="the next file-scoped call reopens it automatically",
)
_guard = make_guard(
    registry=pool.registry,
    tools=TOOLS,
    get_client=pool.client,
    owner_of=lambda args, kwargs: None,
    idle_s=Config.ADVICE_IDLE_S,
    # Retire idle warm scratch sessions by tier (review d2bc679 S1): at
    # warning only those idle >= ADVICE_IDLE_S, at critical every idle one.
    on_reminder=lambda state: pool.retire_idle_scratch(
        0.0 if state == "critical" else float(Config.ADVICE_IDLE_S)
    ),
)


_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def tool(**kw: Any) -> Callable[[_F], _F]:
    """`@mcp.tool()` + the memory guard (see TOOLS)."""

    def deco(fn: _F) -> _F:
        return mcp.tool(**kw)(_guard(fn))  # type: ignore[no-any-return]

    return deco


_SYNC_NOTE = (
    "The file is re-read from disk and re-synced to its session before "
    "answering if it changed (the MCP never writes files). Positions are "
    "1-based line/col, UTF-16 columns."
)


def synced_tool(**kw):
    """`@mcp.tool()` for file-scoped tools: appends _SYNC_NOTE to the REAL
    docstring. (A triple-quoted string + _SYNC_NOTE EXPRESSION is not a docstring — the
    function's __doc__ was None and the tool shipped with an EMPTY description,
    i.e. no instructions for the agent — caught by tests/test_mcp_tools_smoke.py.)"""

    def deco(fn):
        fn.__doc__ = (fn.__doc__ or "").rstrip() + " " + _SYNC_NOTE

        @functools.wraps(fn)
        async def with_running(*args: Any, **kwargs: Any) -> Any:
            out = await fn(*args, **kwargs)
            file_path = kwargs.get("file_path", args[0] if args else None)
            return _attach_still_running(out, file_path)

        return mcp.tool(**kw)(_guard(with_running))

    return deco


def _attach_still_running(out: Any, file_path: Any) -> Any:
    """If the file's last sync timed out with commands still running, add them
    to a JSON-object result as `still_running` (line, kind, running_for_s), so
    the agent sees which command blocks the check instead of an empty report."""
    if not isinstance(out, str) or not isinstance(file_path, str):
        return out
    binding = pool.peek_binding(file_path)
    running = pool.running_commands(binding) if binding is not None else []
    if not running:
        return out
    try:
        result = json.loads(out)
    except ValueError:
        return out
    if not isinstance(result, dict):
        return out
    result["still_running"] = running
    result["still_running_note"] = (
        f"The last check of this file stopped after {Config.LOAD_TIMEOUT:.0f} s because these "
        "commands were still running: their results are missing, and the results of other "
        "commands may be incomplete. A command that keeps running (e.g. metis or auto that "
        "does not terminate) makes every check of the file wait this long until it is changed."
    )
    return dump_json(result)


async def _bound_synced(file_path: str):
    """get_binding + sync — the preamble of every file-scoped tool."""
    binding = await pool.get_binding(file_path)
    reloaded = await pool.sync(binding)
    scratch_spare.schedule_for_binding(pool, binding)
    return binding, reloaded

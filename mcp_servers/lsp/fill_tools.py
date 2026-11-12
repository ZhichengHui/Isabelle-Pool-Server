"""fill_sorries tools of the LSP MCP server (plan 2026-10-18 §1.5, §3.3): the
agent writes a proof with ``sorry`` at every goal it cannot close, the server
runs sledgehammer on each ``sorry`` (one job per file session, tier-aware
slots, a deterministic ETA) and returns the patched text. Registered on the
shared ``mcp`` from server.py when app.py imports this module."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from ..common import dump_json as _j
from ..common.memory_advice import parse_http_error
from .config import Config
from .pool import master_dir_of
from .server import _bound_synced, pool, tool

_TERMINAL = ("done", "cancelled", "error")
_RESULT_KEYS = (
    "status",
    "n",
    "done",
    "filled",
    "queue_position",
    "parked",
    "parked_reason",
    "elapsed_s",
    "eta_s",
    "patched_text",
    "error",
    "sledgehammer_peak_mb",
)


def _rows(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """§3.3: the §3.2 rows without the command index (agents act by line)."""
    return [{k: v for k, v in r.items() if k != "index"} for r in results]


def shape(body: Dict[str, Any], continuing: bool) -> Dict[str, Any]:
    """The tool result of §3.3 from a §3.2 status body, with the advice."""
    out: Dict[str, Any] = {k: body.get(k) for k in _RESULT_KEYS}
    out["continuing"] = continuing
    out["results"] = _rows(body.get("results") or [])
    unsolved = [
        r["line"]
        for r in out["results"]
        if r.get("outcome") != "proved" or not r.get("method")
    ]
    advice: List[str] = []
    if continuing:
        advice.append(
            "The job keeps running server-side (until max_total_s); call "
            "isabelle_fill_status(file_path) to collect it, or "
            "isabelle_fill_cancel(file_path) to stop it. Other tools on this "
            "file answer `busy` while a step runs."
        )
        eta = body.get("eta_s") or {}
        if body.get("parked"):
            advice.append(
                f"The job is parked ({body.get('parked_reason')}): the server is "
                "at critical memory and runs no sledgehammer until the tier drops."
            )
        elif eta:
            advice.append(
                f"Remaining: typical {eta.get('typical')} s, worst {eta.get('worst')} s."
            )
    elif out["filled"]:
        advice.append(
            "Write patched_text to the file, then verify with "
            "isabelle_diagnostic_messages(file_path)."
        )
        if unsolved:
            advice.append(f"Still `sorry` (prove by hand): line(s) {unsolved}.")
    elif out["n"]:
        advice.append(
            f"No sorry was closed (line(s) {unsolved}); prove them by hand, or "
            "state intermediate `have` steps and call isabelle_fill_sorries again."
        )
    else:
        advice.append("The file has no `sorry`; nothing to fill.")
    budget = [
        r["line"]
        for r in out["results"]
        if r.get("outcome") == "skipped"
        and str(r.get("reason") or "").startswith("max_total_s")
    ]
    if budget and not continuing:
        why = next(
            (
                str(r.get("reason"))
                for r in out["results"]
                if str(r.get("reason") or "").startswith("max_total_s")
            ),
            "max_total_s",
        )
        advice.append(
            f"The job's max_total_s ran out before line(s) {budget} were tried ({why}): "
            "call isabelle_fill_sorries again later (a parked job waits for the server's "
            "memory tier to drop) or with a larger max_total_s."
        )
    if body.get("error"):
        advice.append(f"The job ended with an error: {body['error']}.")
    out["advice"] = advice
    return out


def _fill_error(exc: httpx.HTTPStatusError) -> Optional[Dict[str, Any]]:
    """The two fill-specific HTTP errors as agent-facing results; None for
    anything else (the guard translates those)."""
    info = parse_http_error(exc)
    if info["status"] == 409 and info["error"] == "fill_in_flight":
        return {
            "error": "fill_in_flight",
            "advice": [
                "A fill job is already running on this file: "
                "isabelle_fill_status(file_path) to collect it, "
                "isabelle_fill_cancel(file_path) to stop it."
            ],
        }
    if info["status"] == 404 and info["error"] == "no_fill_job":
        return _no_fill_job()
    return None


def _no_fill_job() -> Dict[str, Any]:
    return {
        "error": "no_fill_job",
        "advice": [
            "No fill job on this file; call isabelle_fill_sorries(file_path) first."
        ],
    }


async def _status(binding: Any) -> Dict[str, Any]:
    """One poll — through the client directly (NOT pool.call: a 404 here is
    `no_fill_job`, not a dead session, and must not rebind the file)."""
    c = await pool.client()
    pool.registry.touch(binding.session_id)
    return await c.fill_status(binding.session_id, lease_id=binding.lease_id)


@tool()
async def isabelle_fill_sorries(
    file_path: str,
    timeout_s: Optional[int] = None,
    max_total_s: Optional[float] = None,
    patience_s: Optional[float] = None,
) -> str:
    """Let the SERVER run sledgehammer on every `sorry` of the file. Protocol:
    write the proof with `sorry` at every goal you cannot close (also inside
    `proof … qed` blocks and after `apply` steps), call this tool, write the
    returned `patched_text` to the file, then verify with
    isabelle_diagnostic_messages. One job per file; each `sorry` gets
    `timeout_s` (default 30) of sledgehammer, the whole job `max_total_s`
    (default 600); the server runs the steps one at a time per file and caps
    them globally by memory tier (2 at ok, 1 at warning, parked at critical —
    `queue_position` -1 = not queued; `parked` with the reason). Result:
    {filled, n, continuing, results: [{line, host_kind, subgoals, outcome
    (proved|none|timeout|error|skipped), method, reason, per_subgoal}],
    patched_text, eta_s: {typical, worst}, queue_position, advice}. The tool
    returns after `patience_s` (default 240) with `continuing: true` and the
    partial state — the job KEEPS RUNNING server-side until max_total_s; call
    isabelle_fill_status(file_path) to collect it, isabelle_fill_cancel to
    stop it. `max_total_s` bounds the job INCLUDING time parked at critical
    memory: a job parked that long ends with its untried sorries `skipped`
    (reason `max_total_s while parked`) — call again later. While a step
    runs, other tools on this file answer `busy`."""
    binding, _ = await _bound_synced(file_path)
    text = binding.cached_text
    if text is None:
        with open(binding.file_path, encoding="utf-8") as fh:
            text = fh.read()
    budget = int(timeout_s if timeout_s is not None else Config.FILL_TIMEOUT_S)
    total = float(max_total_s if max_total_s is not None else Config.FILL_MAX_TOTAL_S)
    patience = float(patience_s if patience_s is not None else Config.FILL_PATIENCE_S)
    try:
        body = await pool.call(
            binding,
            lambda c, sid: c.fill_sorries(
                sid,
                text,
                mode="document",
                timeout_s=budget,
                max_total_s=total,
                load_timeout=Config.LOAD_TIMEOUT,
                lease_id=binding.lease_id,
                master_dir=master_dir_of(binding.file_path),
            ),
        )
    except httpx.HTTPStatusError as exc:
        known = _fill_error(exc)
        if known is None:
            raise
        return _j(known)
    started = time.monotonic()
    while body.get("status") not in _TERMINAL:
        if time.monotonic() - started >= patience:
            return _j(shape(body, continuing=True))
        await asyncio.sleep(Config.FILL_POLL_S)
        body = await _status(binding)
    return _j(shape(body, continuing=False))


@tool()
async def isabelle_fill_status(file_path: str) -> str:
    """The state of the file's fill job (after isabelle_fill_sorries returned
    `continuing: true`): the same result shape — `continuing` stays true while
    the job runs (queue_position, parked, eta_s tell why), false once it is
    done/cancelled/error, then write `patched_text` to the file and verify.
    Polling keeps the file session alive. Does not re-sync the file."""
    binding = pool.peek_binding(file_path)  # never opens (review aa96c6d S1)
    if binding is None:
        return _j(_no_fill_job())
    try:
        body = await _status(binding)
    except httpx.HTTPStatusError as exc:
        known = _fill_error(exc)
        if known is None:
            raise
        return _j(known)
    return _j(shape(body, continuing=body.get("status") not in _TERMINAL))


@tool()
async def isabelle_fill_cancel(file_path: str) -> str:
    """Stop the file's fill job: the step in flight ends within its own
    timeout_s, later `sorry`s are reported `skipped`, and the partial
    `patched_text` (the proofs found so far) is returned in the same shape.
    Also the way to free the session when the job is parked at critical."""
    binding = pool.peek_binding(file_path)  # never opens (review aa96c6d S1)
    if binding is None:
        return _j(_no_fill_job())
    c = await pool.client()
    try:
        body = await c.fill_cancel(binding.session_id, lease_id=binding.lease_id)
    except httpx.HTTPStatusError as exc:
        known = _fill_error(exc)
        if known is None:
            raise
        return _j(known)
    if body.get("status") == "none":
        return _j(_no_fill_job())
    out = shape(body, continuing=body.get("status") not in _TERMINAL)
    out["cancelled"] = bool(body.get("cancelled"))
    return _j(out)


__all__ = [
    "isabelle_fill_sorries",
    "isabelle_fill_status",
    "isabelle_fill_cancel",
    "shape",
]

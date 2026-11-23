"""What a tool result says about the heap a file's session needs, and helpers around it.

The server builds the image a session needs before starting it (server/app/services/
import_heap.py): while it builds, a session request answers HTTP 202 `building_import_heap` with
its progress. `raise_if_building` turns that answer into `HeapBuilding`, which the guard returns
as the tool's result (common/guard.py, `agent_result`): the agent reads what is being built and
how far it is, and calls a tool on the file again later. Nothing tells it to retry at once.

(Until 2026-11-23 this module advised the agent to build heaps itself with isabelle_build_heap,
or to move sibling theories into a session; the server now does that.)
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from .project_sessions import session_parents


def session_dirs_of(dirs: Sequence[str]) -> Dict[str, str]:
    """Session name -> the directory whose ROOT defines it, for the project directories `dirs`."""
    out: Dict[str, str] = {}
    for d in dirs:
        for name in session_parents([d]):
            out.setdefault(name, d)
    return out


def join_notes(*notes: Optional[str]) -> Optional[str]:
    """The notes that are there, one after the other; None when there are none."""
    present = [n for n in notes if n]
    return " ".join(present) if present else None


class HeapBuilding(Exception):
    """A session request answered 202: the image is being built. `agent_result` is the tool
    result (the guard returns it as is)."""

    def __init__(self, status: Dict[str, Any]):
        super().__init__(f"building {status.get('session')}")
        self.agent_result = building_result(status)


def building_result(status: Dict[str, Any]) -> Dict[str, Any]:
    """The tool result for a build in progress (status: the server's 202 body)."""
    name = status.get("session") or "?"
    done = status.get("sessions_built") or []
    running = status.get("sessions_running") or []
    elapsed = status.get("elapsed_s")
    parts = [f"The server is building a heap for this file's imports ({name})"]
    if running:
        parts.append(f"now building {', '.join(running)}")
    if done:
        parts.append(f"done: {', '.join(done)}")
    if elapsed is not None:
        parts.append(f"{elapsed} s so far")
    message = ("; ".join(parts) + ". The file opens on that heap once it is built, and its imports "
               "are then not checked again. Work on something else meanwhile and call a tool on "
               "this file again in a minute or two; a call before then only reports progress.")
    return {"status": "building_import_heap", "session": name, "sessions_built": done,
            "sessions_running": running, "elapsed_s": elapsed, "message": message}


def raise_if_building(resp: Dict[str, Any]) -> None:
    """Raise HeapBuilding when a session request answered "the image is being built"."""
    if isinstance(resp, dict) and resp.get("error") == "building_import_heap":
        raise HeapBuilding(resp)

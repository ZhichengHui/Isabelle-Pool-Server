"""The note a file's result carries when its session loads imported theories from source because no
usable heap contains them: either no heap was ever built for them, or the heap that contains them
is out of date (a source theory changed after the build; an out-of-date heap cannot be used).

Without it the agent only sees a slow open, or a timeout with "retry", and retries into the same
minutes-long load (agent runs 2026-11: up to 10 timeouts of 600 s per run). The server reports the
facts (POST /api/v1/select_field: `from_source`, `stale`); this module words the advice. Building
or rebuilding is advised only for sessions of the file's own project (a ROOT in its repository):
those the agent can build, with isabelle_build_heap on the directory of that ROOT. Out-of-date
heaps of other sessions (a shared library) are named, with the advice to report, not rebuild.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .project_sessions import session_parents


def _session(theory: str) -> str:
    return theory.split(".", 1)[0]


def session_dirs_of(dirs: Sequence[str]) -> Dict[str, str]:
    """Session name -> the directory whose ROOT defines it, for the project directories `dirs`."""
    out: Dict[str, str] = {}
    for d in dirs:
        for name in session_parents([d]):
            out.setdefault(name, d)
    return out


def _build_call(task_group: str, project: str, session: str) -> str:
    return (f'isabelle_build_heap(task_group="{task_group}", project="{project}", '
            f'session_name="{session}")')


def heap_note(from_source: Sequence[str], stale: Sequence[str], dirs: Sequence[str],
              task_group: str) -> Optional[str]:
    """The note for a session that loads `from_source` from source, or None when there is nothing
    the agent should know (everything comes from a heap, or only theories of sessions outside the
    project are loaded and no heap of them is out of date)."""
    if not from_source:
        return None
    own = session_dirs_of(dirs)
    theories = ", ".join(from_source)
    sessions: List[str] = list(dict.fromkeys(_session(t) for t in from_source))
    loading = (f"so the theories this file imports ({theories}) are loaded from source, which can "
               "take several minutes")
    if stale:
        heaps = ", ".join(stale)
        mine = [s for s in stale if s in own]
        note = (f"The heap {heaps} contains the theories this file imports, but it is out of "
                "date: its source theories changed after it was built, and an out-of-date heap "
                f"cannot be used, {loading}.")
        if mine:
            calls = "; ".join(_build_call(task_group, own[s], s) for s in mine)
            note += (f" Rebuild it with {calls}, then reopen this file. Rebuild it again each time "
                     "you change those theories.")
        else:
            note += (" It is not a session of this project: do not rebuild it; tell the user that "
                     "it is out of date.")
        return note
    mine = [s for s in sessions if s in own]
    if not mine:
        return None
    calls = "; ".join(_build_call(task_group, own[s], s) for s in mine)
    return (f"No heap contains the theories this file imports, {loading}. Build a heap for them "
            f"with {calls}, then reopen this file; opening it then takes under a minute. Rebuild "
            "the heap each time you change those theories.")

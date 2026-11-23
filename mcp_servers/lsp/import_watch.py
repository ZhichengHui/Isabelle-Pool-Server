"""The local theory files an open file imports, to notice when one of them changes.

A session loads the file's imports once, when it starts: an edit of an imported theory (`B.thy`
next to `A.thy`, or a theory of the project's own session) is not seen by the open file, which
keeps checking against the old version until it is closed and opened again (agent runs 2026-11:
22 manual close/reopen cycles in one run). The binding records these files with their
(mtime_ns, size) when it is made (`import_files`, `state`); every sync compares, and a change makes
the pool reopen the file on a new session (`refresh`).

Watched: imports that resolve to files of the project: a theory of the importing file's directory
(bare name, or a relative path), and `"S.T"` where S is a session defined by a ROOT of the file's
project (`session_dirs`, project_sessions.py), followed through the imports of those files; plus the
ROOT files of those sessions. Not watched: theories that come from a heap or the Isabelle
distribution (they do not change under a running session), and the file itself (it is synced
incrementally anyway).
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .heap_notes import join_notes, session_dirs_of

MAX_FILES = 300
State = Tuple[Tuple[str, Any], ...]


def _stat(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def state(files: Sequence[str]) -> State:
    """(path, (mtime_ns, size) or None) of every watched file."""
    return tuple((f, _stat(f)) for f in files)


def digest(st: State) -> str:
    """A short name of a state: the scratch sessions of a file are keyed by it, so that a warm
    one loaded with an old version of an import is never reused."""
    return hashlib.sha1(repr(st).encode()).hexdigest()[:12] if st else ""


def changed(old: State, new: State) -> List[str]:
    """The base names of the watched files whose state differs."""
    before = dict(old)
    return [os.path.basename(p) for p, s in new if before.get(p, "?") != s]


def _find_theory(root: str, theory: str, cache: Dict[Tuple[str, str], Optional[str]]) -> Optional[str]:
    """The file `<theory>.thy` under the session directory `root` (first match, sorted walk)."""
    key = (root, theory)
    if key not in cache:
        cache[key] = None
        for d, subdirs, files in os.walk(root):
            subdirs[:] = sorted(s for s in subdirs if not s.startswith("."))
            if f"{theory}.thy" in files:
                cache[key] = os.path.join(d, f"{theory}.thy")
                break
    return cache[key]


def _resolve(name: str, from_dir: str, own: Dict[str, str],
             cache: Dict[Tuple[str, str], Optional[str]]) -> Optional[str]:
    """The project file an import names, seen from `from_dir`; None for heap/distribution ones."""
    name = name.strip('"')
    if name.startswith("/"):  # a sibling as the MCP rewrote it (resolve_sibling_imports)
        path = name if name.endswith(".thy") else name + ".thy"
        return path if os.path.isfile(path) else None
    session, dot, theory = name.partition(".")
    if dot and "/" not in name and session in own:
        return _find_theory(own[session], theory, cache)
    path = os.path.normpath(os.path.join(from_dir, name)) + ".thy"
    return path if os.path.isfile(path) else None


async def import_files(client: Any, imports: Sequence[str], file_path: str,
                       session_dirs: Sequence[str]) -> List[str]:
    """The watched files of `file_path` (module docstring), in discovery order, at most
    MAX_FILES. `imports` are the file's imports as file_context gives them; the headers of the
    files found are parsed by the server (`parse_theory_header`)."""
    own = session_dirs_of(session_dirs)
    cache: Dict[Tuple[str, str], Optional[str]] = {}
    me = os.path.abspath(file_path)
    found: List[str] = []
    roots: List[str] = []
    todo: List[Tuple[str, str]] = [(i, os.path.dirname(me)) for i in imports]
    while todo and len(found) < MAX_FILES:
        name, from_dir = todo.pop(0)
        path = _resolve(name, from_dir, own, cache)
        if path is None or path == me or path in found:
            continue
        found.append(path)
        session = name.strip('"').partition(".")[0]
        if session in own and os.path.join(own[session], "ROOT") not in roots:
            roots.append(os.path.join(own[session], "ROOT"))
        try:
            with open(path, encoding="utf-8") as f:
                header = await client.parse_theory_header(f.read())
        except Exception:  # noqa: BLE001  (unreadable or unparsable: watch the file itself only)
            continue
        todo.extend((i, os.path.dirname(path)) for i in header.get("imports") or [])
    return found + roots


async def refresh(pool: Any, binding: Any, now: State) -> None:
    """An imported project theory changed since the session started: start
    a new session with the current versions and move the binding onto it. The old session is
    closed, not released (it holds the old versions); idle scratch sessions of the old
    version are closed and busy ones are closed when returned. The next result says so.
    Called by LspPool.sync() under binding.lock; the binding object stays the one callers hold."""
    names = ", ".join(changed(binding.import_state, now)) or "an imported theory"
    old_digest = binding.import_digest
    fresh = await pool._create_binding(
        binding.file_path, binding.task_group, binding.heap_session, None, release_old=False)
    await pool._safe_destroy(binding)
    for name in ("session_id", "lease_id", "import_files", "import_state", "import_digest"):
        setattr(binding, name, getattr(fresh, name))
    binding.cached_text = None
    binding.last_report = None
    binding.running_since = {}
    binding.spare_scheduled = False
    binding.heap_note = join_notes(
        f"Imported theories changed since this file was opened ({names}): it was reopened on "
        "a new session with their current versions, and checked again from the start.",
        fresh.heap_note)
    binding.heap_note_shown = False
    async with pool._bindings_lock:
        pool._bindings[binding.file_path] = binding
    if old_digest:
        pool._stale_generations.add(old_digest)
        await pool.retire_idle_scratch(0.0, generation=old_digest)

"""Ending whole process trees (Linux ``/proc``).

An Isabelle prover runs as ``bash_process -> bash -> poly`` (plus children such as
ATPs). ``bash_process`` calls ``setsid``, so the prover lives in its own session and
process group, apart from the gateway JVM's group: a signal to the gateway's group
does not reach it, and a signal to one pid leaves the rest of the tree running.
``kill_trees`` signals every process in the given trees AND their process groups,
first SIGTERM, then SIGKILL for whatever is still running after the grace period.

The server's own process group is never signalled.
"""
from __future__ import annotations

import os
import signal
import time
from typing import Dict, Iterable, List, Optional, Set

PROC_ROOT = "/proc"


def stat_fields(pid: int, proc_root: Optional[str] = None) -> Optional[List[str]]:
    """Fields of /proc/<pid>/stat after the command name (state is [0], ppid [1],
    pgid [2], starttime [19]); None when the process is gone. The one parser of
    this format (memory_monitor uses it too)."""
    try:
        with open(f"{proc_root or PROC_ROOT}/{pid}/stat", "r", encoding="utf-8") as fh:
            _, _, rest = fh.read().rpartition(")")
        return rest.split()
    except (OSError, ValueError):
        return None


_stat_fields = stat_fields


#: Process groups that are never ended as a tree or signalled as a group: the
#: gateway's own group (registered by ReplBackendGatewayProcess). Its leader is an
#: ancestor of every prover, so without this a prover that shares the gateway's
#: group (no setsid in bash_process) would make ``tree_root`` pick the gateway.
_protected_groups: Set[int] = set()


def protect_group(pgid: int) -> None:
    _protected_groups.add(pgid)


def unprotect_group(pgid: int) -> None:
    _protected_groups.discard(pgid)


def running(pid: int) -> bool:
    """True while ``pid`` exists and is not a zombie (a zombie has ended; it only
    waits for its parent to collect it)."""
    fields = _stat_fields(pid)
    return bool(fields) and fields[0] not in ("Z", "X")


def children_map() -> Dict[int, List[int]]:
    """ppid -> child pids, from one scan of /proc."""
    result: Dict[int, List[int]] = {}
    try:
        entries = os.listdir(PROC_ROOT)
    except OSError:
        return result
    for name in entries:
        if not name.isdigit():
            continue
        fields = _stat_fields(int(name))
        if fields and len(fields) > 1 and fields[1].isdigit():
            result.setdefault(int(fields[1]), []).append(int(name))
    return result


def descendants(pid: int, children: Optional[Dict[int, List[int]]] = None) -> List[int]:
    """All descendants of ``pid`` (not ``pid`` itself), parents before children."""
    children = children_map() if children is None else children
    out: List[int] = []
    todo = list(children.get(pid, []))
    seen: Set[int] = set()
    while todo:
        p = todo.pop(0)
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        todo.extend(children.get(p, []))
    return out


def marked(key: str, value: str) -> List[int]:
    """Pids of the running processes whose environment has ``key=value`` (from
    /proc/<pid>/environ, the environment at exec). A marker set on one process
    is inherited by every descendant, whoever adopts it later, and no unrelated
    process carries it, so a reused pid is never mistaken for one of them."""
    needle = f"{key}={value}".encode()
    me = os.getpid()
    out: List[int] = []
    try:
        entries = os.listdir(PROC_ROOT)
    except OSError:
        return out
    for name in entries:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            with open(f"{PROC_ROOT}/{name}/environ", "rb") as fh:
                env = fh.read()
        except OSError:
            continue
        if needle in env.split(b"\0") and running(int(name)):
            out.append(int(name))
    return out


def pgid_of(pid: int) -> Optional[int]:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return None


def ancestors(pid: int) -> List[int]:
    """Parent, grandparent, ... of ``pid`` (up to, not including, pid 1)."""
    out: List[int] = []
    seen = {pid}
    fields = _stat_fields(pid)
    while fields and len(fields) > 1 and fields[1].isdigit():
        ppid = int(fields[1])
        if ppid <= 1 or ppid in seen:
            break
        out.append(ppid)
        seen.add(ppid)
        fields = _stat_fields(ppid)
    return out


def tree_root(pid: int) -> int:
    """The leader of ``pid``'s process group when that leader is an ancestor of
    ``pid``, else ``pid``. For a prover's ``poly`` this is the ``bash`` that
    ``bash_process`` started with ``setsid``: ending the tree from there ends
    the whole prover group."""
    pgid = pgid_of(pid)
    if (pgid is not None and pgid != pid and pgid not in _protected_groups
            and pgid in ancestors(pid)):
        return pgid
    return pid


def _signal_all(pids: Iterable[int], groups: Iterable[int], sig: int) -> None:
    for g in groups:
        try:
            os.killpg(g, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def kill_trees(
    roots: Iterable[int],
    *,
    grace: float = 3.0,
    protect_pgids: Iterable[int] = (),
    poll: float = 0.05,
) -> List[int]:
    """End every process in the trees rooted at ``roots`` (roots included).

    SIGTERM to each process and to each of their process groups, wait up to
    ``grace`` seconds, then SIGKILL to whatever still runs (with the
    descendants found at that time). The server's own process group and
    ``protect_pgids`` are never signalled as groups. Returns the pids that were
    signalled. Pids that are already gone are ignored.

    The process group of an ancestor of a root is not signalled either (that
    group holds processes outside the trees, e.g. the gateway JVM), unless the
    root leads that group itself.
    """
    protected = {os.getpgrp(), 0, 1, *protect_pgids, *_protected_groups}
    me = os.getpid()
    children = children_map()
    targets: List[int] = []
    for root in roots:
        if root in (None, 0, 1, me) or not running(root) or root in _protected_groups:
            continue
        for a in ancestors(root):
            g = pgid_of(a)
            if g is not None and g != root:
                protected.add(g)
        for p in [root, *descendants(root, children)]:
            if p != me and p not in targets:
                targets.append(p)
    if not targets:
        return []

    def groups_of(pids: Iterable[int]) -> Set[int]:
        found = {pgid_of(p) for p in pids}
        return {g for g in found if g is not None and g not in protected}

    groups = groups_of(targets)
    _signal_all(targets, groups, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline and any(running(p) for p in targets):
        time.sleep(poll)

    survivors = [p for p in targets if running(p)]
    if survivors:
        children = children_map()
        late = [d for p in survivors for d in descendants(p, children)]
        everyone = survivors + [d for d in late if d != me and d not in survivors]
        _signal_all(everyone, groups | groups_of(everyone), signal.SIGKILL)
        targets.extend(d for d in everyone if d not in targets)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and any(running(p) for p in everyone):
            time.sleep(poll)
    return targets

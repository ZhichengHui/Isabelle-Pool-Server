"""Container-aware memory monitor for the session pool.

Memory management used to live in the Scala backend, where it measured the
gateway JVM heap (`Runtime.getRuntime`). That number is unrelated to where
Isabelle's memory actually goes: each session is a separate `poly` (ML) OS
process the JVM cannot see. This monitor instead reads the **container cgroup**
(`/sys/fs/cgroup/memory.*`), which accounts for every process in the container
— gateway JVM, Python server, and all `poly` sessions — i.e. the exact figure
the OOM killer watches.

Pure stdlib, no psutil. Every file read is defensive: on any failure the
monitor degrades to a safe fallback rather than raising into the request path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from server.app.core.config import Memory
from server.app.core.logging import get_logger
from server.repl.src.python import process_tree

logger = get_logger(__name__)

# cgroup v2 (preferred) and v1 file locations.
_CG_V2_CURRENT = "/sys/fs/cgroup/memory.current"
_CG_V2_MAX = "/sys/fs/cgroup/memory.max"
_CG_V2_STAT = "/sys/fs/cgroup/memory.stat"
_CG_V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
_CG_V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CG_V1_STAT = "/sys/fs/cgroup/memory/memory.stat"
_PROC_MEMINFO = "/proc/meminfo"
# The server's OWN cgroup (cgroup v2: `0::/path` in /proc/self/cgroup). Inside a
# container that is the root (`/`), where the fixed paths above are the
# container's files. On a native host the root cgroup has no memory.current /
# memory.max, so a limit set with `systemd-run --scope -p MemoryMax=…` (or a
# systemd unit's MemoryMax) lives in the server's own cgroup directory — the
# gate must read that one or it is blind to the limit (memory-scaling plan
# review B1, 2026-10-20). Resolved once at monitor start.
_CG_ROOT = "/sys/fs/cgroup"
_PROC_SELF_CGROUP = "/proc/self/cgroup"

# cgroup v1 reports "unlimited" as a near-int64 sentinel; anything this large
# means "no limit", so we fall back to host memory.
_V1_UNLIMITED_FLOOR = 1 << 62


# Where the `used` figure came from: "cgroup_v2" / "cgroup_v1" (container),
# "host" (/proc/meminfo MemTotal - MemAvailable: native Linux, or a container
# without cgroup accounting), "fallback" (nothing readable, e.g. native macOS:
# used reads as 0 and the admission gate is inert — startup logs a warning).
SOURCE_CGROUP_V2 = "cgroup_v2"
SOURCE_CGROUP_V1 = "cgroup_v1"
SOURCE_HOST = "host"
SOURCE_FALLBACK = "fallback"

# Run-time memory tiers. "critical" = the admission rule refuses; "warning" =
# above Memory.WARNING_THRESHOLD; "unknown" = no reading taken yet.
STATE_OK = "ok"
STATE_WARNING = "warning"
STATE_CRITICAL = "critical"
STATE_UNKNOWN = "unknown"

# Process table root (overridden by tests with a fake tree).
_PROC_ROOT = "/proc"


@dataclass(frozen=True)
class MemorySnapshot:
    used_bytes: int
    limit_bytes: int
    source: str = SOURCE_FALLBACK
    # Host/VM MemAvailable when the figures above come from a cgroup (the VM
    # that hosts the container); None when unreadable or already host-based.
    host_available_bytes: Optional[int] = None
    # What everything OUTSIDE this container uses on the host/VM:
    # MemTotal - MemAvailable - used (None when unreadable).
    host_others_used_bytes: Optional[int] = None
    # True when this container is below its own threshold, the host/VM is
    # below the available floor, AND others use more than the headroom this
    # container was promised (limit x (1 - threshold)): something outside this
    # server holds the memory (gap G8; review aa860ea S1 — a container that is
    # itself the pressure is not "external"). None when undecidable.
    external_pressure: Optional[bool] = None

    @property
    def available_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)

    @property
    def pressure_pct(self) -> float:
        if self.limit_bytes <= 0:
            return 0.0
        return (self.used_bytes / self.limit_bytes) * 100.0

    @property
    def used_mb(self) -> float:
        return self.used_bytes / (1024.0 * 1024.0)

    @property
    def limit_mb(self) -> float:
        return self.limit_bytes / (1024.0 * 1024.0)

    @property
    def available_mb(self) -> float:
        return self.available_bytes / (1024.0 * 1024.0)


def _own_cgroup_dir() -> Optional[str]:
    """``/sys/fs/cgroup<path>`` of this process's cgroup v2 membership, or None
    for the root cgroup (the container case) and when unreadable."""
    try:
        with open(_PROC_SELF_CGROUP, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            rel = line[3:].strip()
            if rel and rel != "/":
                return _CG_ROOT.rstrip("/") + rel
            return None
    return None


def _in_own_cgroup(path: str, cg_dir: Optional[str]) -> str:
    """Map a fixed cgroup-v2 root path onto the server's own cgroup directory
    when that directory has the file; otherwise the path unchanged (tests
    point the constants at temp files, which never start with the root)."""
    if cg_dir and path.startswith(_CG_ROOT + "/"):
        candidate = os.path.join(cg_dir, os.path.basename(path))
        if os.path.exists(candidate):
            return candidate
    return path


def _read_int_file(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _read_cgroup_max(path: str) -> Optional[int]:
    """cgroup v2 memory.max is either an integer or the literal "max"."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if raw == "max":
        return None  # no limit -> caller falls back to host memory
    try:
        return int(raw)
    except ValueError:
        return None


def _meminfo_fields(*names: str) -> Dict[str, int]:
    """Named fields of /proc/meminfo in BYTES (the file reports kB); missing or
    unreadable fields are simply absent from the result."""
    found: Dict[str, int] = {}
    try:
        with open(_PROC_MEMINFO, "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in names:
                    parts = rest.split()
                    if parts:
                        try:
                            found[key] = int(parts[0]) * 1024
                        except ValueError:
                            continue
    except OSError:
        return found
    return found


def _host_mem_total_bytes() -> Optional[int]:
    """MemTotal from /proc/meminfo (kB), as host/VM total memory."""
    return _meminfo_fields("MemTotal").get("MemTotal")


def _host_mem_used_bytes() -> Optional[int]:
    """MemTotal - MemAvailable from /proc/meminfo: what the kernel considers
    in use once reclaimable cache is discounted. The `used` figure when no
    cgroup accounting is readable (native Linux installs — before this
    fallback the gate silently read 0 there and never fired)."""
    fields = _meminfo_fields("MemTotal", "MemAvailable")
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        return None
    return max(0, fields["MemTotal"] - fields["MemAvailable"])


def proc_alive(pid: int) -> bool:
    """True while the process table lists ``pid`` (the ML process of a session)."""
    return os.path.isdir(f"{_PROC_ROOT}/{pid}")


def read_starttime(pid: int) -> Optional[int]:
    """Process start time (clock ticks since boot, field 22 of /proc/<pid>/stat).
    Together with the pid it identifies ONE process: a pid reused after a
    prover death carries a different start time (review a96f76d S1)."""
    fields = process_tree.stat_fields(pid, _PROC_ROOT)
    try:
        return int(fields[19]) if fields and len(fields) > 19 else None  # state is fields[0]
    except ValueError:
        return None


def proc_matches(pid: int, starttime: Optional[int]) -> bool:
    """True while ``pid`` exists AND (when known) still has ``starttime``."""
    if not proc_alive(pid):
        return False
    if starttime is None:
        return True
    return read_starttime(pid) == starttime


def read_rss(pid: int) -> Optional[int]:
    """Resident set size of one process in bytes (``VmRSS`` from
    /proc/<pid>/status); None when the process is gone or unreadable."""
    try:
        with open(f"{_PROC_ROOT}/{pid}/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _ppid_map() -> Dict[int, int]:
    """pid -> parent pid for every process in the table (one scan)."""
    result: Dict[int, int] = {}
    try:
        names = os.listdir(_PROC_ROOT)
    except OSError:
        return result
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{_PROC_ROOT}/{name}/stat", "r", encoding="utf-8") as fh:
                stat = fh.read()
        except OSError:
            continue
        # "pid (comm) state ppid ...": comm may hold spaces or parentheses, so
        # split after the LAST ')' — then fields are [state, ppid, ...].
        _, _, rest = stat.rpartition(")")
        fields = rest.split()
        if len(fields) >= 2:
            try:
                result[int(name)] = int(fields[1])
            except ValueError:
                continue
    return result


def pgrp_map() -> Dict[int, Tuple[int, int, str]]:
    """pid -> (ppid, pgrp, comm) for every process (one /proc scan; comm may
    contain spaces or parentheses, so the stat line is split after its last ')')."""
    result: Dict[int, Tuple[int, int, str]] = {}
    try:
        names = os.listdir(_PROC_ROOT)
    except OSError:
        return result
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{_PROC_ROOT}/{name}/stat", "r", encoding="utf-8") as fh:
                stat = fh.read()
        except OSError:
            continue
        head, _, rest = stat.rpartition(")")
        comm = head.partition("(")[2]
        fields = rest.split()
        if len(fields) >= 3:
            try:
                result[int(name)] = (int(fields[1]), int(fields[2]), comm)
            except ValueError:
                continue
    return result


ATP_EXCLUDED_COMMS = frozenset(
    {
        "bash",
        "sh",
        "bash_process",
        "timeout",
        "java",
        "poly",
        "Naproche",
        "python",
        "python3",
        "isabelle",
    }
)


def gateway_children_rss(
    root_pid: int, exclude_pids: Iterable[int] = ()
) -> Tuple[int, int]:
    """``(bytes, count)`` of the gateway's descendants that are not plumbing
    (launcher/bash/JVM/bash_process) and not under a prover: the ATP and SMT
    solvers sledgehammer runs (fill_sorries §1.6). ML's bash goes through
    Scala, so they hang off the JVM — `bash_process` runs each script in a
    NEW session (own pgrp/sid), so the walk follows parent links from
    ``root_pid``, not the process group. Subtrees rooted at ``exclude_pids``
    (the sessions' polys) are skipped whole."""
    excluded = set(int(p) for p in exclude_pids)
    procs = pgrp_map()
    children: Dict[int, List[int]] = {}
    for pid, (ppid, _pgrp, _comm) in procs.items():
        children.setdefault(ppid, []).append(pid)
    total, count = 0, 0
    root_pid = int(root_pid)
    if root_pid not in procs:
        return 0, 0
    stack, seen = [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid in excluded:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
        if pid == root_pid or procs[pid][2] in ATP_EXCLUDED_COMMS:
            continue
        rss = read_rss(pid)
        if rss:
            total += rss
            count += 1
    return total, count


def rss_tree(root_pids: Iterable[int]) -> Dict[int, Optional[int]]:
    """RSS in bytes of each root process PLUS its descendants (a session's poly
    and, during sledgehammer, the ATP children it forks). None for a root that
    is no longer in the process table — the "prover died" signal."""
    roots = list(root_pids)
    if not roots:
        return {}
    parents = _ppid_map()
    children: Dict[int, List[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    out: Dict[int, Optional[int]] = {}
    for root in roots:
        if not proc_alive(root):
            out[root] = None
            continue
        total, stack, seen = 0, [root], set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            total += read_rss(pid) or 0
            stack.extend(children.get(pid, []))
        out[root] = total
    return out


class MemoryMonitor:  # pylint: disable=too-many-instance-attributes
    """Reads container memory pressure and decides whether new sessions fit."""

    def __init__(self) -> None:
        self.pressure_threshold = Memory.PRESSURE_THRESHOLD
        self.warning_threshold = Memory.WARNING_THRESHOLD
        self.min_available_bytes = Memory.MIN_AVAILABLE_MB * 1024 * 1024
        self.fallback_limit_bytes = Memory.FALLBACK_SYSTEM_MB * 1024 * 1024
        # Source of the most recent `used` reading (see SOURCE_* above).
        self.last_source: str = SOURCE_FALLBACK
        # Most recent reading and its tier: what the X-Memory-State header and
        # the cleanup sweep report without touching the files again.
        self.last_snapshot: Optional[MemorySnapshot] = None
        self.last_state: str = STATE_UNKNOWN
        # Own cgroup v2 directory (None = root / container); see _CG_ROOT.
        self.cgroup_dir: Optional[str] = _own_cgroup_dir()

    def _read_inactive_file(self) -> int:
        """Reclaimable page cache (inactive_file) from memory.stat, 0 on failure.

        memory.current/usage_in_bytes INCLUDE page cache — Isabelle heap images
        read at session start stay cached and would otherwise count as 'used'
        forever, tripping the admission gate even when the kernel could reclaim
        them. Subtracting inactive_file is the same correction `docker stats`
        applies (v2 key: inactive_file; v1 hierarchical key: total_inactive_file).
        """
        for path, key in (
            (_CG_V2_STAT, "inactive_file"),
            (_CG_V1_STAT, "total_inactive_file"),
        ):
            try:
                with open(
                    _in_own_cgroup(path, self.cgroup_dir), "r", encoding="utf-8"
                ) as fh:
                    for line in fh:
                        if line.startswith(key + " "):
                            return int(line.split()[1])
            except (OSError, ValueError, IndexError):
                continue
        return 0

    def _read_used(self) -> int:
        """Container cgroup usage (minus reclaimable cache), else host usage
        from /proc/meminfo, else 0. Records where the figure came from in
        ``last_source``."""
        used = _read_int_file(_in_own_cgroup(_CG_V2_CURRENT, self.cgroup_dir))
        if used is not None:
            self.last_source = SOURCE_CGROUP_V2
        else:
            used = _read_int_file(_CG_V1_USAGE)
            if used is not None:
                self.last_source = SOURCE_CGROUP_V1
        if used is not None:
            return max(0, used - self._read_inactive_file())
        host_used = _host_mem_used_bytes()
        if host_used is not None:
            self.last_source = SOURCE_HOST
            return host_used
        self.last_source = SOURCE_FALLBACK
        return 0

    def _read_limit(self) -> int:
        # cgroup v2 numeric limit, else v1 limit (unless sentinel "unlimited"),
        # else host MemTotal, else the configured fallback.
        limit = _read_cgroup_max(_in_own_cgroup(_CG_V2_MAX, self.cgroup_dir))
        if limit is None:
            v1 = _read_int_file(_CG_V1_LIMIT)
            if v1 is not None and v1 < _V1_UNLIMITED_FLOOR:
                limit = v1
        if limit is None:
            limit = _host_mem_total_bytes()
        if limit is None or limit <= 0:
            limit = self.fallback_limit_bytes
        return limit

    def read(self) -> MemorySnapshot:
        used = self._read_used()  # sets last_source
        limit = self._read_limit()
        host_available: Optional[int] = None
        others_used: Optional[int] = None
        external: Optional[bool] = None
        if self.last_source in (SOURCE_CGROUP_V2, SOURCE_CGROUP_V1):
            fields = _meminfo_fields("MemTotal", "MemAvailable")
            host_available = fields.get("MemAvailable")
            host_total = fields.get("MemTotal")
            if host_available is not None and host_total is not None and limit > 0:
                others_used = max(0, host_total - host_available - used)
                headroom = limit * (1.0 - self.pressure_threshold / 100.0)
                own_pressure = used / limit * 100.0
                external = (
                    own_pressure < self.pressure_threshold  # we are not the pressure
                    and host_available < self.min_available_bytes  # the host is short
                    and others_used > headroom  # others exceed our promised headroom
                )
        snap = MemorySnapshot(
            used_bytes=used,
            limit_bytes=limit,
            source=self.last_source,
            host_available_bytes=host_available,
            host_others_used_bytes=others_used,
            external_pressure=external,
        )
        self.last_snapshot = snap
        self.last_state = self.state_for(snap)
        return snap

    def state_for(self, snapshot: MemorySnapshot) -> str:
        """Tier of a snapshot: critical (admission refuses), warning (above
        the warning threshold), else ok."""
        if not self.can_admit(snapshot):
            return STATE_CRITICAL
        if snapshot.pressure_pct >= self.warning_threshold:
            return STATE_WARNING
        return STATE_OK

    def can_admit(
        self, snapshot: Optional[MemorySnapshot] = None, extra_bytes: int = 0
    ) -> bool:
        """True if there is room for another session under current pressure.

        ``extra_bytes`` (batch 3 budget admission) is what the candidate and
        the work already in flight are expected to add: admission holds only
        if usage INCLUDING it stays under the threshold and the floor. A
        host that is itself under the available floor never admits, whatever
        the cause (external or not).
        """
        snap = snapshot if snapshot is not None else self.read()
        if (
            snap.host_available_bytes is not None
            and snap.host_available_bytes < self.min_available_bytes
        ):
            return False  # the host itself is short, whatever the cause
        if snap.limit_bytes <= 0:
            return True
        projected = snap.used_bytes + max(0, extra_bytes)
        return (
            projected / snap.limit_bytes * 100.0 < self.pressure_threshold
            and snap.limit_bytes - projected > self.min_available_bytes
        )

    def status_dict(self, snapshot: Optional[MemorySnapshot] = None) -> Dict[str, Any]:
        snap = snapshot if snapshot is not None else self.read()
        return {
            "memory_used_mb": round(snap.used_mb, 1),
            "memory_limit_mb": round(snap.limit_mb, 1),
            "memory_available_mb": round(snap.available_mb, 1),
            "memory_pressure_pct": round(snap.pressure_pct, 1),
            "memory_source": snap.source,
            "memory_state": self.state_for(snap),
            "external_pressure": snap.external_pressure,
            "host_available_mb": (
                None
                if snap.host_available_bytes is None
                else round(snap.host_available_bytes / (1024.0 * 1024.0), 1)
            ),
            "host_others_used_mb": (
                None
                if snap.host_others_used_bytes is None
                else round(snap.host_others_used_bytes / (1024.0 * 1024.0), 1)
            ),
        }

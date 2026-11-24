"""cgroup limits for session provers and `isabelle build` (efficiency plan D3, D4).

At start-up (``init``, before the gateway JVM starts) the server checks once whether
``systemd-run --user --scope`` works in its environment (a user manager reachable over
D-Bus: a logind session, a user service). When it does:

- every session's ML process runs in its own scope ``ips-sess-<uuid>.scope`` with
  ``MemoryMax`` = ISABELLE_SESSION_MEMORY_MAX (24G): the Isabelle system option
  ``process_policy`` (the prefix of the ML process's command line, ml_process.scala) is
  passed to the gateway as ISABELLE_SESSION_PROCESS_POLICY (session_manager.scala).
  ``systemd-run --scope`` execs the command, so the prover keeps the pid Isabelle and the
  server know; a prover the kernel kills for the limit is found by the existing death
  check and reported ``prover_died``;
- every ``isabelle build`` (heap pool, import heaps, big-step) runs in a scope with
  ``MemoryMax`` = ISABELLE_BUILD_MEMORY_MAX (32G) (``build_prefix``).

All these scopes are put into one slice of this server (``ips_pool_<port>.slice``), whose
usage the admission gate adds to its own cgroup's (memory_monitor): the scopes are no
longer inside the server's cgroup.

When it does not work (no user manager, e.g. a container or an ssh session without
D-Bus; or ISABELLE_CGROUP_SCOPES=off) nothing is prefixed, a warning is logged, and the
server runs as before (the per-process ``--maxheap`` stays the bound).

External provers of sledgehammer are NOT in the session scope: the gateway JVM starts
them (bash_process), not the prover; their total is bounded by the sledgehammer slots.
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional

from server.app.core.config import Memory, Server
from server.app.core.logging import get_logger

logger = get_logger(__name__)

SYSTEMD_RUN = os.getenv("ISABELLE_SYSTEMD_RUN", "systemd-run")
POLICY_ENV = "ISABELLE_SESSION_PROCESS_POLICY"
CHECK_TIMEOUT_S = 15.0

_state: dict = {"checked": False, "available": False, "reason": "not checked"}


def slice_name() -> str:
    """The systemd slice of this server's scopes (``_``: no slice hierarchy)."""
    return f"ips_pool_{Server.PORT}.slice"


def _scope_args(memory_max: str) -> List[str]:
    # MemorySwapMax=0: with swap, a scope at MemoryMax is swapped out and crawls instead of
    # being killed (seen on PGR, 7 GB swap: a prover sat at its limit for minutes).
    return [SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect",
            f"--slice={slice_name()}", "-p", f"MemoryMax={memory_max}", "-p", "MemorySwapMax=0"]


def check_systemd_run() -> tuple[bool, str]:
    """(usable, why not): run ``true`` in a throw-away scope."""
    try:
        out = subprocess.run(_scope_args("256M") + ["true"], capture_output=True,
                             text=True, timeout=CHECK_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if out.returncode != 0:
        return False, (out.stderr or out.stdout or f"exit {out.returncode}").strip()[:300]
    return True, ""


def init() -> bool:
    """Decide once (call before the gateway starts). Sets the session policy for the
    gateway when scopes are usable and none was set by hand. Returns availability."""
    if Memory.CGROUP_SCOPES == "off":
        available, reason = False, "ISABELLE_CGROUP_SCOPES=off"
    else:
        available, reason = check_systemd_run()
    _state.update(checked=True, available=available, reason=reason)
    if available:
        if not os.environ.get(POLICY_ENV):
            os.environ[POLICY_ENV] = " ".join(
                _scope_args(Memory.SESSION_MEMORY_MAX) + ["--unit={unit}"])
        logger.info("cgroup scopes on: sessions %s, builds MemoryMax=%s, slice %s",
                    os.environ.get(POLICY_ENV), Memory.BUILD_MEMORY_MAX, slice_name())
    else:
        if Memory.CGROUP_SCOPES != "off":
            logger.warning(
                "systemd-run --user is not usable (%s): session provers and builds run "
                "without a cgroup memory limit (only Poly/ML --maxheap bounds them)", reason)
        os.environ.pop(POLICY_ENV, None)
    return available


def available() -> bool:
    return bool(_state["available"])


def status() -> dict:
    return dict(_state, slice=slice_name() if _state["available"] else None)


def build_prefix() -> List[str]:
    """Command prefix that puts an ``isabelle build`` into its own limited scope, or []."""
    return _scope_args(Memory.BUILD_MEMORY_MAX) if available() else []


def slice_dir() -> Optional[str]:
    """cgroup v2 directory of this server's slice under the user manager (None when
    scopes are off). The slice exists only while one of its scopes does."""
    if not available():
        return None
    uid = os.getuid()
    return (f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/"
            f"{slice_name()}")


def slice_used_bytes() -> int:
    """memory.current minus inactive_file of the slice; 0 when absent/unreadable."""
    d = slice_dir()
    if d is None:
        return 0
    try:
        with open(os.path.join(d, "memory.current"), encoding="utf-8") as f:
            used = int(f.read().strip())
    except (OSError, ValueError):
        return 0
    inactive = 0
    try:
        with open(os.path.join(d, "memory.stat"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("inactive_file "):
                    inactive = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    return max(0, used - inactive)

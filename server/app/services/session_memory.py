"""Per-session memory attribution (plan §2.B, owner decision D2).

The numbers come from the session's own ML process: ``Backend_Probes.mem_stats``
(an overlay query registered in REPL.ML) returns the process id and the
Poly/ML runtime statistics; the server then reads the resident size of that
pid (plus its sledgehammer children) from ``/proc``. The cgroup stays the truth
for the container total — this is attribution, not admission.

``MemoryAwareSession`` is the session class the manager instantiates; it is a
plain ``_Isabelle_Session`` plus this mixin, kept in its own module because
``session.py`` sits at the source-size gate. Helpers at module level are no-ops
for sessions without the mixin (test stubs).

Field semantics (plan-review 501a1ba S1/S2):
- a probe answer is a *fresh heap sample* only when it carries ``size_heap``;
  ``{}`` (no host command yet) and ``{pid}``-only answers (the cached pid) are
  "no new sample", never failures;
- ``heap_mb`` is ``size_heap``, the dynamic heap the runtime has reserved; the
  mmapped heap image lives outside it, so RSS stays the attribution figure and
  no "live data" is derived from the heap keys.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Mapping, Optional

from server.app.core.config import Memory, Repl, Timeouts
from server.app.core.logging import get_logger
from server.app.services.internal_models import SessionStatus
from server.app.services.memory_monitor import proc_matches, read_starttime
from server.app.services.session import _Isabelle_Session

logger = get_logger(__name__)

HEAP_KEY = "size_heap"
_MB = 1024.0 * 1024.0


def _mb(value: Optional[int]) -> Optional[float]:
    return None if value is None else round(value / _MB, 1)


class SessionMemoryMixin:  # pylint: disable=too-many-instance-attributes
    """Memory facts of one session; see the module docstring."""

    # provided by _Isabelle_Session
    session_id: Any
    status: Any
    backend: Any
    _call_backend: Callable[..., Any]

    ml_pid: Optional[int]
    ml_starttime: Optional[int]  # /proc start time of ml_pid: guards against pid reuse
    ml_heap: Dict[str, int]
    ml_phase: Optional[str]
    rss_bytes: Optional[int]
    rss_sampled_at: float
    mem_sampled_at: float
    mem_probe_error: Optional[str]
    sledgehammer_rss_bytes: int
    sledgehammer_peak_bytes: int
    sledgehammer_shared_with: int
    sledgehammer_sampled_at: float

    def _init_memory(self) -> None:
        self.ml_pid = None
        self.ml_starttime = None
        self.ml_heap = {}
        self.ml_phase = None
        self.rss_bytes = None
        self.rss_sampled_at = 0.0
        self.mem_sampled_at = 0.0
        self.mem_probe_error = None
        self.sledgehammer_rss_bytes = 0
        self.sledgehammer_peak_bytes = 0
        self.sledgehammer_shared_with = 0
        self.sledgehammer_sampled_at = 0.0

    @property
    def heap_bytes(self) -> Optional[int]:
        """Poly/ML ``size_heap`` of the last fresh sample (reserved dynamic heap)."""
        return self.ml_heap.get(HEAP_KEY)

    @property
    def rss_mb(self) -> Optional[float]:
        """Resident size of the ML process (+ children) at the last sweep."""
        return _mb(self.rss_bytes)

    @property
    def heap_mb(self) -> Optional[float]:
        """``size_heap`` of the last fresh sample, in MB."""
        return _mb(self.heap_bytes)

    def record_mem_stats(
        self, facts: Mapping[str, str], now: Optional[float] = None
    ) -> bool:
        """Store a probe answer. Returns True for a fresh heap sample."""
        now = time.time() if now is None else now
        pid = str(facts.get("pid", "") or "")
        if pid.isdigit() and int(pid) != self.ml_pid:
            self.ml_pid = int(pid)
            self.ml_starttime = read_starttime(self.ml_pid)
        self.mem_sampled_at = now
        if "error" in facts:
            self.mem_probe_error = str(facts["error"])[:200]
            return False
        if HEAP_KEY not in facts:
            return False
        heap: Dict[str, int] = {}
        for key, value in facts.items():
            if key == "pid":
                continue
            try:
                heap[str(key)] = int(float(value))  # "now" is a float string
            except (TypeError, ValueError):
                continue
        self.ml_heap = heap
        self.mem_probe_error = None
        return True

    def maybe_probe_mem(
        self, now: Optional[float] = None, min_interval: Optional[float] = None
    ) -> bool:
        """Run the ML probe unless one ran within ``min_interval`` seconds.

        Request-path only (after a call that already paid an overlay); a probe
        submitted from the sweep would queue behind in-flight work on the
        session's single worker thread. A failed probe counts as a sample
        (``mem_sampled_at`` set) and is logged at WARNING without a traceback,
        so a distressed prover costs at most one short probe per interval.
        """
        now = time.time() if now is None else now
        interval = Memory.PROBE_MIN_INTERVAL_S if min_interval is None else min_interval
        if self.status == SessionStatus.CLOSED:
            return False
        if now - self.mem_sampled_at < interval:
            return False
        try:
            raw = self._call_backend(
                lambda: dict(self.backend.raw.mem_stats()),
                timeout=Repl.MEM_TIMEOUT_S + Timeouts.BACKEND_GRACE_S,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.mem_sampled_at = now
            self.mem_probe_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning(
                "memory probe failed session_id=%s: %s",
                self.session_id,
                self.mem_probe_error,
            )
            return False
        return self.record_mem_stats({str(k): str(v) for k, v in raw.items()}, now)

    def set_rss(self, rss_bytes: Optional[int], now: Optional[float] = None) -> None:
        """Record the sweep's RSS reading (bytes) for this session."""
        self.rss_bytes = rss_bytes
        self.rss_sampled_at = time.time() if now is None else now

    def forget_prover(self) -> None:
        """After a backend document reset the session has a NEW ML process:
        drop the pid so the next death check asks the session phase and the
        next probe records the new pid — a stale pid read as "prover died"
        and the reset session was discarded (memory-scaling smoke 2026-10-20)."""
        self.ml_pid = None
        self.ml_starttime = None
        self.mem_sampled_at = 0.0  # probe at the next call

    def note_sledgehammer_rss(
        self, rss_bytes: int, shared_with: int = 0, now: Optional[float] = None
    ) -> None:
        """A sample of the ATP processes while this session's sledgehammer runs
        (fill_sorries §1.6). With other steps in flight the children cannot be
        told apart, so the whole sum is attributed to each and ``shared_with``
        says how many other steps share it (review B4)."""
        self.sledgehammer_rss_bytes = int(rss_bytes)
        self.sledgehammer_peak_bytes = max(self.sledgehammer_peak_bytes, int(rss_bytes))
        self.sledgehammer_shared_with = int(shared_with)
        self.sledgehammer_sampled_at = time.time() if now is None else now

    def clear_sledgehammer_rss(self) -> None:
        """The step ended: nothing of the ATPs is attributed any more (peak stays)."""
        self.sledgehammer_rss_bytes = 0
        self.sledgehammer_shared_with = 0

    def prover_alive(self) -> Optional[bool]:
        """None until the pid is known; else whether THAT process still exists
        (pid present and, when recorded, the same start time — a reused pid
        after a prover death must not read as alive)."""
        if self.ml_pid is None:
            return None
        return proc_matches(self.ml_pid, self.ml_starttime)

    def check_prover_dead(self, timeout: float = 5.0) -> bool:
        """Request-path death check (plan-review f1fb31a B1): ``/proc`` + start
        time when the pid is known; otherwise Isabelle's ``Session.phase`` via
        the backend (a Scala-only read, instant on a corpse — `Terminated(...)`
        after a kill). False when nothing can be told."""
        alive = self.prover_alive()
        if alive is not None:
            return not alive
        try:
            phase = str(
                self._call_backend(self.backend.raw.session_phase, timeout=timeout)
            )
        except Exception:  # pylint: disable=broad-exception-caught
            return False
        self.ml_phase = phase
        return phase.startswith("Terminated")

    def memory_fields(self) -> Dict[str, Any]:
        """Listing / facts fields (never the pid: it is an internal handle)."""
        return {
            "rss_mb": self.rss_mb,
            "heap_mb": self.heap_mb,
            "mem_sampled_at": self.mem_sampled_at or None,
            "mem_probe_error": self.mem_probe_error,
            "sledgehammer_mb": (
                _mb(self.sledgehammer_rss_bytes) if self.sledgehammer_rss_bytes else 0.0
            ),
            "sledgehammer_peak_mb": _mb(self.sledgehammer_peak_bytes) or 0.0,
            "sledgehammer_shared_with": self.sledgehammer_shared_with,
        }


class SessionFillMixin:  # pylint: disable=too-few-public-methods
    """The backend call of the server-side ``fill_sorries`` job (plan 2026-10-18
    §1.1/§1.2): sledgehammer hosted on the command with a chunk-report INDEX —
    the command before a ``sorry`` — with ``max_proofs`` and an optional prover
    list. Lives here, not in session.py (at the size gate; plan review B1); same
    busy/activity accounting and worker timeout as ``sledgehammer_at``."""

    backend: Any
    _call_backend: Callable[..., Any]
    _acquire_request: Callable[[], None]
    _release_request: Callable[[], None]
    update_activity: Callable[[], None]

    def sledgehammer_at_command(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        index: int,
        subgoal: int = 1,
        timeout_s: int = 30,
        max_proofs: int = 1,
        provers: str = "",
        http_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """``{found, subgoals, results}`` or ``{found: False, subgoals, error}``
        from the backend (JSON parsed); ``subgoals`` is the number of goals the
        ``sorry`` at that position would close (-1 when the op never ran)."""
        self.update_activity()
        self._acquire_request()
        try:
            logger.info(
                "running sledgehammer_at_command index=%s subgoal=%s timeout_s=%s max_proofs=%s",
                index,
                subgoal,
                timeout_s,
                max_proofs,
            )
            result = self._call_backend(
                lambda: self.backend.raw.sledgehammer_at_command(
                    int(index),
                    int(subgoal),
                    int(timeout_s),
                    int(max_proofs),
                    str(provers or ""),
                ),
                timeout=http_timeout or (timeout_s + 40.0),
            )
            try:
                parsed = json.loads(result) if result else None
            except (ValueError, TypeError):
                parsed = None
            if not isinstance(parsed, dict):
                return {
                    "found": False,
                    "subgoals": -1,
                    "error": "unparseable backend reply",
                }
            parsed.setdefault("subgoals", -1)
            return parsed
        finally:
            self._release_request()


class MemoryAwareSession(SessionMemoryMixin, SessionFillMixin, _Isabelle_Session):
    """The session class the manager creates: a plain session + memory facts."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._init_memory()


def probe_memory(session: Any, now: Optional[float] = None) -> bool:
    """Request-path hook: probe if due. No-op for sessions without the mixin."""
    if isinstance(session, SessionMemoryMixin):
        return session.maybe_probe_mem(now)
    return False


def prover_dead(session: Any) -> bool:
    """Sweep-side signal: True only when the pid is known AND its process is gone."""
    return isinstance(session, SessionMemoryMixin) and session.prover_alive() is False


def check_prover_dead(session: Any) -> bool:
    """Request-path signal (may ask the backend for the session phase)."""
    return isinstance(session, SessionMemoryMixin) and session.check_prover_dead()


def lease_alive_at(session: Any) -> float:
    """When a leased session last showed that its holder is alive: its last use
    or its last lease renewal (POST .../renew_lease), whichever is later. The
    abandoned-lease rule (``max_lease_age``) counts from here."""
    return max(float(session.last_activity), float(getattr(session, "lease_renewed_at", 0.0)))


def expiry_fields(
    session: Any, now: float, idle_timeout: float, max_lease_age: float
) -> Dict[str, Any]:
    """When the server will reap the session if nothing touches it: idle
    sessions after ``idle_timeout``, leased ones after ``max_lease_age``
    (the abandoned-lease rule; the idle timeout does not apply to a leased
    session, so ``idle_expires_at`` is None then). ``expires_in_s`` is the
    authoritative figure. Absolute epoch seconds + seconds remaining."""
    last = float(session.last_activity)
    leased = bool(session.leased)
    idle_expires_at: Optional[float] = None if leased else last + idle_timeout
    lease_expires_at: Optional[float] = (
        lease_alive_at(session) + max_lease_age if leased else None
    )
    applies: float = (
        lease_expires_at if lease_expires_at is not None else last + idle_timeout
    )
    return {
        "idle_s": round(max(0.0, now - last), 1),
        "idle_expires_at": idle_expires_at,
        "lease_expires_at": lease_expires_at,
        "expires_in_s": round(max(0.0, applies - now), 1),
    }


def memory_fields(session: Any) -> Dict[str, Any]:
    """Listing fields for any session object (None-filled without the mixin)."""
    if isinstance(session, SessionMemoryMixin):
        return session.memory_fields()
    return {
        "rss_mb": None,
        "heap_mb": None,
        "mem_sampled_at": None,
        "mem_probe_error": None,
        "sledgehammer_mb": None,
        "sledgehammer_peak_mb": None,
        "sledgehammer_shared_with": None,
    }

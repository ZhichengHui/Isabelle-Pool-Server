from __future__ import annotations

import asyncio
import threading
import uuid
from collections import OrderedDict
from typing import List, Optional, Set, Tuple, Union

from server.repl.src.python.repl_backend_gateway import ReplBackendGatewayProcess
from server.repl.src.python.thy_init import ThyInit
from server.app.core.config import Memory, Server, Timeouts
from server.app.core.logging import get_logger, logging_context
from server.app.core import metrics
from server.app.errors import GatewayUnavailable, PoolExhausted, SessionBusyError, SessionNotFound, SessionStartError
from server.app.services.backend_reaper import BackendReaperMixin
from server.app.services.memory_monitor import MemoryMonitor
from server.app.services.fill_sorries import FillJobMixin
from server.app.services.memory_policy import MemoryPolicyMixin
from server.app.services.session import SessionStatus, _Isabelle_Session
from server.app.services.session_memory import MemoryAwareSession
from server.app.services.sledgehammer_slots import SledgehammerSlots
from server.app.services.threaded_backend import ThreadedBackend

from pathlib import Path
from server.app.services.build_verify import BuildVerifier
from server.app.services.session_manager_helpers import (
    SessionManagerHelpersMixin,
    _gateway_lock,
)

logger = get_logger(__name__)


class SessionManager(SessionManagerHelpersMixin, MemoryPolicyMixin, FillJobMixin, BackendReaperMixin):

    def __init__(
        self,
        idle_timeout: float = Timeouts.SESSION_IDLE_TIMEOUT ,
        pool_size: int = Server.DEFAULT_POOL_SIZE,
        cleanup_interval: float = Timeouts.CLEANUP_INTERVAL,
        initial_sessions: int = Server.INITIAL_SESSIONS,
    ):
        self.idle_timeout = idle_timeout
        self.max_lease_age = Server.MAX_LEASE_AGE
        self.pool_size = pool_size
        self.cleanup_interval = cleanup_interval
        self.initial_sessions = initial_sessions

        self.gateway: Optional[ReplBackendGatewayProcess] = None
        self.thy_init: Optional[ThyInit] = None

        self._lru: "OrderedDict[uuid.UUID, _Isabelle_Session]" = OrderedDict()
        self._lock = threading.Lock()
        # tags of the backend creations in flight (backend_reaper.py), under _lock
        self._creating_tags: Set[str] = set()
        self._cleanup_task: Optional[asyncio.Task] = None

        # Container-aware memory management (replaces the old Scala JVM-heap
        # logic). Gated by ENABLE_MEMORY_MANAGEMENT; the monitor itself is cheap
        # and always constructed so reporting works even when gating is off.
        self.memory_management_enabled = Server.ENABLE_MEMORY_MANAGEMENT
        self.memory = MemoryMonitor()

        # Bound concurrent sledgehammer (heavy ML) calls server-wide. Without
        # this, a burst of simultaneous sledgehammers spikes memory and can
        # OOM-kill the shared gateway JVM (see docs/ISSUES.md). Lazily bound to the
        # running loop on first use.
        self.sledgehammer_sem = asyncio.Semaphore(Server.MAX_CONCURRENT_SLEDGEHAMMER)
        self.max_concurrent_sledgehammer = Server.MAX_CONCURRENT_SLEDGEHAMMER
        self.slots = SledgehammerSlots(self, Server.MAX_CONCURRENT_SLEDGEHAMMER)

        # Big-step builds: bounded by their own knob (default 1) and gated by
        # memory in verify_big_step_build — not derived from the session pool.
        self.build_verifier = BuildVerifier(
            isabelle_bin="isabelle",
            max_concurrent_builds=Server.MAX_CONCURRENT_BIGSTEP_BUILDS,
            build_jobs_per_request=1,
            temp_parent=Path("/tmp"),
        )

    async def startup(self) -> None:
        where = self._where("startup")
        initial_sessions = self.initial_sessions
        if initial_sessions < 0:
            logger.warning("invalid initial_sessions=%s; defaulting to 2", initial_sessions)
            initial_sessions = Server.INITIAL_SESSIONS

        if initial_sessions == 0:
            logger.info("skipping pool pre-warming because initial_sessions=0")

        self.log_capacity_warnings()

        try:
            self._ensure_gateway()
        except Exception as e:
            if isinstance(e, GatewayUnavailable):
                raise
            raise GatewayUnavailable(f"{where}: gateway startup failed: {e}") from e

        try:
            if self.thy_init is None:
                logger.info("initializing ThyInit")
                self.thy_init = ThyInit()
                logger.info("ThyInit initialized")
        except Exception as e:
            logger.exception("failed to initialize ThyInit")
            raise SessionStartError(f"{where}: failed to initialize ThyInit: {e}") from e

        for i in range(initial_sessions):
            try:
                logger.info("pre-warming session %s/%s", i + 1, initial_sessions)
                await self._create_session()
            except Exception as e:
                logger.exception("failed to pre-start session %s", i)
                raise SessionStartError(f"{where}: failed to pre-start session {i}: {e}") from e

    async def shutdown(self) -> None:
        logger.info("session manager shutdown started")
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("error awaiting cancelled cleanup task")
            self._cleanup_task = None

        with self._lock:
            sessions = list(self._lru.values())
            self._lru.clear()

        for session in sessions:
            try:
                with logging_context(session_id=session.session_id, field=session.field):
                    logger.info("closing session during shutdown")
                    session.close()
            except Exception:
                logger.exception("failed to close session during shutdown")

        if self.gateway is not None:
            try:
                logger.info("terminating REPL gateway")
                self.gateway.terminate()
            except Exception:
                logger.exception("failed to terminate REPL gateway")
            self.gateway = None

        if self.thy_init is not None:
            try:
                logger.info("cleaning up ThyInit generated files")
                for filename in list(self.thy_init.created_files):
                    try:
                        self.thy_init.cleanup(filename)
                    except Exception:
                        logger.exception("failed to cleanup ThyInit file=%s", filename)
            except Exception:
                logger.exception("failed to cleanup ThyInit")
            self.thy_init = None
        logger.info("session manager shutdown finished")

    async def _create_session(
        self,
        initial_thys: Optional[List[str]] = None,
        field: str = Server.DEFAULT_FIELD,
        lease_id: Optional[str] = None,
        session_dirs: Optional[List[str]] = None,
        task_group: Optional[str] = None,
        dependency_extra: Optional[str] = None,
    ) -> _Isabelle_Session:
        where = self._where("_create_session")
        # Off the event loop: may spawn the gateway JVM (~40s) and holds a
        # threading lock — inline it and every other request stalls.
        await asyncio.to_thread(self._ensure_gateway)
        if self.thy_init is None:
            self.thy_init = ThyInit()

        normalized_field = self._normalize_field(field)
        dependency_theories = self._normalize_theories(initial_thys)
        dependency_key = self.build_dependency_key(dependency_theories, normalized_field, extra=dependency_extra)
        wrapper_theory: Optional[str] = None

        with logging_context(field=normalized_field):
            try:
                if not dependency_theories:
                    loaded_theories = ["$ISABELLE_REPL_HOME/thys/IsabelleREPL"]
                    logger.info("creating base session with default IsabelleREPL theory")
                else:
                    wrapper_name = self._build_wrapper_theory_name(dependency_theories, normalized_field)
                    logger.info(
                        "generating wrapper theory wrapper=%s dependency_key=%s theories=%s",
                        wrapper_name,
                        dependency_key[:12],
                        dependency_theories,
                    )
                    gen_result = self.thy_init.gen_file(wrapper_name, dependency_theories)
                    if not getattr(gen_result, "data", None):
                        raise RuntimeError(getattr(gen_result, "err", "unknown ThyInit error"))
                    wrapper_theory = f"$ISABELLE_REPL_HOME/thys/{gen_result.data}"
                    loaded_theories = [wrapper_theory]
            except Exception as e:
                logger.exception("failed to prepare initial theory file")
                raise RuntimeError(f"{where}: failed to generate initial theory file: {e}") from e

            # Memory admission control: before allocating a new backend (the real
            # memory consumer), make sure the container has room. Reclaim idle
            # sessions first; only refuse if pressure persists with nothing idle
            # left to evict. Busy/leased sessions are never touched.
            if self.memory_management_enabled:
                # Budget (batch 3): reserved in-flight work + this field's estimate,
                # applied to relief AND the decision (review aa860ea B1).
                extra = self.session_budget_bytes(normalized_field)
                snap = await asyncio.to_thread(
                    self._relieve_memory_pressure, where, extra
                )
                # Teardown -> cgroup accounting lags (poly exit, kernel reclaim):
                # retry briefly before refusing, so a close immediately followed
                # by a create does not 503 on a stale snapshot.
                retries = Memory.ADMISSION_RETRIES
                while (
                    not self.admits_session(snap, normalized_field, extra)
                    and retries > 0
                ):
                    retries -= 1
                    await asyncio.sleep(Memory.ADMISSION_RETRY_DELAY_S)
                    snap = await asyncio.to_thread(
                        self._relieve_memory_pressure, where, extra
                    )
                # The decision: recompute the budget AFTER the awaits above (a
                # concurrent create may have reserved meanwhile) and reserve
                # with no await in between, so the next create sees this one.
                extra = self.session_budget_bytes(normalized_field)
                if not self.admits_session(snap, normalized_field, extra):
                    raise self.create_refused(
                        where, snap
                    )  # MemoryPressure: 503 + facts
            session_id = uuid.uuid4()
            tag = str(session_id)  # the backend's tag in the JVM (backend_reaper.py)
            self.begin_backend_create(tag)
            # Reserved here, the line before the try whose finally releases it
            # (structural: nothing can leak the reservation — review 0710afc S1).
            self.creates_in_flight += 1
            try:
                logger.info("creating raw backend for session_id=%s", session_id)

                def _create_backend():
                    # ALL Py4J traffic (including building the ArrayList) stays
                    # in this worker thread: a synchronous gateway call on the
                    # event loop froze the whole server when the JVM was busy.
                    return self.create_tracked_backend(
                        tag, loaded_theories, session_dirs, normalized_field
                    )

                try:
                    raw_backend = await asyncio.wait_for(
                        asyncio.to_thread(_create_backend),
                        timeout=Timeouts.SESSION_CREATE,
                    )
                except BaseException:  # timeout, error, cancelled request: the JVM may
                    self.abandon_backend_soon(tag)  # still be starting this prover
                    raise
                finally:
                    # the backend exists (or creation failed): release the reservation
                    self.creates_in_flight -= 1
            except TimeoutError:
                # asyncio.TimeoutError alias (3.11+): the request must not
                # outlive a wedged gateway — fail fast with a clean error.
                # The worker thread itself unblocks when the Py4J
                # read_timeout (Repl.PY4J_READ_TIMEOUT) fires.
                logger.error(
                    "session backend creation timed out after %.0fs (gateway wedged?)",
                    Timeouts.SESSION_CREATE,
                )
                raise GatewayUnavailable(
                    f"{where}: session creation timed out after "
                    f"{Timeouts.SESSION_CREATE:.0f}s — gateway unresponsive"
                ) from None
            except Exception as e:
                logger.exception("failed to create raw backend for session_id=%s", session_id)
                raise RuntimeError(f"{where}: failed to create raw backend for session {session_id}: {e}") from e

            try:
                backend = ThreadedBackend(raw_backend, name=f"isabelle-{session_id.hex[:8]}")
            except Exception as e:
                self.abandon_backend_soon(tag)
                logger.exception("failed to create threaded backend for session_id=%s", session_id)
                raise RuntimeError(f"{where}: failed to create threaded backend for session {session_id}: {e}") from e

            try:
                session = MemoryAwareSession(  # _Isabelle_Session + memory facts
                    session_id=session_id,
                    session_theories=dependency_theories,
                    loaded_theories=loaded_theories,
                    dependency_key=dependency_key,
                    wrapper_theory=wrapper_theory,
                    session_field=normalized_field,
                    backend=backend,
                    task_group=task_group,
                )
            except Exception as e:
                try:
                    await asyncio.to_thread(backend.close)
                finally:  # also when the request is cancelled during the close
                    self.end_backend_create(tag)
                logger.exception("failed to create session object for session_id=%s", session_id)
                raise RuntimeError(f"{where}: failed to create session object for session {session_id}: {e}") from e

            try:
                evict_targets: List[Tuple[uuid.UUID, _Isabelle_Session]] = []
                pool_full = False
                with self._lock:
                    self._lru[session_id] = session
                    self._creating_tags.discard(tag)  # owned by the registry from now on
                    if lease_id is not None:
                        session.acquire_lease(lease_id)
                    self._lru.move_to_end(session_id, last=True)
                    # Evict excess sessions, but SKIP sessions that are
                    # actively processing a request or exclusively leased.
                    while len(self._lru) > self.pool_size:
                        # Walk LRU oldest-first looking for an idle candidate.
                        candidate_id: Optional[uuid.UUID] = None
                        for sid in self._lru:
                            if sid == session_id:
                                continue  # never evict the one we just created
                            s = self._lru[sid]
                            if not s.in_use and not s.leased:
                                candidate_id = sid
                                break
                        if candidate_id is None:
                            # Every session is in-use or leased — undo our
                            # insertion; the (blocking) close happens OUTSIDE
                            # the lock so other requests are not stalled.
                            self._lru.pop(session_id, None)
                            pool_full = True
                            break
                        evict_targets.append(
                            (candidate_id, self._lru.pop(candidate_id))
                        )
                if pool_full:
                    metrics.pool_exhausted.labels("all_busy").inc()
                    await asyncio.to_thread(session.close)
                    raise PoolExhausted(
                        f"Session pool is full ({self.pool_size} sessions) "
                        f"and all sessions are actively processing requests or leased. "
                        f"Try again later or increase ISABELLE_POOL_SIZE."
                    )
                # Close evicted sessions OUTSIDE the lock.
                for oldest_id, oldest in evict_targets:
                    try:
                        with logging_context(session_id=oldest_id, field=oldest.field):
                            logger.info("evicting idle LRU session because pool is full")
                            await asyncio.to_thread(oldest.close)
                            metrics.sessions_evicted.labels("lru").inc()
                    except Exception:
                        logger.exception("failed to close evicted session session_id=%s", oldest_id)
            except PoolExhausted:
                raise  # propagate without wrapping
            except Exception as e:
                await asyncio.to_thread(session.close)
                logger.exception("failed to register session session_id=%s", session_id)
                raise RuntimeError(f"{where}: failed to add session {session_id} to session manager: {e}") from e

            logger.info(
                "session created session_id=%s dependency_key=%s wrapper_theory=%s",
                session_id,
                dependency_key[:12],
                wrapper_theory,
            )
            metrics.sessions_created.inc()
            return session

    async def verify_big_step_build(
        self,
        *,
        theory_name: str,
        theory: str,
        dependencies: Optional[List[str]] = None,
        field: Optional[str],
        timeout: float,
    ):
        normalized_field = self._normalize_field(field)
        # The memory gate runs inside verify, only on a cache miss (a theory
        # already verified is served from the cache even under pressure).
        return await self.build_verifier.verify(
            theory_name=theory_name,
            theory_text=theory,
            dependencies=dependencies,
            field=normalized_field,
            timeout=timeout,
            admit=lambda: self.build_admit("bigstep"),
        )

    async def create_session(
        self,
        theories: Optional[List[str]] = None,
        field: str = Server.DEFAULT_FIELD,
    ) -> _Isabelle_Session:
        try:
            return await self._create_session(theories, field)
        except PoolExhausted:
            raise  # preserve 503 mapping (pool full / memory pressure)
        except Exception as e:
            logger.exception("create_session failed")
            raise SessionStartError(f"{self._where('create_session')}: Failed to create session: {e}") from e

    async def create_leased_session(
        self,
        theories: Optional[List[str]] = None,
        field: str = Server.DEFAULT_FIELD,
        task_group: Optional[str] = None,
        session_dirs: Optional[List[str]] = None,
        dependency_extra: Optional[str] = None,
    ) -> Tuple[_Isabelle_Session, str]:
        lease_id = uuid.uuid4().hex[:12]
        try:
            session = await self._create_session(
                theories, field, lease_id=lease_id,
                session_dirs=session_dirs, task_group=task_group,
                dependency_extra=dependency_extra,
            )
            return session, lease_id
        except PoolExhausted:
            raise  # preserve 503 mapping (pool full / memory pressure)
        except Exception as e:
            logger.exception("create_leased_session failed")
            raise SessionStartError(f"{self._where('create_leased_session')}: Failed to create leased session: {e}") from e

    def _find_and_lease_session_locked(
        self,
        *,
        lease_id: str,
        theories: Optional[List[str]] = None,
        field: Optional[str] = None,
        reuse_dirty: bool = True,
        dependency_extra: Optional[str] = None,
    ) -> Optional[_Isabelle_Session]:
        """Find a matching session and atomically attach the lease while holding the manager lock."""
        target_key = self.build_dependency_key(theories, field, extra=dependency_extra)
        for sid, session in reversed(self._lru.items()):
            if session.status != SessionStatus.ACTIVE:
                continue
            if session.leased:
                continue
            if session.dependency_key != target_key:
                continue
            if not reuse_dirty and len(session.command_history) > 0:
                continue
            if not session.try_acquire_lease(lease_id):
                continue
            self._lru.move_to_end(sid, last=True)
            logger.info(
                "found existing session and leased it atomically session_id=%s dependency_key=%s lease_id=%s",
                sid,
                target_key[:12],
                lease_id,
            )
            return session
        return None

    def find_session(
        self,
        theories: Optional[List[str]] = None,
        field: Optional[str] = None,
        reuse_dirty: bool = True,
    ) -> Optional[_Isabelle_Session]:
        target_key = self.build_dependency_key(theories, field)
        with self._lock:
            for sid, session in reversed(self._lru.items()):
                if session.status != SessionStatus.ACTIVE:
                    continue
                if session.leased:
                    continue
                if session.dependency_key != target_key:
                    continue
                if not reuse_dirty and len(session.command_history) > 0:
                    continue
                # Match found — promote to MRU
                self._lru.move_to_end(sid, last=True)
                logger.info(
                    "found existing session session_id=%s dependency_key=%s",
                    sid,
                    target_key[:12],
                )
                return session
        return None

    async def acquire_session(
        self,
        theories: Optional[List[str]] = None,
        field: Optional[str] = None,
        reuse_dirty: bool = True,
        task_group: Optional[str] = None,
        session_dirs: Optional[List[str]] = None,
        dependency_extra: Optional[str] = None,
    ) -> Tuple[_Isabelle_Session, bool, str]:
        lease_id = uuid.uuid4().hex[:12]

        with self._lock:
            existing = self._find_and_lease_session_locked(
                lease_id=lease_id,
                theories=theories,
                field=field,
                reuse_dirty=reuse_dirty,
                dependency_extra=dependency_extra,
            )
        if existing is not None:
            logger.info(
                "session leased (reused) session_id=%s lease_id=%s",
                existing.session_id, lease_id,
            )
            return existing, True, lease_id

        new_session = await self._create_session(
            initial_thys=theories, field=field, lease_id=lease_id,
            session_dirs=session_dirs, task_group=task_group,
            dependency_extra=dependency_extra,
        )
        logger.info(
            "session leased (new) session_id=%s lease_id=%s",
            new_session.session_id, lease_id,
        )
        return new_session, False, lease_id

    def release_session(self, session_id: Union[str, uuid.UUID], lease_id: str) -> bool:
        """Release the exclusive lease so the session returns to the pool
        for reuse by a future ``acquire_session`` call.

        Unlike ``close_session``, the backend is kept alive.
        """
        sid = self._normalize_session_id(session_id)
        with self._lock:
            session = self._lru.get(sid)
            if session is None:
                raise SessionNotFound(f"Session {sid} not found")
            if session.status == SessionStatus.CLOSED:
                raise SessionNotFound(f"Session {sid} is closed")
            session.require_lease(lease_id)
            if session.in_use:
                raise SessionBusyError(f"Session {sid} is busy and cannot be released")
            self.cancel_job_nowait(sid)  # after lease + busy checks (reviews B1, S1)
            session.release_lease()
            self._lru.move_to_end(sid, last=True)
        with logging_context(session_id=sid, field=session.field):
            logger.info("releasing session lease back to pool")
        return True

    def get_session(
        self,
        session_id: Union[str, uuid.UUID],
        *,
        lease_id: Optional[str] = None,
        require_lease: bool = False,
    ) -> _Isabelle_Session:
        sid = self._normalize_session_id(session_id)
        with self._lock:
            if sid not in self._lru:
                raise SessionNotFound(f"Session {sid} not found")

            session = self._lru[sid]
            if session.status == SessionStatus.CLOSED:
                raise SessionNotFound(f"Session {sid} is closed")
            if require_lease:
                session.require_lease(lease_id)

            self._lru.move_to_end(sid, last=True)
            return session

    def close_session(
        self,
        session_id: Union[str, uuid.UUID],
        *,
        lease_id: Optional[str] = None,
        require_lease: bool = True,
    ) -> bool:
        sid = self._normalize_session_id(session_id)
        with self._lock:
            session = self._lru.get(sid)
            if session is None:
                raise SessionNotFound(f"Session {sid} not found")
            if session.status == SessionStatus.CLOSED:
                raise SessionNotFound(f"Session {sid} is closed")
            if require_lease:
                session.require_lease(lease_id)
            if session.in_use:
                raise SessionBusyError(f"Session {sid} is busy and cannot be closed")
            self.cancel_job_nowait(sid)  # after lease + busy checks (reviews B1, S1)
            self._lru.pop(sid, None)
            self.forget_job(sid)  # S4: registry pruned with the session
            if session.in_use:
                self._lru[sid] = session
                self._lru.move_to_end(sid)
                raise SessionBusyError(
                    f"Session {sid} became busy between in_use check and pop"
                )
            if session.leased:
                session.release_lease()
        with logging_context(session_id=sid, field=session.field):
            logger.info("closing session")
            session.close()
        return True

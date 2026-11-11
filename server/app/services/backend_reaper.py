"""No prover outlives its session: tagged backend creation and the orphan reaper.

Every session backend is created with a tag (its session id) that the gateway
JVM registers BEFORE the prover starts (repl_backend_gateway.scala, "Tagged
backends and the reaper"). That gives three guarantees the untagged factory did
not have:

- A creation the server gives up on (``Timeouts.SESSION_CREATE``, an error) is
  stopped in the JVM by its tag (``abandon_backend``), even while it is still
  starting; a backend that arrives after the server gave up is exited at once.
- The cleanup sweep calls ``reap_orphan_backends``: the JVM stops every tagged
  backend whose tag is neither in the session registry (``_lru``) nor in a
  creation in flight. This is the backstop for any path that drops a session
  without stopping its prover.
- Race freedom of the reaper: the JVM numbers registrations; the sweep reads the
  fence (the current number) BEFORE it reads the live set, and the JVM reaps only
  registrations at or before the fence. A creation adds its tag to
  ``_creating_tags`` before the factory call registers it, and the tag leaves
  that set only after it is in ``_lru`` (or the creation failed), so a live
  backend is never reaped.

Mixed into ``SessionManager``; uses ``self._lock``, ``self._lru``, ``self.gateway``.
"""
from __future__ import annotations

import concurrent.futures
import threading
from typing import Any, List, Optional, Set

from server.app.core.logging import get_logger

logger = get_logger(__name__)


class BackendReaperMixin:
    #: Bound on the reaper's Py4J round trip (seconds).
    REAP_CALL_TIMEOUT_S: float = 10.0

    _lock: threading.Lock
    _lru: Any
    gateway: Any
    create_raw_backend: Any

    _creating_tags: Set[str]  # initialized by SessionManager.__init__

    def begin_backend_create(self, tag: str) -> None:
        with self._lock:
            self._creating_tags.add(tag)

    def end_backend_create(self, tag: str) -> None:
        """The creation of ``tag`` is over (registered in ``_lru`` or failed)."""
        with self._lock:
            self._creating_tags.discard(tag)

    def create_tracked_backend(self, tag: str, loaded_theories, session_dirs, field: str):
        """Worker-thread body of a session creation: the tagged factory call. When
        the server gave up on this creation meanwhile (``abandon_backend``), the
        backend that arrives late is exited here and the call fails."""
        raw = self.create_raw_backend(loaded_theories, session_dirs, field, tag=tag)
        with self._lock:
            abandoned = tag not in self._creating_tags
        if abandoned:
            logger.warning("backend %s arrived after its creation was abandoned; exiting it", tag)
            try:
                raw.exit()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("exit of abandoned backend %s failed", tag)
            raise RuntimeError(f"backend {tag} was abandoned during creation")
        return raw

    def abandon_backend(self, tag: str) -> None:
        """Give up on the creation of ``tag``: forget it and stop it in the JVM
        (also while it is still starting). Best effort; blocking (Py4J)."""
        self.end_backend_create(tag)
        gw = self.gateway
        if gw is None:
            return
        try:
            if gw.close_backend(tag):
                logger.warning("stopped abandoned backend %s", tag)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("could not stop abandoned backend %s: %s: %s", tag, type(e).__name__, e)

    def abandon_backend_soon(self, tag: str) -> None:
        """``abandon_backend`` without blocking the caller (an event-loop path,
        possibly a cancelled request): the tag is forgotten at once, the JVM call
        runs on a daemon thread."""
        self.end_backend_create(tag)
        threading.Thread(
            target=self.abandon_backend, args=(tag,), name=f"abandon-{tag[:8]}", daemon=True
        ).start()

    def reap_orphan_backends(self) -> List[str]:
        """Stop every JVM backend that no registered session and no creation in
        flight owns. Blocking (Py4J); the sweep runs it in a worker thread."""
        gw = self.gateway
        if gw is None:
            return []
        def reap() -> List[str]:
            fence = gw.reap_fence()  # BEFORE the live set (see module docstring)
            with self._lock:
                live = [str(sid) for sid in self._lru] + list(self._creating_tags)
            return list(gw.reap_backends(live, fence))

        # Bounded: a wedged JVM answers a Py4J call only at PY4J_READ_TIMEOUT
        # (15 min), which must not hold up the sweep (gateway recovery, idle and
        # memory reclaim). The call thread is left to finish on its own.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            reaped = pool.submit(reap).result(timeout=self.REAP_CALL_TIMEOUT_S)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("orphan backend reaper failed: %s: %s", type(e).__name__, e)
            return []
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        for tag in reaped:
            logger.error(
                "reaped orphan backend %s: its session is no longer registered, "
                "but its prover was still running", tag,
            )
        return reaped

from __future__ import annotations

import concurrent.futures
import contextvars
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

from server.repl.src.python.repl_backend_gateway import ReplBackend
from server.app.core.config import Repl
from server.app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _Job:
    fn: Callable[[], Any]
    fut: concurrent.futures.Future
    ctx: contextvars.Context


class ThreadedBackend:
    EXIT_TIMEOUT: float = Repl.BACKEND_EXIT_TIMEOUT
    JOIN_TIMEOUT: float = Repl.BACKEND_JOIN_TIMEOUT
    QUEUE_POLL_TIMEOUT: float = Repl.BACKEND_QUEUE_POLL
    def __init__(self, backend: ReplBackend, name: str):
        self._backend = backend
        self._name = name
        self._q: queue.Queue[_Job] = queue.Queue()
        self._stop = threading.Event()
        self._shutting_down = threading.Event()
        self._t = threading.Thread(target=self._run, name=name, daemon=True)
        self._t.start()
        logger.info("threaded backend started worker=%s", name)

    @property
    def raw(self) -> ReplBackend:
        return self._backend

    def submit(self, fn: Callable[[], Any]) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if self._shutting_down.is_set():
            fut.set_exception(RuntimeError(f"Backend {self._name} is shutting down"))
            return fut
        self._q.put(_Job(fn=fn, fut=fut, ctx=contextvars.copy_context()))
        logger.debug("job submitted to threaded backend worker=%s queue_size=%s", self._name, self._q.qsize())
        return fut

    def _run(self) -> None:
        logger.info("threaded backend worker loop running worker=%s", self._name)
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=self.QUEUE_POLL_TIMEOUT)
            except queue.Empty:
                continue
            try:
                result = job.ctx.run(job.fn)
                job.fut.set_result(result)
            except Exception as e:
                logger.exception("threaded backend job failed worker=%s", self._name)
                job.fut.set_exception(e)
        logger.info("threaded backend worker loop stopped worker=%s", self._name)

    def close(self) -> None:
        logger.info("closing threaded backend worker=%s", self._name)

        # 1. Signal shutdown — reject new submissions and drain the queue
        #    so no stale jobs try to use a disconnected Py4J gateway.
        self._shutting_down.set()
        cancelled = 0
        while True:
            try:
                job = self._q.get_nowait()
                job.fut.set_exception(
                    RuntimeError(f"Backend {self._name} shut down before job executed")
                )
                cancelled += 1
            except queue.Empty:
                break
        if cancelled:
            logger.info("cancelled %s pending jobs during shutdown worker=%s", cancelled, self._name)

        # 2. Ask the JVM to stop this backend's Isabelle sessions (poly process
        #    trees + in-JVM Isabelle server). exit() runs on its OWN thread, not
        #    queued behind the worker: the worker may still be inside a call
        #    whose request already timed out (a reset or load that outlives its
        #    budget). Queued, exit() waited for that call; after EXIT_TIMEOUT
        #    the worker was stopped and the queued exit() was never run, so the
        #    session left the registry while its prover kept running for hours.
        #    Session_Manager.shutdown (Scala) is idempotent and safe to run
        #    concurrently with the in-flight call; stopping the prover also
        #    ends that call.
        exit_fut = self._run_exit()
        try:
            exit_fut.result(timeout=self.EXIT_TIMEOUT)
        except Exception:
            logger.exception("backend exit failed or timed out during close worker=%s", self._name)
        finally:
            self._stop.set()
            self._t.join(timeout=self.JOIN_TIMEOUT)
            logger.info("threaded backend closed worker=%s", self._name)

    def _run_exit(self) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                fut.set_result(self._backend.exit())
            except BaseException as e:  # pylint: disable=broad-exception-caught
                fut.set_exception(e)

        threading.Thread(target=run, name=f"{self._name}-exit", daemon=True).start()
        return fut

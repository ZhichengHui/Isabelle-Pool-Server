"""No prover outlives its session (process leak, 2026-11).

Observed on a deployment: sessions were gone from ``GET /api/v1/sessions`` while
their ``poly`` processes kept running at 400 % CPU for 7-17 hours under the
gateway JVM. Causes fixed here, one test group each:

- ``ThreadedBackend.close`` queued ``exit()`` behind the worker's in-flight call
  (a reset/load whose request had already timed out); after EXIT_TIMEOUT it
  stopped the worker and the exit job was never run.
- A session creation the server gave up on (timeout, error, cancelled request)
  left the backend the JVM was still starting without any owner.
- Nothing ended prover trees whose session was no longer registered (backstop:
  the tagged-backend reaper), nor trees left behind by a killed gateway JVM, nor
  a prover the JVM-side stop could not end (pid fallback in the session close).

The fake provers here are real process trees shaped like Isabelle's
``bash_process -> bash (setsid) -> poly -> child``, so "ended" means no process
of the tree runs any more.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

import pytest

from server.app.core.config import Timeouts
from server.app.errors import GatewayUnavailable
from server.app.services.memory_monitor import read_starttime
from server.app.services.session_manager import SessionManager
from server.app.services.session_memory import MemoryAwareSession
from server.app.services.threaded_backend import ThreadedBackend
from server.repl.src.python import process_tree
from server.repl.src.python import repl_backend_gateway as gw_mod

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="process trees are read from /proc"
)

# A poly stand-in that ignores SIGTERM and SIGINT (only SIGKILL ends it), with one child.
_STUBBORN_POLY = (
    "import signal, subprocess, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "subprocess.Popen(['sleep', '600'])\n"
    "time.sleep(600)\n"
)


class FakeProver:
    """``bash_process`` (``outer``, in the caller's process group) -> ``bash``
    (``leader``, own session/group via setsid) -> ``poly`` -> a child."""

    def __init__(self, preexec_fn=None, outer_tail: str = "wait"):
        env = dict(os.environ, FAKE_POLY_PY=sys.executable, FAKE_POLY_CODE=_STUBBORN_POLY)
        self.outer = subprocess.Popen(
            ["bash", "-c",
             f"setsid bash -c '\"$FAKE_POLY_PY\" -c \"$FAKE_POLY_CODE\" & wait' & {outer_tail}"],
            preexec_fn=preexec_fn, env=env,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            tree = process_tree.descendants(self.outer.pid)
            leaders = [p for p in tree if process_tree.pgid_of(p) == p]
            if leaders:
                below = process_tree.descendants(leaders[0])
                if len(below) >= 2:  # poly and its child
                    self.leader = leaders[0]
                    self.poly = below[0]
                    self.tree = [self.leader, *below]
                    return
            time.sleep(0.05)
        raise RuntimeError("fake prover did not start")

    def running(self):
        return [p for p in self.tree if process_tree.running(p)]

    def wait_gone(self, timeout: float = 10.0) -> list:
        deadline = time.monotonic() + timeout
        while self.running() and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.running()

    def cleanup(self):
        for p in self.tree:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        try:
            self.outer.kill()
            self.outer.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def prover():
    fp = FakeProver()
    yield fp
    fp.cleanup()


# ------------------------------------------------------------------ process_tree


def test_kill_trees_ends_whole_prover_group_from_poly(prover):
    """From the poly pid, the whole setsid group ends — also a poly that
    ignores SIGTERM (SIGKILL after the grace) and its child."""
    assert process_tree.tree_root(prover.poly) == prover.leader
    process_tree.kill_trees([process_tree.tree_root(prover.poly)], grace=0.3)
    assert prover.wait_gone() == []
    # ``outer`` (bash_process' place: the caller's process group, outside the
    # tree) was not signalled; it ends normally once its child is gone.
    assert prover.outer.wait(timeout=5) == 0


def test_kill_trees_ignores_dead_pids():
    p = subprocess.Popen(["true"])
    p.wait()
    assert process_tree.kill_trees([p.pid, 0, 1]) == []


# ------------------------------------------------------- ThreadedBackend.close


class _ProverBackend:
    """Raw backend stand-in that owns a fake prover. ``long_call`` stands for a
    reset/load that outlives its request: it returns only when the prover is
    gone. ``exit`` does what Session_Manager.shutdown does in the JVM: it ends
    the prover."""

    def __init__(self, fake: FakeProver):
        self.fake = fake
        self.exit_called = threading.Event()
        self.call_started = threading.Event()

    def long_call(self):
        self.call_started.set()
        while self.fake.running():
            time.sleep(0.05)
        raise RuntimeError("prover stopped")

    def exit(self):
        self.exit_called.set()
        process_tree.kill_trees([self.fake.leader], grace=0.3)


def test_close_ends_prover_while_worker_is_blocked(prover, monkeypatch):
    """The request of a long call timed out; the session is closed while the
    worker is still inside that call. exit() must still run (before the fix it
    waited behind the call, then was dropped) and the prover tree must end."""
    monkeypatch.setattr(ThreadedBackend, "EXIT_TIMEOUT", 10.0)
    raw = _ProverBackend(prover)
    tb = ThreadedBackend(raw, name="test-blocked-close")
    fut = tb.submit(raw.long_call)
    assert raw.call_started.wait(5)
    with pytest.raises(TimeoutError):
        fut.result(timeout=0.2)  # the HTTP request gave up

    start = time.monotonic()
    tb.close()
    assert raw.exit_called.is_set()
    assert prover.wait_gone() == []
    assert time.monotonic() - start < 8  # exit did not wait for the call
    with pytest.raises(RuntimeError, match="prover stopped"):
        fut.result(timeout=5)  # stopping the prover also ended the call


def test_close_runs_exit_even_if_the_call_never_returns(monkeypatch):
    """A call that never returns (wedged) does not keep exit() from running."""
    monkeypatch.setattr(ThreadedBackend, "EXIT_TIMEOUT", 2.0)
    monkeypatch.setattr(ThreadedBackend, "JOIN_TIMEOUT", 0.2)
    release = threading.Event()
    exit_called = threading.Event()

    class _Raw:
        def exit(self):
            exit_called.set()

    tb = ThreadedBackend(_Raw(), name="test-wedged-close")
    tb.submit(lambda: release.wait(30))
    time.sleep(0.1)
    try:
        tb.close()
        assert exit_called.is_set()
    finally:
        release.set()


# ------------------------------------------- session close: pid-based fallback


class _NoopExitRaw:
    """The JVM-side stop failed: exit() returns, the prover still runs."""

    def exit(self):
        pass


def _memory_session(raw) -> MemoryAwareSession:
    return MemoryAwareSession(
        session_id=uuid.uuid4(),
        session_theories=[],
        loaded_theories=["x"],
        dependency_key="k",
        wrapper_theory=None,
        session_field="HOL",
        backend=ThreadedBackend(raw, name="test-fallback"),
        task_group="default",
    )


def test_session_close_ends_known_prover_the_backend_left_running(prover):
    session = _memory_session(_NoopExitRaw())
    session.ml_pid = prover.poly
    session.ml_starttime = read_starttime(prover.poly)
    session.close()
    assert prover.wait_gone() == []


def test_session_close_without_known_pid_kills_nothing(prover):
    session = _memory_session(_NoopExitRaw())
    session.close()
    assert prover.running() == prover.tree


def test_session_close_ignores_a_reused_pid(prover):
    """A recorded pid whose start time differs is another process: untouched."""
    session = _memory_session(_NoopExitRaw())
    session.ml_pid = prover.poly
    session.ml_starttime = (read_starttime(prover.poly) or 0) + 1
    session.close()
    assert prover.running() == prover.tree


# ------------------------------------------------- creation the server gave up


class _JavaLists:
    class jvm:  # noqa: N801
        class java:  # noqa: N801
            class util:  # noqa: N801
                @staticmethod
                def ArrayList():  # noqa: N802
                    class _L(list):
                        def add(self, x):
                            self.append(x)

                    return _L()


class _Raw:
    def __init__(self):
        self.exited = threading.Event()

    def exit(self):
        self.exited.set()


class _SlowFactoryGateway:
    """The JVM is still starting the prover when the server gives up."""

    def __init__(self, delay: float):
        self.gateway = _JavaLists()
        self.delay = delay
        self.tags = []
        self.closed = []
        self.raws = []
        self.done = threading.Event()

    def get_repl_backend_tagged(self, tag, *args):
        self.tags.append(tag)
        time.sleep(self.delay)
        raw = _Raw()
        self.raws.append(raw)
        self.done.set()
        return raw

    def close_backend(self, tag):
        self.closed.append(tag)
        return True

    def is_alive(self):
        return True


class _Thy:
    created_files: list = []

    def gen_file(self, name, deps):
        return type("R", (), {"data": f"{name}.thy", "err": None})()


def _manager(gateway) -> SessionManager:
    sm = SessionManager()
    sm.memory_management_enabled = False
    sm.gateway = gateway
    sm._ensure_gateway = lambda: None  # type: ignore[method-assign]
    sm.thy_init = _Thy()
    return sm


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


def test_creation_timeout_stops_the_backend_by_tag(monkeypatch):
    monkeypatch.setattr(Timeouts, "SESSION_CREATE", 0.2)
    gw = _SlowFactoryGateway(delay=1.0)
    sm = _manager(gw)
    with pytest.raises(GatewayUnavailable):
        asyncio.run(sm._create_session(["Main"], "HOL"))
    tag = gw.tags[0]
    assert _wait(lambda: gw.closed == [tag])  # stopped in the JVM by its tag
    assert tag not in sm._creating_tags
    # The backend that arrives late is exited at once, never registered.
    assert gw.done.wait(5)
    assert _wait(lambda: gw.raws[0].exited.is_set())
    assert not sm._lru


def test_cancelled_creation_stops_the_backend(monkeypatch):
    """A client that disconnects cancels the request; the prover the JVM is
    starting for it must not be left behind."""
    gw = _SlowFactoryGateway(delay=1.0)
    sm = _manager(gw)

    async def run():
        task = asyncio.create_task(sm._create_session(["Main"], "HOL"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert _wait(lambda: len(gw.closed) == 1)
    assert gw.done.wait(5)
    assert _wait(lambda: gw.raws[0].exited.is_set())
    assert not sm._creating_tags


def test_successful_creation_hands_the_tag_to_the_registry():
    gw = _SlowFactoryGateway(delay=0.0)
    sm = _manager(gw)
    session = asyncio.run(sm._create_session(["Main"], "HOL"))
    assert gw.tags == [str(session.session_id)]
    assert not sm._creating_tags and not gw.closed
    assert not gw.raws[0].exited.is_set()
    assert session.session_id in sm._lru


# ----------------------------------------------------------------- the reaper


class _ReapGateway:
    def __init__(self, reaped):
        self.calls = []
        self.reaped = reaped
        self.fence = 41

    def reap_fence(self):
        self.calls.append(("fence",))
        return self.fence

    def reap_backends(self, live, fence):
        self.calls.append(("reap", sorted(live), fence))
        return list(self.reaped)

    def is_alive(self):
        return True


def test_reaper_keeps_registered_and_in_flight_backends():
    gw = _ReapGateway(reaped=["orphan-tag"])
    sm = SessionManager()
    sm.gateway = gw
    sid = uuid.uuid4()
    sm._lru[sid] = object()
    sm.begin_backend_create("starting-tag")
    assert sm.reap_orphan_backends() == ["orphan-tag"]
    assert gw.calls == [("fence",), ("reap", sorted([str(sid), "starting-tag"]), 41)]


def test_reaper_survives_an_old_gateway_without_the_methods():
    sm = SessionManager()
    sm.gateway = object()  # a JVM built before this fix
    assert sm.reap_orphan_backends() == []


def test_cleanup_sweep_runs_the_reaper():
    gw = _ReapGateway(reaped=[])
    sm = SessionManager()
    sm.memory_management_enabled = False
    sm.gateway = gw
    asyncio.run(sm.cleanup_once())
    assert ("fence",) in gw.calls and any(c[0] == "reap" for c in gw.calls)


# ------------------------------------------------- gateway JVM termination


def test_gateway_terminate_ends_prover_trees_outside_its_group(monkeypatch):
    """The prover groups are not in the gateway's process group (setsid), so
    the group SIGTERM/SIGKILL left them running when the JVM died."""
    fp = FakeProver(preexec_fn=os.setsid, outer_tail="sleep 600")
    try:
        proc = gw_mod.ReplBackendGatewayProcess.__new__(gw_mod.ReplBackendGatewayProcess)
        proc.process = fp.outer
        proc.gateway = type("G", (), {"shutdown": lambda self: None})()
        monkeypatch.setattr(gw_mod.ReplBackendGatewayProcess, "TERMINATE_WAIT", 0.5)
        proc.terminate()
        assert fp.wait_gone() == []
    finally:
        fp.cleanup()

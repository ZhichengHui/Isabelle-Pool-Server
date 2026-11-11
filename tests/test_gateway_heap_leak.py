"""Gateway JVM heap leak (2026-11-11): sessions that ended must leave nothing in
the gateway JVM.

The live tests start their own gateway JVM (never the pool server's) and need an
Isabelle installation with the HOL heap and the REPL component:

    ISABELLE_LIVE_TESTS=1 ISABELLE_HOME=... pytest tests/test_gateway_heap_leak.py

They run tests/gateway_leak_probe.py: every way a backend can end (normal exit,
Session_Start failure, failing initial use_theories, abandoned creation, exit
after the session's manager thread died) is repeated, and afterwards the JVM must
hold no session, server, manager, backend or prover, and no poly may run.

The unit test at the end needs no JVM.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

from server.repl.src.python import repl_backend_gateway as gw_mod

LIVE = os.environ.get("ISABELLE_LIVE_TESTS") == "1" and sys.platform.startswith("linux")
live = pytest.mark.skipif(
    not LIVE, reason="live test: set ISABELLE_LIVE_TESTS=1 and ISABELLE_HOME (HOL heap)"
)

RETAINED = ("sessions", "servers", "managers", "backends", "provers")


def _check_nothing_retained(report, scenario):
    steps = [s for s in report["steps"] if s["scenario"] == scenario]
    assert steps, scenario
    # after the pending Event_Timer delays had their chance to fire (bounded
    # retention); a leaked session stays
    last = report["drained"]
    base = report["baseline"]
    for key in RETAINED:
        assert last[key] <= base[key], (scenario, key, base[key], last[key], steps)
    assert last["poly"] == 0, (scenario, steps)
    # Session state is gone too: commands of the probe theory and the heap.
    assert last["commands"] <= base["commands"], (scenario, steps)
    assert last["live_mb"] - base["live_mb"] < 64, (scenario, base, last)


@live
@pytest.mark.parametrize(
    "scenario", ["normal", "start_fail", "load_fail", "abandon", "prover_killed", "stop_fail"]
)
def test_ended_backends_leave_nothing(scenario):
    sys.path.insert(0, os.path.dirname(__file__))
    import gateway_leak_probe as probe

    report = probe.run(cycles=3, scenarios=[scenario], lemmas=100, settle=3.0, drain=45.0)
    _check_nothing_retained(report, scenario)
    steps = [s for s in report["steps"] if s["scenario"] == scenario]
    teardowns = [s["forced_teardowns"] for s in steps]
    if scenario == "stop_fail":
        # one forced teardown per failed stop; it is not a fatal JVM error
        assert teardowns == [1] * len(steps), steps
        assert report["alive"] is True
    else:
        # a stop that completed (also with a nonzero prover return code) is not forced
        assert teardowns == [0] * len(steps), steps
    assert report["fatal_error"] in ("", None)


@live
def test_thread_death_by_oom_marks_gateway_dead():
    """A thread that dies of an OutOfMemoryError makes alive() answer false, so
    the server replaces the gateway (instead of serving from a full heap)."""
    gw = gw_mod.ReplBackendGatewayProcess()
    try:
        jvm = gw.gateway.jvm
        assert gw.is_alive()
        handler = jvm.java.lang.Thread.getDefaultUncaughtExceptionHandler()
        handler.uncaughtException(
            jvm.java.lang.Thread.currentThread(),
            jvm.java.lang.OutOfMemoryError("leak probe"),
        )
        assert jvm.repl.ReplBackendGateway.alive() is False
        assert "leak probe" in jvm.repl.ReplBackendGateway.fatal_error()
        assert gw.is_alive() is False
    finally:
        gw.terminate()


class _FakeGatewayObject:
    def __init__(self, alive: bool, reason: str):
        self._alive, self._reason = alive, reason

    def alive(self):
        return self._alive

    def fatal_error(self):
        return self._reason


def test_is_alive_logs_why_the_gateway_is_dead(caplog):
    proc = gw_mod.ReplBackendGatewayProcess.__new__(gw_mod.ReplBackendGatewayProcess)

    class _Proc:
        returncode = None

        def poll(self):
            return None

    proc.process = _Proc()
    fake = _FakeGatewayObject(False, 'thread "Isabelle.Session.manager" died: OOM')
    proc.gateway = type("G", (), {})()
    proc.gateway.jvm = type("J", (), {})()
    proc.gateway.jvm.repl = type("R", (), {"ReplBackendGateway": fake})()
    with caplog.at_level(logging.ERROR):
        assert proc.is_alive() is False
    assert "Isabelle.Session.manager" in caplog.text

    fake._alive = True
    assert proc.is_alive() is True

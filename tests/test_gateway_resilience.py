"""2026-09-10 incident hardening tests (issue 5, recommendations 1-3):

- Rec 1: the gateway JVM's stdout/stderr are redirected to durable log files
  and GC logging is injected into the JVM env (unless already configured).
- Rec 2: every Py4J connection carries a read_timeout, and the liveness probe
  logs (not swallows) probe exceptions.
- Rec 3: session backend creation is bounded by Timeouts.SESSION_CREATE and
  raises GatewayUnavailable (not a 57-minute hang).

No Isabelle backend needed — subprocess/Py4J/gateway are fakes.
"""
from __future__ import annotations

import threading

import pytest

from server.repl.src.python import repl_backend_gateway as gw_mod
from server.app.core.config import Timeouts
from server.app.errors import GatewayUnavailable
from server.app.services.session_manager import SessionManager


# ---------------------------------------------------------------- Rec 1/2: gateway process

class _FakeStream:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0)

    def __iter__(self):
        return iter(self._lines)


class _FakePopen:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.stdout = _FakeStream(["37963\n", "jvm says hello\n"])
        self.returncode = None
        self.pid = 999_999_999  # no such process: the gateway group is not protected

    def poll(self):
        return self.returncode


class _FakeJavaGateway:
    def __init__(self, gateway_parameters):
        self.params = gateway_parameters


def test_gateway_process_wires_logging_and_read_timeout(monkeypatch, tmp_path):
    """Rec 1+2: JVM stdout/stderr are wired to a durable log; Py4J gets read_timeout."""
    popen_kwargs = {}

    def fake_popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return _FakePopen(*args, **kwargs)

    monkeypatch.setattr(gw_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(gw_mod, "JavaGateway", _FakeJavaGateway)
    monkeypatch.setattr(gw_mod, "_gateway_log_dir", lambda: tmp_path)
    # preexec_fn=os.setsid is POSIX-only; the gateway only ever spawns in the
    # Linux container, so stub it for the Windows dev/test host.
    monkeypatch.setattr(gw_mod.os, "setsid", lambda: None, raising=False)

    proc = gw_mod.ReplBackendGatewayProcess()

    # stderr is a file handle into the log dir, not sys.stderr
    stderr_target = popen_kwargs["stderr"]
    assert getattr(stderr_target, "name", "").endswith("gateway-jvm.log")
    # Py4J read timeout is set from config
    assert proc.gateway.params.read_timeout == gw_mod.Repl.PY4J_READ_TIMEOUT
    # stdout pump copied the post-port line into the durable log
    import time as _t

    for _ in range(50):
        if (tmp_path / "gateway-jvm.log").read_text().count("jvm says hello"):
            break
        _t.sleep(0.02)
    assert "jvm says hello" in (tmp_path / "gateway-jvm.log").read_text()


# ---------------------------------------------------------------- Rec 2: probe logging

class _RaisingGateway:
    def __init__(self):
        self.probe_calls = 0

    def is_alive(self):
        self.probe_calls += 1
        raise ConnectionError("socket went away")

    def terminate(self):
        pass


def _bare_manager(gateway):
    mgr = SessionManager.__new__(SessionManager)
    mgr.gateway = gateway
    mgr._lock = threading.Lock()
    mgr._lru = {}
    mgr._creating_tags = set()
    mgr.thy_init = object()
    return mgr


def test_probe_exception_is_logged_not_swallowed(caplog):
    mgr = _bare_manager(_RaisingGateway())
    with caplog.at_level("WARNING"):
        assert mgr.gateway_alive() is False
    assert any("liveness probe raised" in r.message for r in caplog.records)


# ---------------------------------------------------------------- Rec 3: create timeout

def test_backend_creation_wall_timeout(monkeypatch):
    """A wedged gateway must turn into GatewayUnavailable within the budget."""
    import asyncio

    mgr = SessionManager.__new__(SessionManager)
    mgr.gateway = object()
    mgr._lock = threading.Lock()
    mgr._lru = {}
    mgr._creating_tags = set()
    mgr.thy_init = object()
    mgr.memory_management_enabled = False

    # Shrink the budget so the test doesn't actually wait 600 s.
    monkeypatch.setattr(Timeouts, "SESSION_CREATE", 0.2)

    async def _selective_sleep(fn, *args):
        # Only the backend-creation closure wedges; other to_thread calls
        # (e.g. _ensure_gateway) return immediately.
        if getattr(fn, "__name__", "") == "_create_backend":
            await asyncio.sleep(5)
        return None

    monkeypatch.setattr(asyncio, "to_thread", _selective_sleep)
    with pytest.raises(GatewayUnavailable):
        asyncio.run(mgr._create_session())

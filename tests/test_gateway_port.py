"""The gateway's Py4J port is the first all-digit line of the JVM's stdout: a JVM warning
printed before it (hsperfdata pid collision across pid namespaces) used to be parsed as the
port and fail the server start."""
from __future__ import annotations

import io

import pytest

gw = pytest.importorskip("server.repl.src.python.repl_backend_gateway")


def _proc():
    p = object.__new__(gw.ReplBackendGatewayProcess)  # no JVM: only _read_port is used
    p._jvm_log = io.StringIO()
    return p


def test_port_after_a_jvm_warning():
    p = _proc()
    warning = "OpenJDK 64-Bit Server VM warning: Could not map the hsperfdata file\n"
    assert p._read_port(io.StringIO(warning + "40123\nlater output\n")) == 40123
    assert p._jvm_log.getvalue() == warning


def test_no_port_is_an_error():
    with pytest.raises(RuntimeError, match="exited before printing its port"):
        _proc()._read_port(io.StringIO("Error: could not start\n"))

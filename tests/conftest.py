"""Shared pytest fixtures.

The server's base logger (``isabelle_pool_server``, server/app/core/logging.py) has
``propagate = False`` so its records are not emitted twice via the root
fallback handlers. pytest's ``caplog`` only listens on the root logger, so
without help no assertion on ``caplog.records`` can ever see a server log line
(2026-10-05: test_cleanup_offloop / test_gateway_resilience had been failing
since they were written). This fixture attaches caplog's handler to the base
logger for the duration of every test.
"""
from __future__ import annotations

import logging

import pytest

from server.app.core.logging import BASE_LOGGER_NAME


@pytest.fixture(autouse=True)
def _capture_server_logs(caplog):
    base = logging.getLogger(BASE_LOGGER_NAME)
    base.addHandler(caplog.handler)
    try:
        yield
    finally:
        base.removeHandler(caplog.handler)


@pytest.fixture(autouse=True)
def _no_scratch_spares(monkeypatch):
    """The LSP MCP prepares spare scratch sessions in the background
    (mcp_servers/lsp/scratch_spare.py); a test that scripts a fake client's
    responses or counts its sessions would see those extra calls. Off by
    default; tests/test_mcp_lsp_scratch_spare.py turns it on."""
    from mcp_servers.lsp.config import Config

    monkeypatch.setattr(Config, "SCRATCH_SPARE", 0)

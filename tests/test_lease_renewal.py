"""Lease renewal: a held session that is renewed but not used is not reaped as
abandoned (server), and the LSP MCP renews every lease it holds and keeps warm
scratch sessions however long they were idle (client side)."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid

from server.app.services.session import SessionStatus
from server.app.services.session_manager import SessionManager
from server.app.services.session_memory import expiry_fields, lease_alive_at


class _LeasedSession:
    status = SessionStatus.ACTIVE
    leased = True
    lease_id = "L"
    field = "HOL"

    def __init__(self, last_activity: float, renewed: float):
        self.last_activity = last_activity
        self.lease_renewed_at = renewed

    def is_idle(self, timeout, now=None):
        return ((time.time() if now is None else now) - self.last_activity) > timeout


def _manager(sessions):
    mgr = SessionManager.__new__(SessionManager)
    mgr._lock = threading.Lock()
    mgr._lru = dict(sessions)
    mgr.idle_timeout = 1.0
    mgr.max_lease_age = 100.0
    mgr.memory_management_enabled = False
    mgr.gateway = None
    closed = []

    def close(sid, *, require_lease=True):
        closed.append(sid)
        mgr._lru.pop(sid, None)
        return True

    mgr.close_session = close
    return mgr, closed


def test_reaper_spares_a_renewed_lease_and_closes_an_abandoned_one():
    now = time.time()
    renewed, abandoned = uuid.uuid4(), uuid.uuid4()
    mgr, closed = _manager({
        renewed: _LeasedSession(last_activity=now - 1000, renewed=now - 10),
        abandoned: _LeasedSession(last_activity=now - 1000, renewed=0.0),
    })
    asyncio.run(mgr.cleanup_once())
    assert closed == [abandoned]


def test_expiry_counts_from_the_renewal():
    now = 10_000.0
    s = _LeasedSession(last_activity=now - 500, renewed=now - 20)
    assert lease_alive_at(s) == now - 20
    assert expiry_fields(s, now, 1.0, 100.0)["expires_in_s"] == 80.0


# ------------------------------------------------------------------ LSP MCP side


class _FakeClient:
    def __init__(self):
        self.renewed = []
        self.closed = []
        self.n = 0

    async def create_session(self, **kw):
        self.n += 1
        return {"session_id": f"s{self.n}", "lease_id": f"l{self.n}"}

    async def renew_lease(self, session_id, *, lease_id=None):
        self.renewed.append((session_id, lease_id))
        return {"success": True}

    async def close_session(self, session_id, *, lease_id=None):
        self.closed.append(session_id)


def _pool():
    from mcp_servers.lsp.pool import LspPool

    pool = LspPool()
    pool._client = _FakeClient()
    return pool


def test_renew_all_renews_every_held_lease():
    from mcp_servers.lsp import lease_renewal

    pool = _pool()
    key = pool.scratch_key("default", None, ["Main"], None)

    async def run():
        a = await pool.acquire_scratch(key)
        b = await pool.acquire_scratch(key)
        await pool.release_scratch(key, *a)  # warm and idle: still held, still renewed
        return a, b, await lease_renewal.renew_all(pool)

    a, b, n = asyncio.run(run())
    assert n == 2 and sorted(pool._client.renewed) == sorted([a, b])


def test_warm_scratch_session_is_reused_however_long_it_was_idle(monkeypatch):
    pool = _pool()
    key = pool.scratch_key("default", None, ["Main"], None)

    async def run():
        a = await pool.acquire_scratch(key)
        await pool.release_scratch(key, *a)
        later = time.time() + 10_000  # far beyond any former idle TTL
        monkeypatch.setattr(time, "time", lambda: later)
        return a, await pool.acquire_scratch(key)

    a, b = asyncio.run(run())
    assert a == b and pool._client.closed == []


def test_renewal_task_runs_in_the_background(monkeypatch):
    from mcp_servers.lsp import lease_renewal
    from mcp_servers.lsp.config import Config

    monkeypatch.setattr(Config, "LEASE_RENEW_S", 0.01)
    pool = _pool()
    key = pool.scratch_key("default", None, ["Main"], None)

    async def run():
        a = await pool.acquire_scratch(key)  # starts the renewal task
        await asyncio.sleep(0.05)
        monkeypatch.setattr(Config, "LEASE_RENEW_S", 0.0)  # let the loop end
        await asyncio.sleep(0.02)
        return a

    a = asyncio.run(run())
    assert a in pool._client.renewed
    assert lease_renewal  # imported for the module under test

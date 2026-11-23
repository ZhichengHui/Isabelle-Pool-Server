"""idle_release: a resident (streamable-http) LSP MCP releases what no client used for
IDLE_RELEASE_S — open files (warm release; the next call reopens) and warm scratch
sessions — and skips a file whose binding is busy."""
from __future__ import annotations

import asyncio
import time

from mcp_servers.common import KIND_BINDING
from mcp_servers.lsp import idle_release
from mcp_servers.lsp.config import Config
from mcp_servers.lsp.pool import FileBinding, LspPool


def _pool_with(files: dict[str, float], now: float) -> tuple[LspPool, list]:
    """An LspPool holding one binding per path, last used `age` seconds before `now`;
    close_binding / retire_idle_scratch are recorded instead of calling a server."""
    pool = LspPool()
    calls: list = []
    for i, (path, age) in enumerate(files.items()):
        sid = f"s{i}"
        pool._bindings[path] = FileBinding(file_path=path, session_id=sid, lease_id=f"L{i}",
                                           task_group="default")
        pool.registry.register(sid, f"L{i}", KIND_BINDING, path)
        pool.registry.touch(sid, now=now - age)

    async def close_binding(path, destroy=False):
        calls.append(("close", path, destroy))
        pool._bindings.pop(path, None)
        return True

    async def retire_idle_scratch(max_idle_s=0.0):
        calls.append(("retire", max_idle_s))
        return 0

    pool.close_binding = close_binding  # type: ignore[method-assign]
    pool.retire_idle_scratch = retire_idle_scratch  # type: ignore[method-assign]
    return pool, calls


def test_releases_only_files_idle_past_the_limit_and_retires_scratch():
    now = time.time()
    pool, calls = _pool_with({"/w/old.thy": 4000.0, "/w/recent.thy": 10.0}, now)
    released = asyncio.run(idle_release.release_once(pool, 1800.0, now=now))
    assert released == ["/w/old.thy"]
    assert ("close", "/w/old.thy", False) in calls  # warm release, not destroy
    assert ("retire", 1800.0) in calls
    assert "/w/recent.thy" in pool._bindings


def test_skips_a_file_whose_binding_is_busy():
    now = time.time()
    pool, calls = _pool_with({"/w/busy.thy": 4000.0}, now)

    async def run():
        binding = pool._bindings["/w/busy.thy"]
        async with binding.lock:  # a sync / tool call in progress
            return await idle_release.release_once(pool, 1800.0, now=now)

    assert asyncio.run(run()) == []
    assert not [c for c in calls if c[0] == "close"]


def test_peek_binding_quiet_does_not_reset_the_idle_clock():
    now = time.time()
    pool, _ = _pool_with({"/w/a.thy": 4000.0}, now)
    assert pool.peek_binding_quiet("/w/a.thy") is not None
    entry = pool.registry.idle(min_idle_s=1800.0, now=now, kinds=(KIND_BINDING,))
    assert [e.owner for e in entry] == ["/w/a.thy"]


def test_disabled_by_default_starts_no_task(monkeypatch):
    monkeypatch.setattr(Config, "IDLE_RELEASE_S", 0.0)
    pool = LspPool()

    async def run():
        idle_release.ensure(pool)
        return getattr(pool, "_idle_release_task", None)

    assert asyncio.run(run()) is None


def test_enabled_starts_one_task(monkeypatch):
    monkeypatch.setattr(Config, "IDLE_RELEASE_S", 1800.0)
    pool = LspPool()

    async def run():
        idle_release.ensure(pool)
        first = pool._idle_release_task
        idle_release.ensure(pool)
        same = pool._idle_release_task is first
        first.cancel()
        return same

    assert asyncio.run(run()) is True

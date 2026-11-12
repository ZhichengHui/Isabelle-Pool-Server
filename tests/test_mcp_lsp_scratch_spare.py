"""Spare scratch sessions (mcp_servers/lsp/scratch_spare.py): after a file is
bound, and after each multi_attempt, one idle scratch session per context is
prepared in the background and loaded with the file's text, so the next
multi_attempt neither creates a session nor checks the prefix from scratch."""
from __future__ import annotations

import asyncio
import json

import pytest

from mcp_servers.lsp.config import Config
from tests.test_mcp_lsp_server import FakeClient

TEXT = "theory Fan imports Main begin\nlemma a: True by simp\nend\n"


def _fresh(monkeypatch, spare=1):
    pytest.importorskip("mcp")
    from mcp_servers.lsp import app as lsp_app
    from mcp_servers.lsp import server as lsp_server

    monkeypatch.setattr(Config, "SCRATCH_SPARE", spare)
    pool = lsp_server.pool
    # fresh state for this test, restored afterwards: the pool is module-level, and sessions
    # left in it would serve later tests' calls (which then never reach their fake client)
    fake = FakeClient()
    for name, value in [("_bindings", {}), ("_scratch", {}), ("_scratch_counts", {}),
                        ("_spare_pending", {}), ("_client", fake)]:
        monkeypatch.setattr(pool, name, value)
    return lsp_app, pool, fake


async def _settle(pool):
    while pool._spare_tasks:
        await asyncio.gather(*list(pool._spare_tasks), return_exceptions=True)


def _scratch_created(fake):
    return [c for c in fake.created if (c["label"] or "").startswith("scratch")]


def test_open_prepares_a_loaded_spare(tmp_path, monkeypatch):
    lsp_app, pool, fake = _fresh(monkeypatch)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        await _settle(pool)

    asyncio.run(run())
    assert len(_scratch_created(fake)) == 1
    (queue,) = pool._scratch.values()
    spare_sid = queue._queue[0][0]
    assert {"session_id": spare_sid, "text": TEXT, "report": True, "master_dir": str(f.parent)} in fake.loads
    assert queue.qsize() == 1 and not pool._spare_pending


def test_multi_attempt_uses_the_spare_and_keeps_one_ready(tmp_path, monkeypatch):
    lsp_app, pool, fake = _fresh(monkeypatch)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        await _settle(pool)
        before = len(_scratch_created(fake))
        out = json.loads(await lsp_app.isabelle_multi_attempt(
            file_path=str(f), line=2, candidates=["lemma b: True by simp"]))
        after_call = len(_scratch_created(fake))
        await _settle(pool)
        return before, after_call, out

    before, after_call, out = asyncio.run(run())
    assert before == 1 and after_call == 1  # the candidate ran on the spare
    assert out[0]["candidate"] == "lemma b: True by simp"
    (queue,) = pool._scratch.values()
    assert queue.qsize() == 1  # it went back warm; no second spare needed


def test_spare_off(tmp_path, monkeypatch):
    lsp_app, pool, fake = _fresh(monkeypatch, spare=0)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        await _settle(pool)

    asyncio.run(run())
    assert _scratch_created(fake) == [] and not pool._spare_tasks


def test_memory_refusal_means_no_spare(tmp_path, monkeypatch):
    lsp_app, pool, fake = _fresh(monkeypatch)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        fake.fail_create_503 = True  # binding exists; the scratch create is refused
        await _settle(pool)

    asyncio.run(run())
    assert _scratch_created(fake) == [] and not pool._spare_pending
    assert all(q.qsize() == 0 for q in pool._scratch.values())


def test_first_multi_attempt_finds_a_spare_per_candidate(tmp_path, monkeypatch):
    """Candidates run in parallel, so a spare helps only if each finds one: open prepares
    min(SCRATCH_SPARE, MAX_PARALLEL) distinct, loaded spares, and the first multi_attempt with
    that many candidates creates no session."""
    lsp_app, pool, fake = _fresh(monkeypatch, spare=4)
    monkeypatch.setattr(Config, "MAX_PARALLEL", 4)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)
    cands = [f"lemma c{i}: True by simp" for i in range(4)]

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        await _settle(pool)
        (queue,) = pool._scratch.values()
        spares = sorted(sid for sid, _, _ in queue._queue)
        before = len(_scratch_created(fake))
        out = json.loads(await lsp_app.isabelle_multi_attempt(
            file_path=str(f), line=2, candidates=cands))
        after = len(_scratch_created(fake))
        await _settle(pool)
        return spares, before, after, out, queue.qsize()

    spares, before, after, out, idle = asyncio.run(run())
    assert len(set(spares)) == 4 and before == after == 4
    assert all({"session_id": s, "text": TEXT, "report": True, "master_dir": str(f.parent)} in fake.loads for s in spares)
    assert [r["candidate"] for r in out] == cands and idle == 4


def test_spares_capped_by_max_parallel(tmp_path, monkeypatch):
    lsp_app, pool, fake = _fresh(monkeypatch, spare=4)
    monkeypatch.setattr(Config, "MAX_PARALLEL", 2)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        await _settle(pool)

    asyncio.run(run())
    assert len(_scratch_created(fake)) == 2


def test_spares_are_created_concurrently(tmp_path, monkeypatch):
    """The spares' sessions are requested at once (the server creates them in parallel);
    requested one after another, 4 AutoCorrode spares took 80 s, longer than the agent's pause."""
    lsp_app, pool, fake = _fresh(monkeypatch, spare=4)
    monkeypatch.setattr(Config, "MAX_PARALLEL", 4)
    inflight = {"now": 0, "max": 0}
    create = fake.create_session

    async def slow_create(*args, **kwargs):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.05)
        inflight["now"] -= 1
        return await create(*args, **kwargs)

    monkeypatch.setattr(fake, "create_session", slow_create)
    f = tmp_path / "Fan.thy"
    f.write_text(TEXT)

    async def run():
        await lsp_app.isabelle_open(file_path=str(f))
        await _settle(pool)

    asyncio.run(run())
    assert inflight["max"] == 4 and len(_scratch_created(fake)) == 4


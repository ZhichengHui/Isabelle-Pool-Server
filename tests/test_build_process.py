"""build_process: a build that runs into its timeout, or is cancelled, is ended with every process
it started (the launcher's children are where `isabelle build` does its work)."""
import asyncio
import os
import time

import pytest

from server.app.services import build_process


def _group_alive(pgid, within_s=2.0):
    """Whether the group still exists after `within_s` (a signalled child takes a moment to be
    reaped once its parent is gone)."""
    deadline = time.monotonic() + within_s
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        if time.monotonic() > deadline:
            return True
        time.sleep(0.05)


def test_a_timeout_ends_the_whole_process_group(tmp_path):
    procs = []
    # the launcher starts a child that would outlive it, like isabelle -> java/poly
    cmd = ["bash", "-c", "sleep 60 & echo started; wait"]
    rc, timed_out = asyncio.run(build_process.run(
        cmd, tmp_path / "b.log", timeout_s=0.5, on_start=procs.append))
    assert timed_out and rc is not None
    assert not _group_alive(procs[0].pid)
    assert "started" in build_process.tail(tmp_path / "b.log")


def test_a_normal_end_keeps_the_log(tmp_path):
    rc, timed_out = asyncio.run(build_process.run(
        ["bash", "-c", "echo Finished X; exit 3"], tmp_path / "b.log"))
    assert (rc, timed_out) == (3, False)
    assert build_process.tail(tmp_path / "b.log").strip() == "Finished X"


def test_cancelling_the_waiter_ends_the_group(tmp_path):
    procs = []

    async def main():
        task = asyncio.ensure_future(build_process.run(
            ["bash", "-c", "sleep 60 & wait"], tmp_path / "b.log", on_start=procs.append))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert not _group_alive(procs[0].pid)

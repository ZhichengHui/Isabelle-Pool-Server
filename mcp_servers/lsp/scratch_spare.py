"""Spare scratch sessions: keep idle scratch sessions per context ready, already loaded with the
file's text, so that isabelle_multi_attempt does not wait for a new Isabelle session (~20 s) and
its first full check of the file prefix (~20 s).

Spares are prepared in the background, while the agent thinks: after a file is first bound
(`schedule_for_binding`) and topped up after each multi_attempt (`ensure`). Each context keeps
min(Config.SCRATCH_SPARE, Config.MAX_PARALLEL) of them (0 disables), within the room left under
Config.SCRATCH_POOL_SIZE: the candidates of a multi_attempt run in parallel (at most MAX_PARALLEL
at once) and the call is as slow as its slowest candidate, so a spare helps only if every
candidate finds one — also on the first call, before any fan-out is known. The context is the scratch
key of the file (task group, heap, header imports, field), derived from the file itself, so
nothing is configured per heap. Because a spare has loaded the file, the next multi_attempt's
`prefix + candidate` load takes the server's incremental path. Any failure (memory pressure, a
pool at its cap, a timeout) only means fewer spares: a candidate without one creates a session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from .config import Config
from .pool import FileBinding, LspPool, field_for, header_fields, master_dir_of

logger = logging.getLogger(__name__)


def ensure(pool: LspPool, key: Tuple, text: str, master_dir: Optional[str] = None) -> None:
    """Start preparing spares for `key` with `text` until min(SCRATCH_SPARE, MAX_PARALLEL) are
    idle or being prepared."""
    want = min(Config.SCRATCH_SPARE, Config.MAX_PARALLEL)
    if want <= 0 or not text:
        return
    queue = pool._scratch.get(key)
    idle = queue.qsize() if queue is not None else 0
    pending = pool._spare_pending.get(key, 0)
    if idle + pending >= want:
        return
    # new sessions must fit under the context's session cap
    creatable = Config.SCRATCH_POOL_SIZE - pool._scratch_counts.get(key, 0) - pending
    n = min(want - pending, idle + creatable)
    if n > idle:
        pool._spare_pending[key] = pending + n
        _spawn(pool, _prepare(pool, key, text, n, master_dir))


async def _prepare(
    pool: LspPool, key: Tuple, text: str, n: int, master_dir: Optional[str] = None,
) -> None:
    """Acquire `n` scratch sessions concurrently (the idle ones first, then new ones, which the
    server creates in parallel), load the text into each, and release them only when all are
    done. Released one by one, the next acquire would get back the session just released and
    no new session would be made; acquired one after another, the creations were serialized
    (4 AutoCorrode spares took 80 s, longer than the agent's pause)."""
    finished = 0
    all_finished = asyncio.Event()

    def finish() -> None:
        nonlocal finished
        finished += 1
        if finished == n:
            all_finished.set()

    async def one() -> None:
        entered = False
        try:
            async with pool.scratch_session(key) as (sid, lease):
                entered = True
                try:
                    c = await pool.client()
                    await c.load_document(sid, text, report=True,
                                          timeout=Config.ATTEMPT_TIMEOUT, lease_id=lease,
                                          master_dir=master_dir)
                finally:
                    finish()
                    await all_finished.wait()  # hold the session until every spare is loaded
        finally:
            if not entered:
                finish()  # the acquire failed: do not keep the others waiting

    try:
        outcomes = await asyncio.gather(*(one() for _ in range(n)), return_exceptions=True)
        failed = [o for o in outcomes if isinstance(o, BaseException)]
        if failed:  # fewer spares is a normal outcome
            logger.info("%d of %d scratch spares not prepared for %s: %s",
                        len(failed), n, key, failed[0])
    finally:
        left = pool._spare_pending.get(key, n) - n
        if left > 0:
            pool._spare_pending[key] = left
        else:
            pool._spare_pending.pop(key, None)


def schedule_for_binding(pool: LspPool, binding: FileBinding) -> None:
    """Once per binding: work out the file's scratch key (header parse, in the background) and
    start preparing its spares."""
    if Config.SCRATCH_SPARE <= 0 or binding.spare_scheduled or not binding.cached_text:
        return
    binding.spare_scheduled = True
    text = binding.cached_text

    async def run() -> None:
        try:
            c = await pool.client()
            master_dir = master_dir_of(binding.file_path)
            imports, suggested = await header_fields(c, text, master_dir)
            key = pool.scratch_key(binding.task_group, binding.heap_session, imports,
                                   field_for(suggested))
        except Exception as e:  # noqa: BLE001
            logger.info("scratch spare: no key for %s: %s", binding.file_path, e)
            return
        ensure(pool, key, text, master_dir)

    _spawn(pool, run())


def _spawn(pool: LspPool, coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    pool._spare_tasks.add(task)
    task.add_done_callback(pool._spare_tasks.discard)

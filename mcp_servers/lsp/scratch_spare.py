"""Spare scratch sessions: keep Config.SCRATCH_SPARE idle scratch sessions per context ready,
already loaded with the file's text, so that isabelle_multi_attempt does not wait for a new
Isabelle session (~20 s) and its first full check of the file prefix (~20 s).

A spare is prepared in the background, while the agent thinks: after a file is first bound
(`schedule_for_binding`) and after each multi_attempt (`ensure`). The context is the scratch key
of the file (task group, heap, header imports, field), derived from the file itself, so nothing
is configured per heap. Because the spare has loaded the file, the next multi_attempt's
`prefix + candidate` load takes the server's incremental path. Any failure (memory pressure, a
pool at its cap, a timeout) only means no spare: the call that needs a scratch session then
creates one as before.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Tuple

from .config import Config
from .pool import FileBinding, LspPool, field_for, header_fields

logger = logging.getLogger(__name__)


def ensure(pool: LspPool, key: Tuple, text: str) -> None:
    """Start preparing a spare for `key` with `text` unless enough are idle or being prepared,
    or the context is at Config.SCRATCH_POOL_SIZE."""
    if Config.SCRATCH_SPARE <= 0 or not text or key in pool._spare_pending:
        return
    queue = pool._scratch.get(key)
    idle = queue.qsize() if queue is not None else 0
    if idle >= Config.SCRATCH_SPARE or pool._scratch_counts.get(key, 0) >= Config.SCRATCH_POOL_SIZE:
        return
    pool._spare_pending.add(key)
    _spawn(pool, _prepare(pool, key, text))


async def _prepare(pool: LspPool, key: Tuple, text: str) -> None:
    try:
        async with pool.scratch_session(key) as (sid, lease):
            c = await pool.client()
            await c.load_document(sid, text, report=True,
                                  timeout=Config.ATTEMPT_TIMEOUT, lease_id=lease)
    except Exception as e:  # noqa: BLE001  (no spare is a normal outcome)
        logger.info("scratch spare not prepared for %s: %s", key, e)
    finally:
        pool._spare_pending.discard(key)


def schedule_for_binding(pool: LspPool, binding: FileBinding) -> None:
    """Once per binding: work out the file's scratch key (header parse, in the background) and
    start preparing a spare for it."""
    if Config.SCRATCH_SPARE <= 0 or binding.spare_scheduled or not binding.cached_text:
        return
    binding.spare_scheduled = True
    text = binding.cached_text

    async def run() -> None:
        try:
            c = await pool.client()
            imports, suggested = await header_fields(c, text)
            key = pool.scratch_key(binding.task_group, binding.heap_session, imports,
                                   field_for(suggested))
        except Exception as e:  # noqa: BLE001
            logger.info("scratch spare: no key for %s: %s", binding.file_path, e)
            return
        ensure(pool, key, text)

    _spawn(pool, run())


def _spawn(pool: LspPool, coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    pool._spare_tasks.add(task)
    task.add_done_callback(pool._spare_tasks.discard)

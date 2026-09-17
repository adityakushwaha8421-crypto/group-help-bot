"""Job enqueueing. Redis/arq by default; INLINE_JOBS=true runs jobs in-process through the bounded, per-case
ordered runner in app.workers.runner."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.utils.logging import get_logger

log = get_logger("queue")
_pool = None


async def get_pool():
    global _pool
    if _pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def enqueue(func_name: str, *args: Any, job_id: str | None = None, defer_seconds: int = 0) -> None:
    s = get_settings()
    if s.inline_jobs:
        from app.workers.runner import get_runner

        await get_runner().submit(func_name, *args, job_id=job_id, defer_seconds=defer_seconds)
        return
    pool = await get_pool()
    await pool.enqueue_job(func_name, *args, _job_id=job_id, _defer_by=defer_seconds or None)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None

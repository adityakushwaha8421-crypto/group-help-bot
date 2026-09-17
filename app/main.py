"""Entry point: FastAPI (health + case timeline API) and the Telegram input bot in one process.
Background jobs run in the arq worker (or in-process when INLINE_JOBS=true)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.config import get_settings
from app.db.session import create_all, dispose
from app.utils.logging import configure_logging, get_logger

log = get_logger("main")
_stop = asyncio.Event()
_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.log_level)
    if s.is_sqlite:
        await create_all()
    from app.telegram.input_bot import run_polling

    if s.telegram_bot_token:
        _tasks.append(asyncio.create_task(run_polling(_stop)))
    else:
        from app.config import ENV_FILE

        log.error(
            "TELEGRAM_BOT_TOKEN is not set - the input bot will NOT start. Check that the .env exists and is readable",
            env_file=str(ENV_FILE),
            env_file_exists=ENV_FILE.exists(),
        )
    if s.betix_monitor_mode == "user":
        from app.telegram.betix_monitor import run_user_monitor

        _tasks.append(asyncio.create_task(run_user_monitor(_stop)))
    if s.inline_jobs:
        from app.followups.scheduler import run_forever
        from app.workers.tasks import poster_factory, recover

        await recover()
        _tasks.append(asyncio.create_task(run_forever(poster_factory, s.followup_sweep_seconds, _stop)))
    log.info("application started", env=s.app_env, inline_jobs=s.inline_jobs)
    try:
        yield
    finally:
        _stop.set()
        for t in _tasks:
            t.cancel()
        await asyncio.gather(*_tasks, return_exceptions=True)
        from app.admin.pool import close_pool
        from app.workers.runner import get_runner

        await get_runner().shutdown()  # queued jobs are recovered from the database on the next start
        await close_pool()  # the shared Illunise browser
        await dispose()


app = FastAPI(title="Betix Payment Verifier", lifespan=lifespan)


@app.get("/health")
async def health():
    """Liveness plus a look at the load: job queue, Illunise browser tabs, AI reads, Telegram sends, DB pool."""
    from app.admin.pool import get_pool
    from app.ai.analyzer import ai_stats
    from app.db.session import get_engine
    from app.telegram.throttle import get_throttle
    from app.workers.runner import get_runner

    pool = get_engine().pool
    db = (
        {"in_use": pool.checkedout(), "size": pool.size(), "overflow": pool.overflow()}
        if hasattr(pool, "checkedout")
        else {}
    )
    return {
        "ok": True,
        "jobs": get_runner().stats(),
        "browser": get_pool().stats(),
        "ai": ai_stats(),
        "telegram": get_throttle().stats(),
        "db": db,
    }


def main() -> None:
    s = get_settings()
    uvicorn.run("app.main:app", host=s.api_host, port=s.api_port, log_level=s.log_level.lower())


if __name__ == "__main__":
    main()

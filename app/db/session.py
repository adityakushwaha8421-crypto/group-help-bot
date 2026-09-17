"""Async engine / session factory."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        kwargs: dict = {
            "pool_pre_ping": True,  # a connection dropped by a Postgres restart is noticed, not used
            "pool_size": s.db_pool_size,
            "max_overflow": s.db_max_overflow,
            "pool_timeout": s.db_pool_timeout_seconds,
            "pool_recycle": 1800,
        }
        if s.is_sqlite:
            os.makedirs(
                os.path.dirname(s.database_url.split("///")[-1]) or ".", exist_ok=True
            ) if ":memory:" not in s.database_url else None
            kwargs = {}
        _engine = create_async_engine(s.database_url, **kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _sessionmaker


@asynccontextmanager
async def session_scope():
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Create tables directly (used by tests / sqlite dev). Production uses alembic."""
    from app.db.models import Base

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None

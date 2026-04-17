"""
db.py — Async database layer using SQLAlchemy asyncio + asyncpg.

Replaces all psycopg2 synchronous calls throughout the codebase.
Provides:
  - async_engine: SQLAlchemy AsyncEngine
  - AsyncSession: session factory
  - get_session(): FastAPI dependency (async generator)
  - get_raw_conn(): raw asyncpg connection for health probe
  - init_db(): called at FastAPI startup
  - close_db(): called at FastAPI shutdown
"""
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def _build_async_url(database_url: str) -> str:
    """Convert postgresql:// -> postgresql+asyncpg://"""
    url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database not initialised -- call init_db() first")
    return _engine


async def init_db(database_url: str | None = None) -> None:
    global _engine, _session_factory
    url = database_url or os.environ.get("DATABASE_URL", "")
    if not url:
        return  # DB optional in test/dev without Postgres
    async_url = _build_async_url(url)
    _engine = create_async_engine(
        async_url,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def close_db() -> None:
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async DB session."""
    if _session_factory is None:
        yield None  # graceful degradation -- DB not configured
        return
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_ctx() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for use outside FastAPI (worker jobs, cron tasks)."""
    if _session_factory is None:
        yield None
        return
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_raw_conn():
    """Return a raw asyncpg connection for health probes. Caller must close it."""
    if _engine is None:
        return None
    return await _engine.raw_connection()

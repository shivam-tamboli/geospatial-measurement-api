"""Async engine / session factory and the FastAPI session dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

_engine_kwargs: dict[str, object] = {"pool_pre_ping": True}
if not _settings.database_url.startswith("sqlite"):
    _engine_kwargs.update(pool_size=_settings.db_pool_size, max_overflow=_settings.db_max_overflow)

engine = create_async_engine(_settings.database_url, **_engine_kwargs)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session; roll back on error."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

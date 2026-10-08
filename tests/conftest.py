"""Test configuration. Env vars must be set before ``app`` is imported."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator

_TMP = tempfile.mkdtemp(prefix="geo_api_tests_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{_TMP}/uploads"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["LOG_FORMAT"] = "console"
os.environ["MAX_UPLOAD_SIZE_MB"] = "1"
os.environ["FRONTEND_DIST_DIR"] = f"{_TMP}/no-frontend"  # a local frontend/dist build must not shadow API 404s

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import models  # noqa: E402,F401


@pytest_asyncio.fixture(autouse=True)
async def _database() -> AsyncIterator[None]:
    """Fresh schema per test; dispose the pool so connections never cross event loops."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to the ASGI app. Background tasks finish before a call returns."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

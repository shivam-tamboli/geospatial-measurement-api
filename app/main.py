"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes import files
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.middleware import MaxBodySizeMiddleware
from app.core.static import mount_frontend
from app.core.logging import configure_logging, request_id_ctx
from app.db.base import Base
from app.db.session import async_session_factory, engine
from app.models import models  # noqa: F401  (register tables on Base.metadata)
from app.services import file_service

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Create tables / upload dir on startup and fail jobs orphaned by the previous process."""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_factory() as session:
        interrupted = await file_service.fail_interrupted_jobs(session)
    if interrupted:
        logger.warning("Marked interrupted jobs as FAILED", extra={"count": interrupted})
    logger.info("Application started", extra={"environment": settings.environment})
    yield
    await engine.dispose()
    logger.info("Application stopped")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Upload Shapefile (ZIP) or KML files and get per-feature area / length measurements.",
    lifespan=lifespan,
)
register_exception_handlers(app)
app.include_router(files.router)


@app.middleware("http")
async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Attach a request ID to logs and emit one access-log line per request."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = request_id_ctx.set(request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)
    response.headers["x-request-id"] = request_id
    logger.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            "request_id": request_id,
        },
    )
    return response


# Registered before CORS so CORS stays outermost and the browser can read this 413.
app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_upload_bytes)

if settings.allowed_origins:
    # Added last so it is the outermost middleware and also answers preflight requests.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )


@app.get("/health", tags=["health"], summary="Liveness / DB readiness probe", response_model=None)
async def health() -> dict[str, str] | JSONResponse:
    """Return 200 when the database is reachable, otherwise 503 ``DB_UNAVAILABLE``."""
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any driver/pool error means "not ready"
        logger.exception("Health check: database unavailable")
        return JSONResponse(status_code=503, content={"detail": "Database unavailable", "code": "DB_UNAVAILABLE"})
    return {"status": "ok"}


# Must stay last: the "/" mount would otherwise shadow routes registered after it.
mount_frontend(app, settings.frontend_dist_dir)

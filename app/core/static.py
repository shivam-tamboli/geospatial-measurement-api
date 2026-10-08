"""Serve the built single-page frontend from FastAPI."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

logger = logging.getLogger(__name__)

# Paths under these prefixes are API/docs routes: an unknown one must stay a JSON 404, never index.html.
_NON_SPA_PREFIXES = ("api/", "docs", "redoc", "openapi.json", "health")


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to ``index.html`` for unknown, extension-less, non-API paths."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            last_segment = path.rsplit("/", 1)[-1]
            is_page_route = "." not in last_segment and not path.startswith(_NON_SPA_PREFIXES)
            if exc.status_code == 404 and is_page_route:
                return await super().get_response("index.html", scope)
            raise


def mount_frontend(app: FastAPI, dist_dir: Path) -> bool:
    """Mount the built frontend at ``/`` if ``dist_dir`` exists. Must be called after all API routes.

    Returns:
        ``True`` if mounted, ``False`` if there is no build (API-only mode).
    """
    if not (dist_dir / "index.html").is_file():
        logger.info("No frontend build found; serving API only", extra={"dist_dir": str(dist_dir)})
        return False
    app.mount("/", SPAStaticFiles(directory=dist_dir, html=True), name="frontend")
    logger.info("Serving frontend", extra={"dist_dir": str(dist_dir)})
    return True

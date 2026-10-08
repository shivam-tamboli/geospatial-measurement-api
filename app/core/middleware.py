"""ASGI middleware."""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# A multipart upload's Content-Length includes boundaries and part headers on top of the file itself.
# Allow for that so a file exactly at the limit is not rejected here; the service enforces the exact
# limit on the real file bytes.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class MaxBodySizeMiddleware:
    """Reject requests whose declared ``Content-Length`` exceeds the upload limit, before reading the body.

    Starlette spools a whole multipart body before the route handler runs, so without this a
    multi-gigabyte upload would consume bandwidth and disk before ever getting a 413. This check
    answers from the headers alone.

    Requests without a ``Content-Length`` (chunked transfer) or with an unparseable one are passed
    through; for those, the streaming size check in ``file_service`` is the fallback.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, overhead_bytes: int = MULTIPART_OVERHEAD_BYTES) -> None:
        self.app = app
        self.limit = max_bytes + overhead_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            declared = _content_length(scope)
            if declared is not None and declared > self.limit:
                logger.warning(
                    "Rejected oversized request by Content-Length",
                    extra={"content_length": declared, "limit_bytes": self.limit, "path": scope.get("path")},
                )
                response = JSONResponse(
                    status_code=413, content={"detail": "File too large", "code": "FILE_TOO_LARGE"}
                )
                await response(scope, receive, send)  # `receive` is never called: the body stays unread
                return
        await self.app(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    """Return the declared Content-Length, or ``None`` if absent or malformed."""
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                length = int(value)
            except ValueError:
                return None
            return length if length >= 0 else None
    return None

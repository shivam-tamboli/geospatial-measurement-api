"""Structured logging setup (JSON for production, readable console for development)."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes present on every LogRecord; anything else came from ``extra=``.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object, including ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_ctx.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-friendly formatter that still appends ``extra`` fields as key=value."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = " ".join(
            f"{k}={v}" for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
        )
        return f"{base} {extras}".rstrip()


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Uvicorn's access log is replaced by our request middleware.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

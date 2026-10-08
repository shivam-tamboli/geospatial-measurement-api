"""Domain exceptions and the HTTP error contract: ``{"detail": "...", "code": "..."}``."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for errors that are safe to show to API clients."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"

    def __init__(self, detail: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class InvalidFileTypeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_FILE_TYPE"


class EmptyFileError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "EMPTY_FILE"


class FileTooLargeError(AppError):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    code = "FILE_TOO_LARGE"


class UploadedFileNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "FILE_NOT_FOUND"


class FileNotReadyError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "FILE_NOT_READY"


class FileProcessingFailedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "FILE_PROCESSING_FAILED"


class CorruptFileError(AppError):
    """The upload could not be parsed as a valid Shapefile / KML."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "CORRUPT_FILE"


def _error_response(status_code: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "code": code})


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers so every error leaves the API in the same shape."""

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, exc.detail, exc.code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"] if p != "body")
            parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        return _error_response(422, "; ".join(parts) or "Invalid request", "VALIDATION_ERROR")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return _error_response(exc.status_code, str(exc.detail), codes.get(exc.status_code, "HTTP_ERROR"))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception", extra={"error_type": type(exc).__name__})
        return _error_response(500, "An internal error occurred.", "INTERNAL_ERROR")

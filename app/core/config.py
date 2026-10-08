"""Application settings, loaded once from environment variables (and ``.env``)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, failing fast on malformed values."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _normalise_database_url(url: str) -> str:
    """Force the asyncpg driver.

    Managed providers (Render, Heroku) hand out ``postgres://`` or
    ``postgresql://`` URLs, which SQLAlchemy would resolve to a sync driver.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def _parse_origins(raw: str | None, environment: str) -> tuple[str, ...]:
    """Parse ``ALLOWED_ORIGINS`` (comma-separated). Unset means ``*`` in development, none elsewhere."""
    if raw is None:
        return ("*",) if environment == "development" else ()
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, typed view over the process environment."""

    app_name: str
    environment: str
    log_level: str
    log_format: str  # "json" | "console"
    database_url: str
    db_pool_size: int
    db_max_overflow: int
    upload_dir: Path
    max_upload_size_mb: int
    max_extracted_size_mb: int
    max_zip_members: int
    default_page_size: int
    max_page_size: int
    list_default_page_size: int
    list_max_page_size: int
    allowed_origins: tuple[str, ...]
    frontend_dist_dir: Path

    @property
    def max_upload_bytes(self) -> int:
        """Upload size limit in bytes."""
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def max_extracted_bytes(self) -> int:
        """Maximum total uncompressed size of a ZIP archive, in bytes (zip-bomb guard)."""
        return self.max_extracted_size_mb * 1024 * 1024


def _load_settings() -> Settings:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required (see .env.example)")

    environment = os.getenv("ENVIRONMENT", "development")
    return Settings(
        app_name=os.getenv("APP_NAME", "Geospatial Measurement API"),
        environment=environment,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_format=os.getenv("LOG_FORMAT", "json").lower(),
        database_url=_normalise_database_url(database_url),
        db_pool_size=_env_int("DB_POOL_SIZE", 5),
        db_max_overflow=_env_int("DB_MAX_OVERFLOW", 10),
        upload_dir=Path(os.getenv("UPLOAD_DIR", "uploads")),
        max_upload_size_mb=_env_int("MAX_UPLOAD_SIZE_MB", 50),
        max_extracted_size_mb=_env_int("MAX_EXTRACTED_SIZE_MB", 500),
        max_zip_members=_env_int("MAX_ZIP_MEMBERS", 200),
        default_page_size=_env_int("DEFAULT_PAGE_SIZE", 50),
        max_page_size=_env_int("MAX_PAGE_SIZE", 500),
        list_default_page_size=_env_int("LIST_DEFAULT_PAGE_SIZE", 20),
        list_max_page_size=_env_int("LIST_MAX_PAGE_SIZE", 100),
        allowed_origins=_parse_origins(os.getenv("ALLOWED_ORIGINS"), environment),
        frontend_dist_dir=Path(os.getenv("FRONTEND_DIST_DIR", "frontend/dist")),
    )


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return _load_settings()

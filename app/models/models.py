"""ORM models: an uploaded file and the features extracted from it."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# JSONB on PostgreSQL; plain JSON elsewhere (lets the test-suite run on SQLite).
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    """Client-side timestamp: microsecond precision on every backend, so newest-first ordering is stable."""
    return datetime.now(timezone.utc)


class FileStatus(str, enum.Enum):
    """Lifecycle of an uploaded file."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class UploadedFile(Base):
    """Metadata for an uploaded geospatial file."""

    __tablename__ = "uploaded_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[FileStatus] = mapped_column(
        Enum(FileStatus, name="file_status"), default=FileStatus.PENDING, index=True
    )
    feature_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crs: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-fatal problems found while parsing (e.g. KML layers that were skipped).
    warnings: Mapped[list[str]] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    features: Mapped[list[Feature]] = relationship(
        back_populates="file", cascade="all, delete-orphan", passive_deletes=True
    )


class Feature(Base):
    """One geometry (plus attributes and measurement) from an uploaded file."""

    __tablename__ = "features"
    __table_args__ = (Index("ix_features_file_id_index", "file_id", "feature_index", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="CASCADE")
    )
    feature_index: Mapped[int] = mapped_column(Integer)
    geometry_type: Mapped[str] = mapped_column(String(64))
    geometry_wkt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 2D GeoJSON geometry reprojected to WGS84, ready for web maps.
    geometry_geojson: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    crs: Mapped[str] = mapped_column(Text)
    properties: Mapped[dict] = mapped_column(JSONType, default=dict)

    measurement_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "area" | "length"
    measurement_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    measurement_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    measurement_crs: Mapped[str | None] = mapped_column(Text, nullable=True)

    file: Mapped[UploadedFile] = relationship(back_populates="features")

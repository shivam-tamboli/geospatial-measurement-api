"""Pydantic v2 request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.models import FileStatus


class ErrorResponse(BaseModel):
    """Uniform error body returned for every non-2xx response."""

    detail: str
    code: str


class FileUploadResponse(BaseModel):
    """Returned immediately after upload; processing continues in the background."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: FileStatus


class FileInfo(BaseModel):
    """File metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    feature_count: int | None = Field(None, description="Null until processing completes")
    crs: str | None = Field(
        None,
        description="Original CRS: 'EPSG:<code>' only for exact registry matches, otherwise 'CUSTOM:<WKT prefix>'. "
        "Null until processing completes",
    )
    status: FileStatus
    error: str | None = Field(
        None, validation_alias=AliasChoices("error_message", "error"), description="Failure reason when status is FAILED"
    )
    warnings: list[str] = Field(
        default_factory=list, description="Non-fatal problems, e.g. KML layers that were skipped (file is still COMPLETED)"
    )
    file_size: int
    created_at: datetime
    updated_at: datetime


class FileSummary(BaseModel):
    """Compact file entry for listings."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: FileStatus
    feature_count: int | None = None
    crs: str | None = None
    created_at: datetime


class FileListPage(BaseModel):
    """Paginated list of uploaded files, newest first."""

    page: int
    page_size: int
    total: int
    total_pages: int
    items: list[FileSummary]


class Measurement(BaseModel):
    """A single measurement. All fields are null for geometries without one (e.g. Point)."""

    type: str | None = Field(None, description="'area' or 'length'")
    value: float | None = None
    unit: str | None = Field(None, description="'m²' for area, 'm' for length")
    projected_crs: str | None = Field(None, description="Projected CRS the value was computed in")


class FeatureMeasurement(BaseModel):
    """One feature with its geometry, attributes and measurement."""

    feature_id: int = Field(description="Zero-based index of the feature within the file")
    geometry_type: str
    geometry: str | None = Field(None, description="Geometry as WKT in the original CRS")
    geometry_geojson: dict[str, Any] | None = Field(
        None, description="2D GeoJSON geometry reprojected to WGS84 (EPSG:4326), for rendering on maps"
    )
    crs: str = Field(description="Original CRS of the file")
    properties: dict[str, Any]
    measurement: Measurement


class MeasurementsPage(BaseModel):
    """Paginated measurements."""

    file_id: uuid.UUID
    page: int
    page_size: int
    total: int
    total_pages: int
    items: list[FeatureMeasurement]

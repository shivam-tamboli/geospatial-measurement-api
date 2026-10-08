"""Pydantic v2 request/response schemas.

Every field carries an example so the interactive docs at ``/docs`` show realistic values.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.models import FileStatus

_FILE_ID = "d518ba8f-66b0-491e-8c45-86af0a5acb53"

_POLYGON_ITEM: dict[str, Any] = {
    "feature_id": 0,
    "geometry_type": "Polygon",
    "geometry": "POLYGON Z ((77 28 0, 77.01 28 0, 77.01 28.01 0, 77 28.01 0, 77 28 0))",
    "geometry_geojson": {
        "type": "Polygon",
        "coordinates": [[[77.0, 28.0], [77.01, 28.0], [77.01, 28.01], [77.0, 28.01], [77.0, 28.0]]],
    },
    "crs": "EPSG:4326",
    "properties": {"Name": "Plot A", "Description": ""},
    "measurement": {"type": "area", "value": 1090165.1617245723, "unit": "m²", "projected_crs": "EPSG:32643"},
}
_POINT_ITEM: dict[str, Any] = {
    "feature_id": 2,
    "geometry_type": "Point",
    "geometry": "POINT (77.005 28.005)",
    "geometry_geojson": {"type": "Point", "coordinates": [77.005, 28.005]},
    "crs": "EPSG:4326",
    "properties": {"Name": "Well", "Description": ""},
    "measurement": {"type": None, "value": None, "unit": None, "projected_crs": None},
}


class ErrorResponse(BaseModel):
    """Uniform error body returned for every non-2xx response."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"detail": "File 'd518ba8f-…' not found.", "code": "FILE_NOT_FOUND"}]}
    )

    detail: str = Field(description="Human-readable explanation, safe to show to end users.", examples=["File not found."])
    code: str = Field(
        description="Stable machine-readable error code; branch on this, not on `detail`.", examples=["FILE_NOT_FOUND"]
    )


class FileUploadResponse(BaseModel):
    """Returned immediately after upload; processing continues in the background."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={"examples": [{"id": _FILE_ID, "filename": "survey.kml", "status": "PENDING"}]},
    )

    id: uuid.UUID = Field(description="Use this id to poll `GET /api/files/{id}/`.", examples=[_FILE_ID])
    filename: str = Field(description="Client filename, reduced to its base name.", examples=["survey.kml"])
    status: FileStatus = Field(description="Always `PENDING` at upload time.", examples=["PENDING"])


class FileInfo(BaseModel):
    """File metadata and processing status."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": _FILE_ID,
                    "filename": "survey.kml",
                    "feature_count": 3,
                    "crs": "EPSG:4326",
                    "status": "COMPLETED",
                    "error": None,
                    "warnings": [],
                    "file_size": 557,
                    "created_at": "2026-10-08T22:21:08.192249Z",
                    "updated_at": "2026-10-08T22:21:08.213687Z",
                }
            ]
        },
    )

    id: uuid.UUID = Field(examples=[_FILE_ID])
    filename: str = Field(examples=["survey.kml"])
    feature_count: int | None = Field(
        None, description="Number of features read. Null until processing completes.", examples=[3]
    )
    crs: str | None = Field(
        None,
        description=(
            "Original CRS of the file. `EPSG:<code>` only when the CRS exactly matches that EPSG definition, "
            "otherwise `CUSTOM:` followed by the first 100 characters of its WKT. Null until processing completes."
        ),
        examples=["EPSG:4326", "EPSG:32643"],
    )
    status: FileStatus = Field(
        description="`PENDING` → `PROCESSING` → `COMPLETED` or `FAILED`.", examples=["COMPLETED"]
    )
    error: str | None = Field(
        None,
        validation_alias=AliasChoices("error_message", "error"),
        description="Why processing failed. Only set when status is `FAILED`.",
        examples=["'parcels.shp' does not declare a coordinate reference system (missing .prj). Include the .prj file in the ZIP."],
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal problems. The file is still `COMPLETED`, but part of it was not read.",
        examples=[["KML layer 'Roads' could not be read and was skipped; its features are missing."]],
    )
    file_size: int = Field(description="Size of the upload in bytes.", examples=[557])
    created_at: datetime = Field(examples=["2026-10-08T22:21:08.192249Z"])
    updated_at: datetime = Field(examples=["2026-10-08T22:21:08.213687Z"])


class FileSummary(BaseModel):
    """Compact file entry used in listings."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(examples=[_FILE_ID])
    filename: str = Field(examples=["survey.kml"])
    status: FileStatus = Field(examples=["COMPLETED"])
    feature_count: int | None = Field(None, description="Null until processing completes.", examples=[3])
    crs: str | None = Field(None, description="Same labelling rules as `FileInfo.crs`.", examples=["EPSG:4326"])
    created_at: datetime = Field(examples=["2026-10-08T22:21:08.192249Z"])


class FileListPage(BaseModel):
    """One page of uploaded files, newest first."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "page": 1,
                    "page_size": 20,
                    "total": 1,
                    "total_pages": 1,
                    "items": [
                        {
                            "id": _FILE_ID,
                            "filename": "survey.kml",
                            "status": "COMPLETED",
                            "feature_count": 3,
                            "crs": "EPSG:4326",
                            "created_at": "2026-10-08T22:21:08.192249Z",
                        }
                    ],
                }
            ]
        }
    )

    page: int = Field(description="Current 1-based page number.", examples=[1])
    page_size: int = Field(description="Maximum items per page.", examples=[20])
    total: int = Field(description="Total number of uploaded files.", examples=[42])
    total_pages: int = Field(description="Number of pages; 0 when there are no files.", examples=[3])
    items: list[FileSummary]


class Measurement(BaseModel):
    """A single measurement. Every field is null for geometries that have none (e.g. a Point)."""

    type: str | None = Field(None, description="`area` for polygons, `length` for lines.", examples=["area", "length"])
    value: float | None = Field(None, description="Measured value in `unit`.", examples=[1090165.1617245723])
    unit: str | None = Field(None, description="`m²` for area, `m` for length.", examples=["m²", "m"])
    projected_crs: str | None = Field(
        None,
        description="UTM CRS the geometry was reprojected to before measuring (chosen from the feature's centroid).",
        examples=["EPSG:32643"],
    )


class FeatureMeasurement(BaseModel):
    """One feature with its geometry, attributes and measurement."""

    feature_id: int = Field(
        description="Zero-based index of the feature within the file (numbered consecutively across all layers).",
        examples=[0],
    )
    geometry_type: str = Field(
        description="Shapely geometry type: Polygon, MultiPolygon, LineString, Point, GeometryCollection, …",
        examples=["Polygon"],
    )
    geometry: str | None = Field(
        None,
        description="Geometry as WKT, exactly as in the file and in its original CRS. Null for features without geometry.",
        examples=["POLYGON Z ((77 28 0, 77.01 28 0, 77.01 28.01 0, 77 28.01 0, 77 28 0))"],
    )
    geometry_geojson: dict[str, Any] | None = Field(
        None,
        description=(
            "The same geometry reprojected to WGS84 (EPSG:4326) as 2D GeoJSON with 6 decimal places, ready for a web map. "
            "Null only for features without usable geometry."
        ),
        examples=[{"type": "Point", "coordinates": [77.005, 28.005]}],
    )
    crs: str = Field(description="Original CRS of this feature's source layer.", examples=["EPSG:4326"])
    properties: dict[str, Any] = Field(
        description="Attribute table / KML extended data as a flat JSON object.",
        examples=[{"Name": "Plot A", "Description": ""}],
    )
    measurement: Measurement


class MeasurementsPage(BaseModel):
    """One page of per-feature measurements, ordered by `feature_id`."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "file_id": _FILE_ID,
                    "page": 1,
                    "page_size": 50,
                    "total": 3,
                    "total_pages": 1,
                    "items": [_POLYGON_ITEM, _POINT_ITEM],
                }
            ]
        }
    )

    file_id: uuid.UUID = Field(examples=[_FILE_ID])
    page: int = Field(description="Current 1-based page number.", examples=[1])
    page_size: int = Field(description="Maximum items per page.", examples=[50])
    total: int = Field(description="Total number of features in the file.", examples=[3])
    total_pages: int = Field(description="Number of pages; 0 when the file has no features.", examples=[1])
    items: list[FeatureMeasurement]

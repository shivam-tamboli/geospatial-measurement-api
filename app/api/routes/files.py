"""HTTP routes for file upload, metadata and measurements. Routes delegate to services."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Path, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session
from app.schemas.schemas import ErrorResponse, FileInfo, FileListPage, FileUploadResponse, MeasurementsPage
from app.services import file_service

_settings = get_settings()

router = APIRouter(prefix="/api/files", tags=["files"])

_EXAMPLE_ID = "d518ba8f-66b0-491e-8c45-86af0a5acb53"
_FILE_ID_PARAM = Path(description="Id returned by the upload endpoint.", examples=[_EXAMPLE_ID])
_PAGE_PARAM = Query(1, ge=1, description="1-based page number.", examples=[1])


def _error(description: str, **examples: tuple[str, str, str]) -> dict[str, Any]:
    """Build an OpenAPI response entry whose examples show the real ``{"detail", "code"}`` body.

    Args:
        description: Shown next to the status code.
        **examples: ``name=(summary, detail, code)`` for each distinct error that status can carry.
    """
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "examples": {
                    name: {"summary": summary, "value": {"detail": detail, "code": code}}
                    for name, (summary, detail, code) in examples.items()
                }
            }
        },
    }


_ERR_VALIDATION = (
    "Invalid request parameter",
    "query.page: Input should be greater than or equal to 1",
    "VALIDATION_ERROR",
)
_ERR_BAD_ID = (
    "Malformed file id",
    "path.file_id: Input should be a valid UUID, invalid length: expected length 32 for simple format, found 3",
    "VALIDATION_ERROR",
)


@router.post(
    "/",
    response_model=FileUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["files"],
    summary="Upload a Shapefile (.zip) or KML file",
    description=(
        "Accepts a multipart upload (form field `file`): either a `.zip` containing a Shapefile "
        "(`.shp`, `.shx`, `.dbf` and a `.prj` that declares the CRS) or a `.kml`. The file is validated and stored, "
        "and the response comes back immediately with status `PENDING`; parsing and measuring happen in the background. "
        "Poll `GET /api/files/{id}/` until the status is `COMPLETED` or `FAILED`.\n\n"
        "Obviously bad input is rejected here: wrong extension or content, empty file, over the size limit, a ZIP with no "
        "`.shp`. Problems that need a real parse (for example a Shapefile with no `.prj`) are only discovered in the "
        "background, and show up as status `FAILED` with an explanation in `error`.\n\n"
        "Example: `curl -F \"file=@survey.kml\" http://localhost:8000/api/files/`"
    ),
    response_description="The new file's id and its initial `PENDING` status.",
    responses={
        400: _error(
            "The upload was rejected before processing.",
            invalid_type=(
                "Wrong extension or content",
                "Invalid file type. Upload a .zip (Shapefile) or a .kml file.",
                "INVALID_FILE_TYPE",
            ),
            empty=("Zero-byte file", "The uploaded file is empty.", "EMPTY_FILE"),
        ),
        413: _error(
            "The file is larger than `MAX_UPLOAD_SIZE_MB`.",
            too_large=("Over the size limit", "File too large", "FILE_TOO_LARGE"),
        ),
        422: _error(
            "The file is structurally invalid, or the `file` form field is missing.",
            corrupt=("Not a valid ZIP", "The file is not a valid ZIP archive.", "CORRUPT_FILE"),
            no_shp=("ZIP without a Shapefile", "The ZIP archive does not contain a .shp file.", "CORRUPT_FILE"),
            missing_field=("No `file` field in the form", "file: Field required", "VALIDATION_ERROR"),
        ),
    },
)
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="A `.zip` containing a Shapefile, or a `.kml` file."),
    session: AsyncSession = Depends(get_session),
) -> FileUploadResponse:
    """Store the upload and queue processing; returns immediately with ``PENDING`` status."""
    record = await file_service.create_upload(session, file)
    background_tasks.add_task(file_service.process_file, record.id)
    return FileUploadResponse.model_validate(record)


@router.get(
    "/",
    response_model=FileListPage,
    tags=["files"],
    summary="List uploaded files",
    description=(
        "Returns uploaded files newest first, one page at a time. Each entry has just enough to render a list: "
        "id, filename, status, feature count, CRS and upload time. Use the id with the other endpoints for details. "
        "An empty database returns `total: 0` and an empty `items` list, not an error."
    ),
    response_description="A page of file summaries plus the pagination totals.",
    responses={422: _error("A pagination parameter is out of range.", bad_page=_ERR_VALIDATION)},
)
async def list_files(
    page: int = _PAGE_PARAM,
    page_size: int = Query(
        _settings.list_default_page_size,
        ge=1,
        le=_settings.list_max_page_size,
        description=f"Items per page, 1 to {_settings.list_max_page_size}.",
        examples=[20],
    ),
    session: AsyncSession = Depends(get_session),
) -> FileListPage:
    """Return file summaries for building an uploads list."""
    return await file_service.list_files(session, page, page_size)


@router.get(
    "/{file_id}/",
    response_model=FileInfo,
    tags=["files"],
    summary="Get file metadata and processing status",
    description=(
        "Returns the file's current status together with what is known about it so far. `feature_count` and `crs` "
        "are null until processing completes. If processing failed, `error` says why. If the file completed but part "
        "of it could not be read (for example one folder of a multi-folder KML), `warnings` lists what was skipped. "
        "This is the endpoint to poll after an upload."
    ),
    response_description="File metadata: status, feature count, original CRS, error and warnings.",
    responses={
        404: _error(
            "No file with this id.",
            not_found=("Unknown id", f"File '{_EXAMPLE_ID}' not found.", "FILE_NOT_FOUND"),
        ),
        422: _error("The id is not a valid UUID.", bad_id=_ERR_BAD_ID),
    },
)
async def get_file_info(
    file_id: uuid.UUID = _FILE_ID_PARAM, session: AsyncSession = Depends(get_session)
) -> FileInfo:
    """Return metadata for an uploaded file."""
    return await file_service.get_file_info(session, file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsPage,
    tags=["files"],
    summary="List per-feature measurements",
    description=(
        "Returns each feature of a `COMPLETED` file: its geometry as WKT in the original CRS, the same geometry as "
        "WGS84 GeoJSON for maps, its attributes, and its measurement. Polygons get an area in m², lines a length in m, "
        "both computed after reprojecting to the UTM zone of the feature's centroid. Points and unsupported geometry "
        "types return a measurement whose fields are all null. Features are ordered by `feature_id`.\n\n"
        "Calling this before processing finishes returns 409; for a failed file it returns 422 with the failure reason."
    ),
    response_description="One page of features with geometry, properties and measurement, plus pagination totals.",
    responses={
        404: _error(
            "No file with this id.",
            not_found=("Unknown id", f"File '{_EXAMPLE_ID}' not found.", "FILE_NOT_FOUND"),
        ),
        409: _error(
            "The file is still being processed.",
            not_ready=("Still processing", "File is still PROCESSING. Try again shortly.", "FILE_NOT_READY"),
        ),
        422: _error(
            "Processing failed for this file, or a parameter is invalid.",
            failed=(
                "File failed to process",
                "'parcels.shp' does not declare a coordinate reference system (missing .prj). Include the .prj file in the ZIP.",
                "FILE_PROCESSING_FAILED",
            ),
            bad_page=_ERR_VALIDATION,
        ),
    },
)
async def get_measurements(
    file_id: uuid.UUID = _FILE_ID_PARAM,
    page: int = _PAGE_PARAM,
    page_size: int = Query(
        _settings.default_page_size,
        ge=1,
        le=_settings.max_page_size,
        description=f"Items per page, 1 to {_settings.max_page_size}.",
        examples=[50],
    ),
    session: AsyncSession = Depends(get_session),
) -> MeasurementsPage:
    """Return geometry, properties and measurement for each feature."""
    return await file_service.list_measurements(session, file_id, page, page_size)

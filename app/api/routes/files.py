"""HTTP routes for file upload, metadata and measurements. Routes delegate to services."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session
from app.schemas.schemas import ErrorResponse, FileInfo, FileListPage, FileUploadResponse, MeasurementsPage
from app.services import file_service

_settings = get_settings()

router = APIRouter(prefix="/api/files", tags=["files"])

_errors = {
    400: {"model": ErrorResponse, "description": "Invalid or empty file"},
    404: {"model": ErrorResponse, "description": "File not found"},
    409: {"model": ErrorResponse, "description": "File not processed yet"},
    413: {"model": ErrorResponse, "description": "File too large"},
    422: {"model": ErrorResponse, "description": "Validation error or unparseable file"},
}


@router.post(
    "/",
    response_model=FileUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={k: _errors[k] for k in (400, 413, 422)},
    summary="Upload a Shapefile (.zip) or KML file",
)
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="A .zip containing a Shapefile, or a .kml file"),
    session: AsyncSession = Depends(get_session),
) -> FileUploadResponse:
    """Store the upload and queue processing; returns immediately with ``PENDING`` status."""
    record = await file_service.create_upload(session, file)
    background_tasks.add_task(file_service.process_file, record.id)
    return FileUploadResponse.model_validate(record)


@router.get(
    "/",
    response_model=FileListPage,
    responses={422: _errors[422]},
    summary="List uploaded files (newest first, paginated)",
)
async def list_files(
    page: int = Query(1, ge=1, description="1-based page number"),
    page_size: int = Query(_settings.list_default_page_size, ge=1, le=_settings.list_max_page_size),
    session: AsyncSession = Depends(get_session),
) -> FileListPage:
    """Return file summaries for building an uploads list."""
    return await file_service.list_files(session, page, page_size)


@router.get(
    "/{file_id}/",
    response_model=FileInfo,
    responses={k: _errors[k] for k in (404, 422)},
    summary="Get file metadata and processing status",
)
async def get_file_info(file_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> FileInfo:
    """Return metadata for an uploaded file."""
    return await file_service.get_file_info(session, file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsPage,
    responses={k: _errors[k] for k in (404, 409, 422)},
    summary="List per-feature measurements (paginated)",
)
async def get_measurements(
    file_id: uuid.UUID,
    page: int = Query(1, ge=1, description="1-based page number"),
    page_size: int = Query(_settings.default_page_size, ge=1, le=_settings.max_page_size),
    session: AsyncSession = Depends(get_session),
) -> MeasurementsPage:
    """Return geometry, properties and measurement for each feature."""
    return await file_service.list_measurements(session, file_id, page, page_size)

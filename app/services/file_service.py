"""Orchestration: store uploads, run the parse → measure → persist pipeline, serve queries.

Routes call into this module only; it is the single place that touches both the
database and the geospatial services.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from pyproj import CRS
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AppError,
    EmptyFileError,
    FileNotReadyError,
    FileProcessingFailedError,
    FileTooLargeError,
    InvalidFileTypeError,
    UploadedFileNotFoundError,
)
from app.db.session import async_session_factory
from app.models.models import Feature, FileStatus, UploadedFile
from app.schemas.schemas import FeatureMeasurement, FileInfo, FileListPage, FileSummary, Measurement, MeasurementsPage
from app.services.crs_handler import crs_label, geojson_many, to_wgs84_many
from app.services.file_parser import ParsedFeature, ParsedFile, parse_geospatial_file, quick_validate
from app.services.measurement import measure_geometries

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = frozenset({".zip", ".kml"})
_UPLOAD_CHUNK = 1024 * 1024
_INSERT_BATCH = 1000
_GENERIC_FAILURE = "Processing failed due to an unexpected internal error."
_INTERRUPTED_REASON = "Service restarted before processing completed"


# --------------------------------------------------------------------------- upload


def _validate_filename(filename: str | None) -> tuple[str, str]:
    """Validate the client-supplied filename and return ``(safe_name, extension)``.

    Raises:
        InvalidFileTypeError: Missing name or unsupported extension.
    """
    safe_name = Path((filename or "").replace("\\", "/")).name.strip()
    extension = Path(safe_name).suffix.lower()
    if not safe_name or extension not in ALLOWED_EXTENSIONS:
        raise InvalidFileTypeError("Invalid file type. Upload a .zip (Shapefile) or a .kml file.")
    return safe_name[:255], extension


def _check_signature(head: bytes, extension: str) -> None:
    """Cheap content sniffing so a renamed binary is rejected before it hits a parser."""
    if extension == ".zip" and not head.startswith(b"PK"):
        raise InvalidFileTypeError("The file has a .zip extension but is not a ZIP archive.")
    if extension == ".kml" and not head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        raise InvalidFileTypeError("The file has a .kml extension but does not look like XML.")


async def _stream_to_disk(upload: UploadFile, destination: Path, extension: str, max_bytes: int) -> int:
    """Stream an upload to ``destination`` enforcing the size limit; return bytes written."""
    size = 0
    first = True
    with destination.open("wb") as out:
        while chunk := await upload.read(_UPLOAD_CHUNK):
            if first:
                _check_signature(chunk[:64], extension)
                first = False
            size += len(chunk)
            if size > max_bytes:
                raise FileTooLargeError(f"File exceeds the maximum upload size of {max_bytes // (1024 * 1024)} MB.")
            await asyncio.to_thread(out.write, chunk)
    if size == 0:
        raise EmptyFileError("The uploaded file is empty.")
    return size


async def create_upload(session: AsyncSession, upload: UploadFile, settings: Settings | None = None) -> UploadedFile:
    """Validate and persist an upload, creating its ``PENDING`` database record.

    The stored filename is a server-generated UUID; the client name is kept only as metadata.

    Raises:
        InvalidFileTypeError, EmptyFileError, FileTooLargeError, CorruptFileError: Validation failures (nothing is kept on disk).
    """
    settings = settings or get_settings()
    safe_name, extension = _validate_filename(upload.filename)

    file_id = uuid.uuid4()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = settings.upload_dir / f"{file_id}{extension}"

    try:
        size = await _stream_to_disk(upload, stored_path, extension, settings.max_upload_bytes)
        await asyncio.to_thread(quick_validate, stored_path, extension)
    except BaseException:
        stored_path.unlink(missing_ok=True)
        raise

    record = UploadedFile(
        id=file_id, filename=safe_name, stored_path=str(stored_path), file_size=size, status=FileStatus.PENDING
    )
    session.add(record)
    try:
        await session.commit()
    except BaseException:
        stored_path.unlink(missing_ok=True)
        raise
    logger.info("File uploaded", extra={"file_id": str(file_id), "upload_name": safe_name, "size_bytes": size})
    return record


# ------------------------------------------------------------------------ processing


def _build_feature_rows(file_id: uuid.UUID, parsed: ParsedFile) -> list[dict[str, Any]]:
    """Measure every parsed feature and shape it into DB rows (CPU-bound; run off the event loop).

    Features are batched by source CRS (normally just one) so that reprojection and measurement
    use the bulk, per-UTM-zone functions instead of transforming feature by feature.
    """
    # The parser gives every feature from one source the same CRS object, so identity grouping is
    # safe, and it avoids hashing a CRS (which serialises it to WKT) once per feature.
    batches: dict[int, tuple[CRS, list[ParsedFeature]]] = {}
    for feat in parsed.features:
        batches.setdefault(id(feat.crs), (feat.crs, []))[1].append(feat)

    rows: list[dict[str, Any]] = []
    for crs, feats in batches.values():
        geometries = [f.geometry for f in feats]
        wgs84 = to_wgs84_many(geometries, crs)
        measurements = measure_geometries(geometries, crs, [f.index for f in feats], wgs84_geometries=wgs84)
        geojson = geojson_many(wgs84)
        label = crs_label(crs)
        for feat, result, doc in zip(feats, measurements, geojson):
            rows.append(
                {
                    "file_id": file_id,
                    "feature_index": feat.index,
                    "geometry_type": feat.geometry_type,
                    "geometry_wkt": feat.wkt,
                    "geometry_geojson": doc,
                    "crs": label,
                    "properties": feat.properties,
                    "measurement_type": result.type,
                    "measurement_value": result.value,
                    "measurement_unit": result.unit,
                    "measurement_crs": result.projected_crs,
                }
            )
    rows.sort(key=lambda row: row["feature_index"])
    return rows


def _parse_and_measure(file_id: uuid.UUID, path: Path, settings: Settings) -> tuple[ParsedFile, list[dict[str, Any]]]:
    parsed = parse_geospatial_file(path, path.suffix.lower(), settings.max_extracted_bytes, settings.max_zip_members)
    return parsed, _build_feature_rows(file_id, parsed)


async def _mark_failed(session_factory: async_sessionmaker[AsyncSession], file_id: uuid.UUID, reason: str) -> None:
    """Record a failure using a fresh session, so a poisoned transaction can't block it."""
    async with session_factory() as session:
        await session.execute(
            update(UploadedFile).where(UploadedFile.id == file_id).values(status=FileStatus.FAILED, error_message=reason)
        )
        await session.commit()


async def process_file(
    file_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
) -> None:
    """Background job: parse the stored file, measure features and persist the results.

    Never raises: any failure is logged and recorded as ``FAILED`` on the file record.
    Features and the ``COMPLETED`` status are written in one transaction, so a file
    is never observed half-processed.

    Args:
        file_id: Primary key of the :class:`UploadedFile` to process.
        session_factory: Overridable for tests; defaults to the application factory.
        settings: Overridable for tests.
    """
    session_factory = session_factory or async_session_factory
    settings = settings or get_settings()
    log_ctx = {"file_id": str(file_id)}

    try:
        async with session_factory() as session:
            record = await session.get(UploadedFile, file_id)
            if record is None:
                logger.error("Cannot process unknown file", extra=log_ctx)
                return
            stored_path = Path(record.stored_path)
            record.status = FileStatus.PROCESSING
            await session.commit()
            logger.info("Processing started", extra=log_ctx)

            parsed, rows = await asyncio.to_thread(_parse_and_measure, file_id, stored_path, settings)

            for start in range(0, len(rows), _INSERT_BATCH):
                await session.execute(insert(Feature), rows[start : start + _INSERT_BATCH])
            record.status = FileStatus.COMPLETED
            record.feature_count = len(rows)
            record.crs = parsed.crs_label
            record.warnings = parsed.warnings
            record.error_message = None
            await session.commit()
        logger.info(
            "Processing completed",
            extra={**log_ctx, "feature_count": len(rows), "warning_count": len(parsed.warnings)},
        )
    except AppError as exc:
        logger.warning("Processing failed", extra={**log_ctx, "reason": exc.detail, "code": exc.code})
        await _safe_mark_failed(session_factory, file_id, exc.detail)
    except Exception:  # noqa: BLE001 - a background task must never propagate
        logger.exception("Processing crashed", extra=log_ctx)
        await _safe_mark_failed(session_factory, file_id, _GENERIC_FAILURE)


async def _safe_mark_failed(session_factory: async_sessionmaker[AsyncSession], file_id: uuid.UUID, reason: str) -> None:
    try:
        await _mark_failed(session_factory, file_id, reason)
    except Exception:  # noqa: BLE001
        logger.exception("Could not record failure status", extra={"file_id": str(file_id)})


async def fail_interrupted_jobs(session: AsyncSession) -> int:
    """Mark every ``PENDING``/``PROCESSING`` job as ``FAILED``; call once at startup.

    Jobs run as in-process background tasks, so a job still in flight when the service
    starts can only be an orphan of the previous process. This assumes a single running
    instance; with several instances, one starting would fail another's live jobs.

    Returns:
        Number of records updated.
    """
    result = await session.execute(
        update(UploadedFile)
        .where(UploadedFile.status.in_([FileStatus.PENDING, FileStatus.PROCESSING]))
        .values(status=FileStatus.FAILED, error_message=_INTERRUPTED_REASON)
    )
    await session.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------------------- queries


async def get_file(session: AsyncSession, file_id: uuid.UUID) -> UploadedFile:
    """Fetch a file record.

    Raises:
        UploadedFileNotFoundError: No such file.
    """
    record = await session.get(UploadedFile, file_id)
    if record is None:
        raise UploadedFileNotFoundError(f"File '{file_id}' not found.")
    return record


async def get_file_info(session: AsyncSession, file_id: uuid.UUID) -> FileInfo:
    """Return file metadata as a response schema.

    Raises:
        UploadedFileNotFoundError: No such file.
    """
    return FileInfo.model_validate(await get_file(session, file_id))


async def list_files(session: AsyncSession, page: int, page_size: int) -> FileListPage:
    """Return one page of uploaded files, newest first."""
    total = await session.scalar(select(func.count()).select_from(UploadedFile)) or 0
    result = await session.execute(
        select(UploadedFile)
        .order_by(UploadedFile.created_at.desc(), UploadedFile.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return FileListPage(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=math.ceil(total / page_size) if total else 0,
        items=[FileSummary.model_validate(r) for r in result.scalars()],
    )


async def list_measurements(session: AsyncSession, file_id: uuid.UUID, page: int, page_size: int) -> MeasurementsPage:
    """Return one page of feature measurements for a completed file.

    Raises:
        UploadedFileNotFoundError: No such file.
        FileNotReadyError: File is still ``PENDING``/``PROCESSING``.
        FileProcessingFailedError: File processing failed (message explains why).
    """
    record = await get_file(session, file_id)
    if record.status in (FileStatus.PENDING, FileStatus.PROCESSING):
        raise FileNotReadyError(f"File is still {record.status.value}. Try again shortly.")
    if record.status == FileStatus.FAILED:
        raise FileProcessingFailedError(record.error_message or "File processing failed.")

    total = await session.scalar(select(func.count()).select_from(Feature).where(Feature.file_id == file_id)) or 0
    result = await session.execute(
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.feature_index)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        FeatureMeasurement(
            feature_id=f.feature_index,
            geometry_type=f.geometry_type,
            geometry=f.geometry_wkt,
            geometry_geojson=f.geometry_geojson,
            crs=f.crs,
            properties=f.properties or {},
            measurement=Measurement(
                type=f.measurement_type,
                value=f.measurement_value,
                unit=f.measurement_unit,
                projected_crs=f.measurement_crs,
            ),
        )
        for f in result.scalars()
    ]
    return MeasurementsPage(
        file_id=file_id,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=math.ceil(total / page_size) if total else 0,
        items=items,
    )

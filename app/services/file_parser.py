"""Parse Shapefile (ZIP) and KML uploads into plain feature records.

Everything here is synchronous and CPU/IO bound; callers run it in a worker thread.
"""

from __future__ import annotations

import logging
import math
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import fiona
import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.core.exceptions import CorruptFileError
from app.services.crs_handler import WGS84, crs_label

logger = logging.getLogger(__name__)

# KML is read-only in some GDAL builds' driver table; make sure Fiona will open it.
fiona.drvsupport.supported_drivers["KML"] = "rw"

_COPY_CHUNK = 1024 * 1024


class MissingCRSError(CorruptFileError):
    """The file does not declare a CRS, so measurements would be meaningless."""

    code = "MISSING_CRS"


@dataclass(slots=True)
class ParsedFeature:
    """A single feature as read from the file (geometry still in the original CRS)."""

    index: int
    geometry: BaseGeometry | None
    geometry_type: str
    wkt: str | None
    crs: CRS
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedFile:
    """All features of an upload plus the file-level CRS label."""

    crs_label: str
    features: list[ParsedFeature]
    warnings: list[str] = field(default_factory=list)


def parse_geospatial_file(path: Path, extension: str, max_extracted_bytes: int, max_zip_members: int) -> ParsedFile:
    """Parse an uploaded ``.zip`` (Shapefile) or ``.kml`` into features.

    Args:
        path: Location of the stored upload.
        extension: Lower-case extension including the dot (``.zip`` or ``.kml``).
        max_extracted_bytes: Total uncompressed size cap for ZIP archives.
        max_zip_members: Maximum number of entries allowed in a ZIP archive.

    Returns:
        The parsed features, CRS label and any non-fatal warnings (e.g. skipped KML layers).

    Raises:
        CorruptFileError: The file is unreadable, empty, or structurally invalid.
        MissingCRSError: A Shapefile has no ``.prj`` / CRS.
    """
    if extension == ".zip":
        with tempfile.TemporaryDirectory(prefix="shp_") as tmp:
            root = Path(tmp)
            _safe_extract_zip(path, root, max_extracted_bytes, max_zip_members)
            shapefiles = sorted(p for p in root.rglob("*") if p.suffix.lower() == ".shp" and p.is_file())
            if not shapefiles:
                raise CorruptFileError("The ZIP archive does not contain a .shp file.")
            sources = [(shp, None) for shp in shapefiles]
            features, warnings = _read_sources(sources, driver=None, default_crs=None)
    elif extension == ".kml":
        layers = _list_kml_layers(path)
        features, warnings = _read_sources([(path, layer) for layer in layers], driver="KML", default_crs=WGS84)
    else:  # pragma: no cover - validated upstream
        raise CorruptFileError(f"Unsupported file extension: {extension}")

    if not features:
        detail = " " + " ".join(warnings) if warnings else ""
        raise CorruptFileError(f"The file contains no readable features.{detail}")

    unique_crs = {id(f.crs): f.crs for f in features}.values()  # one CRS object per source; avoids per-feature hashing
    labels = list(dict.fromkeys(crs_label(crs) for crs in unique_crs))
    return ParsedFile(crs_label=", ".join(labels), features=features, warnings=warnings)


def quick_validate(path: Path, extension: str) -> None:
    """Cheap structural check run at upload time so obviously corrupt files fail fast.

    Only inspects the ZIP central directory / the first bytes of a KML; the real
    parse happens in the background job.

    Raises:
        CorruptFileError: ZIP is invalid or has no ``.shp``; KML has no ``<kml`` root marker.
    """
    if extension == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                names = [n.lower() for n in archive.namelist()]
        except (zipfile.BadZipFile, OSError) as exc:
            raise CorruptFileError("The file is not a valid ZIP archive.") from exc
        if not any(n.endswith(".shp") for n in names):
            raise CorruptFileError("The ZIP archive does not contain a .shp file.")
    elif extension == ".kml":
        with path.open("rb") as fh:
            head = fh.read(8192).lower()
        if b"<kml" not in head:
            raise CorruptFileError("The file is not valid KML (no <kml> root element found).")


def _list_kml_layers(path: Path) -> list[str | None]:
    """KML folders/documents appear as layers; read all of them."""
    try:
        layers = fiona.listlayers(str(path))
    except Exception as exc:  # noqa: BLE001 - Fiona raises assorted driver errors
        logger.warning("Could not list KML layers", extra={"error": str(exc)})
        raise CorruptFileError("The KML file could not be parsed. It may be corrupt or not valid KML.") from exc
    return list(layers) or [None]


def _read_sources(
    sources: list[tuple[Path, str | None]], driver: str | None, default_crs: CRS | None
) -> tuple[list[ParsedFeature], list[str]]:
    """Read every (path, layer) source and number features consecutively across them.

    A KML layer that cannot be read is skipped and reported in the returned warnings
    (the file is still processed); an unreadable Shapefile fails the whole file.
    """
    features: list[ParsedFeature] = []
    warnings: list[str] = []
    for src_path, layer in sources:
        try:
            kwargs: dict[str, Any] = {"engine": "fiona"}
            if driver:
                kwargs["driver"] = driver
            if layer is not None:
                kwargs["layer"] = layer
            gdf = gpd.read_file(src_path, **kwargs)
        except Exception as exc:  # noqa: BLE001 - Fiona/pyogrio/GDAL raise many unrelated types
            logger.warning(
                "Failed to read geospatial source",
                extra={"source": src_path.name, "layer": layer, "error": f"{type(exc).__name__}: {exc}"},
            )
            if driver == "KML" and layer is not None:
                warnings.append(f"KML layer '{layer}' could not be read and was skipped; its features are missing.")
                continue  # one broken folder should not sink a multi-layer KML
            raise CorruptFileError(
                f"Could not read '{src_path.name}'. The file may be corrupt or incomplete "
                "(a Shapefile ZIP needs at least .shp, .shx and .dbf)."
            ) from exc

        crs = CRS.from_user_input(gdf.crs) if gdf.crs is not None else default_crs
        if crs is None:
            raise MissingCRSError(
                f"'{src_path.name}' does not declare a coordinate reference system (missing .prj). "
                "Include the .prj file in the ZIP."
            )
        features.extend(_to_features(gdf, crs, start_index=len(features)))
    return features, warnings


def _to_features(gdf: gpd.GeoDataFrame, crs: CRS, start_index: int) -> list[ParsedFeature]:
    """Convert a GeoDataFrame into :class:`ParsedFeature` records."""
    geom_col = gdf.geometry.name
    records = gdf.drop(columns=[geom_col]).to_dict("records")
    out: list[ParsedFeature] = []
    for offset, (geom, props) in enumerate(zip(gdf.geometry, records)):
        if geom is None or geom.is_empty:
            geom_type, wkt = ("None" if geom is None else geom.geom_type), None
        else:
            geom_type, wkt = geom.geom_type, geom.wkt
        out.append(
            ParsedFeature(
                index=start_index + offset,
                geometry=geom if wkt is not None else None,
                geometry_type=geom_type,
                wkt=wkt,
                crs=crs,
                properties={str(k): to_jsonable(v) for k, v in props.items()},
            )
        )
    return out


def to_jsonable(value: Any) -> Any:
    """Coerce pandas/numpy/datetime values into JSON-safe Python values (NaN/NaT → ``None``)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (np.generic,)):
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return None if value is pd.NaT else value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _safe_extract_zip(zip_path: Path, dest: Path, max_total_bytes: int, max_members: int) -> None:
    """Extract a ZIP defensively (zip-slip, zip-bomb, encrypted entries).

    Raises:
        CorruptFileError: The archive is invalid or violates a safety limit.
    """
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise CorruptFileError("The file is not a valid ZIP archive.") from exc

    with archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) > max_members:
            raise CorruptFileError(f"The ZIP archive contains too many files (limit {max_members}).")
        if sum(m.file_size for m in members) > max_total_bytes:
            raise CorruptFileError("The ZIP archive is too large when uncompressed.")

        written = 0
        dest_resolved = dest.resolve()
        for member in members:
            parts = PurePosixPath(member.filename.replace("\\", "/")).parts
            if parts and (parts[0] == "__MACOSX" or parts[-1].startswith(".")):
                continue  # macOS resource forks / hidden files
            if member.flag_bits & 0x1:
                raise CorruptFileError("Password-protected ZIP archives are not supported.")
            target = (dest / PurePosixPath(*parts)).resolve() if parts else None
            if (
                target is None
                or PurePosixPath(member.filename).is_absolute()
                or ".." in parts
                or dest_resolved not in target.parents
            ):
                raise CorruptFileError("The ZIP archive contains an unsafe file path.")

            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(member) as src, open(target, "wb") as out:
                    while chunk := src.read(_COPY_CHUNK):
                        written += len(chunk)
                        if written > max_total_bytes:  # header sizes can lie
                            raise CorruptFileError("The ZIP archive is too large when uncompressed.")
                        out.write(chunk)
            except (zipfile.BadZipFile, RuntimeError, EOFError, OSError) as exc:
                raise CorruptFileError("The ZIP archive is corrupt and could not be extracted.") from exc

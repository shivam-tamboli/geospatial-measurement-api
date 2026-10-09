"""Parse Shapefile (ZIP) and KML uploads into plain feature records.

Everything here is synchronous and CPU/IO bound; callers run it in a worker thread.
"""

from __future__ import annotations

import logging
import math
import tempfile
import xml.etree.ElementTree as ET
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
from shapely.errors import ShapelyError
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from app.core.exceptions import CorruptFileError
from app.services.crs_handler import WGS84, crs_label

logger = logging.getLogger(__name__)

# KML is read-only in some GDAL builds' driver table; make sure Fiona will open it.
fiona.drvsupport.supported_drivers["KML"] = "rw"

_COPY_CHUNK = 1024 * 1024
_MAX_FEATURE_WARNINGS = 10  # per-feature geometry warnings kept verbatim; the rest are summarised in one line


class _UnopenableLayerName(Exception):
    """A KML layer listed by Fiona cannot be opened by that name (GDAL >= 3.10); read the file by position."""


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
        sources = [(path, layer) for layer in _list_kml_layers(path)]
        try:
            features, warnings = _read_sources(sources, driver="KML", default_crs=WGS84)
        except _UnopenableLayerName:
            # All-or-nothing: mixing name-based and position-based reads can return one layer twice and
            # skip another, because on these GDAL versions the listed order is not Fiona's internal order.
            logger.info("KML layer names not openable; reading every layer by position")
            features, warnings = _read_sources(sources, driver="KML", default_crs=WGS84, by_position=True)
    else:  # pragma: no cover - validated upstream
        raise CorruptFileError(f"Unsupported file extension: {extension}")

    if not features:
        detail = " " + " ".join(warnings) if warnings else ""
        raise CorruptFileError(f"The file contains no readable features.{detail}")

    if extension == ".kml":
        unusable, nulled = _null_unparseable_points(features, path)
        if unusable:
            warnings.append(_unparseable_points_warning(unusable, nulled))

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
    sources: list[tuple[Path, str | None]], driver: str | None, default_crs: CRS | None, by_position: bool = False
) -> tuple[list[ParsedFeature], list[str]]:
    """Read every (path, layer) source and number features consecutively across them.

    A KML layer that cannot be read is skipped and reported in the returned warnings
    (the file is still processed); an unreadable Shapefile fails the whole file.

    Raises:
        _UnopenableLayerName: A KML layer cannot be opened by its listed name and ``by_position`` is off.
    """
    features: list[ParsedFeature] = []
    warnings: list[str] = []
    unbuildable: list[str] = []
    for index, (src_path, layer) in enumerate(sources):
        try:
            gdf = _load_layer(src_path, layer, index, driver, by_position)
        except _UnopenableLayerName:
            raise
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
        layer_features = _to_features(gdf, crs, start_index=len(features))
        for offset, (geometry_type, reason) in gdf.attrs.get("geometry_problems", {}).items():
            layer_features[offset].geometry_type = geometry_type  # keep the real type, not "None"
            unbuildable.append(f"Feature {layer_features[offset].index}: {reason} — skipped (geometry set to null)")
        features.extend(layer_features)
    return features, warnings + _summarise_warnings(unbuildable)


def _summarise_warnings(messages: list[str]) -> list[str]:
    """Keep the first few per-feature warnings; summarise the rest so a bad file can't produce thousands."""
    if len(messages) <= _MAX_FEATURE_WARNINGS:
        return messages
    extra = len(messages) - _MAX_FEATURE_WARNINGS
    return messages[:_MAX_FEATURE_WARNINGS] + [f"...and {extra} more features had geometries that could not be built."]


def _load_layer(path: Path, layer: str | None, index: int, driver: str | None, by_position: bool) -> gpd.GeoDataFrame:
    """Read a layer; if a geometry cannot be built, re-read it feature by feature instead of failing it.

    GeoPandas builds every Shapely geometry while reading, and Shapely cannot represent some things
    GDAL happily reports, most commonly a LineString with a single point (``GEOSException: point
    array must contain 0 or >1 elements``). One such feature then raises out of ``read_file`` and the
    whole layer is lost. Only errors of that kind trigger the slower per-feature path, so a normal
    file takes the normal route and a genuine driver failure still fails as before.
    """
    try:
        return _read_layer(path, layer, index, driver, by_position)
    except (ValueError, ShapelyError) as exc:
        if isinstance(exc, _UnopenableLayerName):
            raise
        logger.info(
            "Layer read failed while building geometries; retrying feature by feature",
            extra={"source": path.name, "layer": layer, "error": f"{type(exc).__name__}: {exc}"},
        )
        return _read_layer(path, layer, index, driver, by_position, tolerant=True)


def _read_layer(
    path: Path, layer: str | None, index: int, driver: str | None, by_position: bool, tolerant: bool = False
) -> gpd.GeoDataFrame:
    """Read one layer with Fiona.

    A KML whose placemarks sit directly under ``<Document>`` (no ``<Folder>``), or whose folders have
    no ``<name>``, gets auto-generated layer names. With GDAL >= 3.10 those names, as returned by
    ``fiona.listlayers``, are rejected by ``fiona.open`` with ``ValueError: Null layer``, so every
    such KML failed. (GDAL 3.9 accepted them, which is why it only showed up on the arm64 Docker image.)

    Reading by position works on every GDAL version, but Fiona cannot open layer ``0`` by index (it is
    treated as "no layer given"), so position 0 is read without a layer argument, which Fiona resolves
    to its first layer. The listed order is not always Fiona's internal order, so positions must be
    used for *every* layer of a file or for none: see :func:`parse_geospatial_file`.

    Args:
        path: File to read.
        layer: Layer name as listed by Fiona, or ``None`` to read the default layer.
        index: Position of ``layer`` among the file's layers.
        driver: Fiona driver name (``"KML"``) or ``None`` to auto-detect (Shapefile).
        by_position: Read by position instead of by name.
        tolerant: Build geometries one at a time and null the ones that cannot be built.

    Raises:
        _UnopenableLayerName: The name cannot be opened (KML only, and only when ``by_position`` is off).
    """
    base: dict[str, Any] = {"driver": driver} if driver else {}

    def read(**extra: Any) -> gpd.GeoDataFrame:
        if tolerant:
            return _read_features_tolerant(path, **base, **extra)
        return gpd.read_file(path, engine="fiona", **base, **extra)

    if layer is None or (by_position and index == 0):
        return read()
    if by_position:
        return read(layer=index)
    try:
        return read(layer=layer)
    except ValueError as exc:
        if driver == "KML" and "Null layer" in str(exc):
            raise _UnopenableLayerName(layer) from exc
        raise


def _read_features_tolerant(path: Path, *, driver: str | None = None, layer: str | int | None = None) -> gpd.GeoDataFrame:
    """Read a layer feature by feature, replacing any geometry Shapely cannot build with ``None``.

    The frame is assembled by the same ``GeoDataFrame.from_features`` call GeoPandas uses, from
    records whose bad geometries have already been removed, so the result looks like a normal read.
    The unbuildable ones are recorded in ``gdf.attrs["geometry_problems"]`` as
    ``{row offset: (geometry type, reason)}``.
    """
    kwargs: dict[str, Any] = {}
    if driver:
        kwargs["driver"] = driver
    if layer is not None:
        kwargs["layer"] = layer
    problems: dict[int, tuple[str, str]] = {}
    with fiona.open(path, **kwargs) as source:
        epsg = source.crs.to_epsg(confidence_threshold=100)  # same CRS resolution as geopandas.read_file
        crs: Any = epsg if epsg is not None else (source.crs_wkt or None)
        columns = list(source.schema["properties"])
        records = []
        for offset, record in enumerate(source):
            feature = dict(record.__geo_interface__)
            geometry = feature.get("geometry")
            if geometry:
                try:
                    shape(geometry)
                except (ValueError, ShapelyError) as exc:
                    problems[offset] = (str(geometry.get("type", "Geometry")), _describe_unbuildable(geometry, exc))
                    feature["geometry"] = None
            records.append(feature)
    gdf = gpd.GeoDataFrame.from_features(records, crs=crs, columns=columns + ["geometry"])
    gdf.attrs["geometry_problems"] = problems
    return gdf


def _describe_unbuildable(geometry: dict[str, Any], exc: Exception) -> str:
    """Human-readable reason a GeoJSON-like geometry could not be turned into a Shapely geometry."""
    kind = str(geometry.get("type", "Geometry"))
    coordinates = geometry.get("coordinates") or []
    if kind == "LineString" and len(coordinates) < 2:
        return "LineString has fewer than 2 points"
    if kind == "MultiLineString" and any(len(part) < 2 for part in coordinates):
        return "MultiLineString has a part with fewer than 2 points"
    return f"{kind} geometry could not be built ({str(exc)[:80]})"


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


# ------------------------------------------------------------------ unparseable KML coordinates


def _local(tag: str) -> str:
    """Tag name without its XML namespace."""
    return tag.rsplit("}", 1)[-1]


def _norm(text: object) -> str:
    """Normalise a name/description for matching: ``None`` -> ``""`` and whitespace collapsed."""
    return " ".join(str(text).split()) if text else ""


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name:
            return child.text or ""
    return ""


def _point_is_unusable(point: ET.Element) -> bool:
    """True if a KML ``<Point>`` has no usable longitude/latitude.

    GDAL turns a Point with empty, missing or non-numeric coordinates into ``POINT (0 0)`` (or, for
    ``77,abc``, into ``POINT (77 0)``) instead of reporting an error. An unusable altitude is
    harmless (the geometry is used as 2D) and does not count.
    """
    coords = next((c for c in point if _local(c.tag) == "coordinates"), None)
    tokens = (coords.text or "").split() if coords is not None else []
    if not tokens:
        return True
    parts = tokens[0].split(",")
    if len(parts) < 2:
        return True
    try:
        return not (math.isfinite(float(parts[0])) and math.isfinite(float(parts[1])))
    except ValueError:
        return True


def _scan_kml_points(path: Path) -> dict[tuple[str, str], list[bool]]:
    """Stream the KML once and record, per ``(name, description)``, whether each ``<Point>`` placemark is unusable.

    Entries are in document order. Only placemarks whose geometry is a plain ``<Point>`` are recorded.
    The file has already been parsed successfully by GDAL, so a parse error here just disables the check.
    """
    flags: dict[tuple[str, str], list[bool]] = {}
    try:
        for _, element in ET.iterparse(path, events=("end",)):
            if _local(element.tag) != "Placemark":
                continue
            point = next((c for c in element if _local(c.tag) == "Point"), None)
            if point is not None:
                key = (_norm(_child_text(element, "name")), _norm(_child_text(element, "description")))
                flags.setdefault(key, []).append(_point_is_unusable(point))
            element.clear()  # keep memory flat on large files
    except ET.ParseError:
        logger.warning("Could not scan KML for unparseable coordinates", exc_info=True)
        return {}
    return flags


def _null_unparseable_points(features: list[ParsedFeature], path: Path) -> tuple[int, int]:
    """Replace the invented geometry of KML Points with unusable coordinates by a null geometry.

    GDAL cannot tell us which points it invented, and a genuine ``0,0`` point looks identical, so
    the raw XML is scanned for Points with unusable coordinates and matched to the parsed features by
    ``(name, description)`` and order. A match is only acted on if the parsed point really has a
    zero coordinate (which is what GDAL produces for bad input), so a good point that merely shares
    a name with a bad one is never nulled.

    Returns:
        ``(unusable, nulled)``: how many Points had unusable coordinates, and how many of them could
        be matched to a feature and were set to a null geometry.
    """
    flags = _scan_kml_points(path)
    unusable = sum(sum(entries) for entries in flags.values())
    if not unusable:
        return 0, 0

    nulled = 0
    for feature in features:
        if feature.geometry_type != "Point" or feature.geometry is None:
            continue
        key = (_norm(feature.properties.get("Name")), _norm(feature.properties.get("Description")))
        entries = flags.get(key)
        if not entries:
            continue
        is_unusable = entries.pop(0)
        geometry = feature.geometry
        if is_unusable and isinstance(geometry, Point) and (geometry.x == 0 or geometry.y == 0):
            feature.geometry, feature.wkt = None, None
            nulled += 1
    if nulled:
        logger.warning("Nulled KML points with unparseable coordinates", extra={"count": nulled, "unusable": unusable})
    return unusable, nulled


def _unparseable_points_warning(unusable: int, nulled: int) -> str:
    """User-facing warning for KML points with empty or unparseable coordinates."""
    noun = "feature" if unusable == 1 else "features"
    if nulled == unusable:
        return f"{unusable} {noun} had empty or unparseable coordinates; the geometry of {'it was' if unusable == 1 else 'each was'} set to null."
    return (
        f"{unusable} {noun} had empty or unparseable coordinates. {nulled} had their geometry set to null; "
        f"{unusable - nulled} could not be matched to a feature and may be placed at (0, 0)."
    )

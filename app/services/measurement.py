"""Area / length calculation, always performed in a projected CRS."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import shapely
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.services.crs_handler import (
    crs_label,
    group_by_utm_epsg,
    project_for_measurement,
    reproject_many,
    to_wgs84_many,
)

logger = logging.getLogger(__name__)

AREA_UNIT = "m²"
LENGTH_UNIT = "m"

_AREA_TYPES = frozenset({"Polygon", "MultiPolygon"})
_LENGTH_TYPES = frozenset({"LineString", "MultiLineString"})
_NO_MEASUREMENT_TYPES = frozenset({"Point", "MultiPoint"})


@dataclass(frozen=True, slots=True)
class MeasurementResult:
    """Outcome of measuring one geometry. All fields are ``None`` when nothing applies."""

    type: str | None = None  # "area" | "length"
    value: float | None = None
    unit: str | None = None
    projected_crs: str | None = None


NO_MEASUREMENT = MeasurementResult()


def _has_degenerate_line(geometry: BaseGeometry) -> bool:
    """True if the geometry is, or contains, a LineString with fewer than 2 points.

    Shapely itself refuses to build such a line, so with Shapely objects this is never true. It is a
    cheap guard for geometry that did not come through Shapely's constructors (a different reader,
    a test double), where measuring would otherwise hand PROJ an unusable line.
    """
    parts = getattr(geometry, "geoms", None) or [geometry]
    return any(part.geom_type == "LineString" and len(part.coords) < 2 for part in parts)


def measure_geometry(geometry: BaseGeometry | None, source_crs: CRS, feature_index: int | None = None) -> MeasurementResult:
    """Measure a geometry in metres / square metres.

    * Polygon / MultiPolygon → area (m²)
    * LineString / MultiLineString → length (m)
    * Point / MultiPoint → no measurement
    * Anything else (GeometryCollection, LinearRing, empty, missing) → logged, no measurement

    This function never raises: failures are logged and yield :data:`NO_MEASUREMENT`
    so a single bad feature cannot fail a whole file.

    Args:
        geometry: The feature geometry in ``source_crs`` (may be ``None``).
        source_crs: CRS of ``geometry``.
        feature_index: Only used to give log lines context.
    """
    log_ctx = {"feature_index": feature_index}

    if geometry is None or geometry.is_empty:
        logger.warning("Feature has no geometry; skipping measurement", extra=log_ctx)
        return NO_MEASUREMENT

    geom_type = geometry.geom_type
    if geom_type in _NO_MEASUREMENT_TYPES:
        return NO_MEASUREMENT
    if geom_type not in _AREA_TYPES | _LENGTH_TYPES:
        logger.warning("Unsupported geometry type for measurement", extra={**log_ctx, "geometry_type": geom_type})
        return NO_MEASUREMENT
    if _has_degenerate_line(geometry):
        logger.warning("LineString has fewer than 2 points; skipping measurement", extra=log_ctx)
        return NO_MEASUREMENT

    try:
        if geom_type in _AREA_TYPES and not geometry.is_valid:
            logger.warning("Measuring an invalid polygon; area may be inaccurate", extra=log_ctx)
        projected, projected_crs = project_for_measurement(geometry, source_crs)
        if geom_type in _AREA_TYPES:
            return MeasurementResult("area", float(projected.area), AREA_UNIT, crs_label(projected_crs))
        return MeasurementResult("length", float(projected.length), LENGTH_UNIT, crs_label(projected_crs))
    except Exception:  # noqa: BLE001 - deliberate: isolate bad features (pyproj/shapely raise many types)
        logger.exception("Measurement failed", extra={**log_ctx, "geometry_type": geom_type})
        return NO_MEASUREMENT


def measure_geometries(
    geometries: Sequence[BaseGeometry | None],
    source_crs: CRS,
    feature_indexes: Sequence[int],
    wgs84_geometries: Sequence[BaseGeometry | None] | None = None,
) -> list[MeasurementResult]:
    """Measure many geometries that share one source CRS, with one PROJ call per UTM zone.

    Equivalent to calling :func:`measure_geometry` on each geometry (and tested to match it),
    but without two Python-level transforms per feature:

    1. every geometry is reprojected to WGS84 in one call (skipped if ``wgs84_geometries`` is given),
    2. centroids and UTM zones are computed with numpy,
    3. features are **grouped by target UTM CRS** and each group is reprojected from the source CRS
       with a single ``GeoSeries.to_crs`` call, then measured with vectorized ``area``/``length``.

    Like the scalar version this never raises: unsupported or unmeasurable features yield
    :data:`NO_MEASUREMENT`, and a failing group falls back to per-feature measurement so
    its neighbours are unaffected.

    Args:
        geometries: Feature geometries in ``source_crs`` (``None`` allowed).
        source_crs: Shared CRS of all ``geometries``.
        feature_indexes: Feature index of each geometry, used only for log context.
        wgs84_geometries: Optional precomputed WGS84 versions (same order) to avoid re-projecting.

    Returns:
        One :class:`MeasurementResult` per input geometry, in input order.
    """
    results = [NO_MEASUREMENT] * len(geometries)

    measurable: dict[int, BaseGeometry] = {}  # position -> polygon / line geometry
    for pos, geom in enumerate(geometries):
        if geom is None or geom.is_empty:
            logger.warning("Feature has no geometry; skipping measurement", extra={"feature_index": feature_indexes[pos]})
        elif geom.geom_type in _AREA_TYPES | _LENGTH_TYPES:
            if _has_degenerate_line(geom):
                logger.warning("LineString has fewer than 2 points; skipping measurement", extra={"feature_index": feature_indexes[pos]})
                continue
            measurable[pos] = geom
        elif geom.geom_type not in _NO_MEASUREMENT_TYPES:
            logger.warning(
                "Unsupported geometry type for measurement",
                extra={"feature_index": feature_indexes[pos], "geometry_type": geom.geom_type},
            )
    if not measurable:
        return results

    wgs = list(wgs84_geometries) if wgs84_geometries is not None else to_wgs84_many(geometries, source_crs)
    placeable: dict[int, BaseGeometry] = {}  # position -> its WGS84 geometry, used only for the zone lookup
    for pos in measurable:
        wgs_geom = wgs[pos]
        if wgs_geom is None:
            logger.error("Measurement failed: could not reproject to WGS84", extra={"feature_index": feature_indexes[pos]})
        else:
            placeable[pos] = wgs_geom

    ordered = list(placeable)
    for epsg, members in group_by_utm_epsg([placeable[pos] for pos in ordered]).items():
        positions = [ordered[m] for m in members]
        if epsg == 0:
            for pos in positions:
                logger.error(
                    "Measurement failed: centroid is not a valid lon/lat", extra={"feature_index": feature_indexes[pos]}
                )
            continue
        try:
            _measure_zone_group({pos: measurable[pos] for pos in positions}, source_crs, epsg, feature_indexes, results)
        except Exception:  # noqa: BLE001 - isolate a bad group, then retry its features one by one
            logger.warning("Bulk measurement failed for UTM group; retrying per feature", extra={"epsg": epsg}, exc_info=True)
            for pos in positions:
                results[pos] = measure_geometry(measurable[pos], source_crs, feature_indexes[pos])
    return results


def _measure_zone_group(
    group: dict[int, BaseGeometry],
    source_crs: CRS,
    epsg: int,
    feature_indexes: Sequence[int],
    results: list[MeasurementResult],
) -> None:
    """Project one UTM group in a single call and write its measurements into ``results``."""
    label = f"EPSG:{epsg}"  # the target is built with CRS.from_epsg(), so the label is exact by construction
    positions = list(group)
    projected = reproject_many([group[pos] for pos in positions], source_crs, CRS.from_epsg(epsg))
    is_area = np.array([group[pos].geom_type in _AREA_TYPES for pos in positions])
    values = np.where(is_area, shapely.area(projected), shapely.length(projected))
    invalid_polygons = is_area & ~shapely.is_valid(projected)

    for k, pos in enumerate(positions):
        value = float(values[k])
        if not np.isfinite(value):
            logger.error("Measurement failed: non-finite result", extra={"feature_index": feature_indexes[pos]})
            continue
        if invalid_polygons[k]:
            logger.warning("Measuring an invalid polygon; area may be inaccurate", extra={"feature_index": feature_indexes[pos]})
        results[pos] = (
            MeasurementResult("area", value, AREA_UNIT, label)
            if is_area[k]
            else MeasurementResult("length", value, LENGTH_UNIT, label)
        )

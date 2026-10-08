"""CRS utilities: labelling, UTM zone auto-detection and reprojection for measurement."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

import geopandas as gpd
import numpy as np
import numpy.typing as npt
import shapely
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

logger = logging.getLogger(__name__)

WGS84 = CRS.from_epsg(4326)

CUSTOM_CRS_PREFIX = "CUSTOM:"
_CUSTOM_WKT_CHARS = 100
GEOJSON_DECIMALS = 6  # ~0.1 m; keeps payloads small for web maps

# UTM is only defined between 80°S and 84°N; beyond that we use Universal Polar Stereographic.
_UTM_MAX_LAT = 84.0
_UTM_MIN_LAT = -80.0
_UPS_NORTH_EPSG = 32661
_UPS_SOUTH_EPSG = 32761


def crs_label(crs: CRS) -> str:
    """Return a label for a CRS that never reports a guessed EPSG code.

    ``EPSG:<code>`` is returned only when PROJ identifies the CRS with 100 % confidence *and*
    the EPSG definition is equal to the given CRS. Anything else (no match, a fuzzy match,
    or a definition that differs from the registry entry) is labelled
    ``CUSTOM:<first 100 characters of its WKT>``.
    """
    epsg = crs.to_epsg(min_confidence=100)
    if epsg is not None:
        try:
            if CRS.from_epsg(epsg) == crs:
                return f"EPSG:{epsg}"
        except CRSError:
            logger.warning("EPSG code reported by PROJ could not be loaded", extra={"epsg": epsg})
    return f"{CUSTOM_CRS_PREFIX}{crs.to_wkt()[:_CUSTOM_WKT_CHARS]}"


@lru_cache(maxsize=256)
def _transformer(source: CRS, target: CRS) -> Transformer:
    """Build (and cache) a lon/lat-ordered transformer; construction is expensive."""
    return Transformer.from_crs(source, target, always_xy=True)


def utm_epsgs_for_lonlat(lons: npt.ArrayLike, lats: npt.ArrayLike) -> npt.NDArray[np.int64]:
    """Vectorized UTM zone lookup: EPSG codes for arrays of WGS84 positions.

    Uses the standard 6°-wide zones (``zone = floor((lon + 180) / 6) + 1``), the
    ``326xx`` series for the northern hemisphere and ``327xx`` for the southern.
    Latitudes outside the UTM domain map to the UPS polar CRSs.
    Norway/Svalbard zone exceptions are intentionally ignored (distortion is negligible).

    Args:
        lons: Longitudes in degrees (any finite value; wrapped into [-180, 180)).
        lats: Latitudes in degrees.

    Returns:
        EPSG codes, with ``0`` where the position is invalid (non-finite, or ``|lat| > 90``).
    """
    lon = np.asarray(lons, dtype=float)
    lat = np.asarray(lats, dtype=float)
    valid = np.isfinite(lon) & np.isfinite(lat) & (np.abs(lat) <= 90.0)
    wrapped = np.where(valid, (lon + 180.0) % 360.0 - 180.0, 0.0)
    zone = np.minimum(np.floor((wrapped + 180.0) / 6.0).astype(np.int64) + 1, 60)
    epsg = np.where(lat >= 0, 32600, 32700) + zone
    epsg = np.where(lat > _UTM_MAX_LAT, _UPS_NORTH_EPSG, epsg)
    epsg = np.where(lat < _UTM_MIN_LAT, _UPS_SOUTH_EPSG, epsg)
    return np.where(valid, epsg, 0).astype(np.int64)


def utm_epsg_for_lonlat(lon: float, lat: float) -> int:
    """Return the EPSG code of the UTM (or polar UPS) CRS for one WGS84 position.

    Raises:
        ValueError: If the position is not a valid lon/lat.
    """
    code = int(utm_epsgs_for_lonlat([lon], [lat])[0])
    if code == 0:
        raise ValueError(f"Invalid geographic position: ({lon}, {lat})")
    return code


def select_projected_crs(geometry: BaseGeometry, source_crs: CRS) -> CRS:
    """Pick the UTM CRS appropriate for a geometry, based on its centroid.

    The centroid is taken in WGS84 so the zone is derived from real lon/lat
    regardless of what CRS the file uses.

    Raises:
        ValueError: If the geometry is empty or its centroid cannot be projected to lon/lat.
    """
    if geometry.is_empty:
        raise ValueError("Cannot select a projected CRS for an empty geometry")

    geographic = geometry if source_crs == WGS84 else shapely_transform(_transformer(source_crs, WGS84).transform, geometry)
    centroid = geographic.centroid
    if not (math.isfinite(centroid.x) and math.isfinite(centroid.y)):
        raise ValueError("Geometry centroid is not finite after transforming to WGS84")
    return CRS.from_epsg(utm_epsg_for_lonlat(centroid.x, centroid.y))


def project_for_measurement(geometry: BaseGeometry, source_crs: CRS) -> tuple[BaseGeometry, CRS]:
    """Reproject a geometry into its auto-detected UTM CRS (metres).

    The geometry is *always* reprojected, even if the source is already projected,
    because projected CRSs may use feet or other non-metric units.

    Returns:
        ``(projected_geometry, projected_crs)``.

    Raises:
        ValueError: If a suitable CRS cannot be chosen or the result is non-finite.
    """
    target = select_projected_crs(geometry, source_crs)
    projected = shapely_transform(_transformer(source_crs, target).transform, geometry)
    if projected.is_empty or not math.isfinite(projected.bounds[0]):
        raise ValueError(f"Reprojection to {crs_label(target)} produced invalid coordinates")
    return projected, target


def to_wgs84(geometry: BaseGeometry, source_crs: CRS) -> BaseGeometry:
    """Reproject a geometry to WGS84 (lon/lat) for display; a no-op if it is already WGS84.

    Raises:
        ValueError: If the result contains non-finite coordinates.
    """
    if source_crs == WGS84:
        return geometry
    result = shapely_transform(_transformer(source_crs, WGS84).transform, geometry)
    if result.is_empty or not math.isfinite(result.bounds[0]):
        raise ValueError("Reprojection to WGS84 produced invalid coordinates")
    return result


def geometry_to_geojson(geometry: BaseGeometry, source_crs: CRS) -> dict[str, Any]:
    """Serialize a geometry as a 2D GeoJSON geometry in WGS84, as web maps expect.

    Z values are dropped and coordinates rounded to :data:`GEOJSON_DECIMALS` places.

    Raises:
        ValueError: If the geometry cannot be reprojected to valid WGS84 coordinates.
    """
    geographic = shapely.force_2d(to_wgs84(geometry, source_crs))
    rounded = shapely.transform(geographic, lambda coords: np.round(coords, GEOJSON_DECIMALS))
    return json.loads(shapely.to_geojson(rounded))


# --------------------------------------------------------------------------- bulk (vectorized) API
#
# Strategy: the per-feature functions above cost two Python-level PROJ transforms per feature
# (to WGS84 for the zone lookup, then to UTM). For files with many features the bulk functions
# below do the same work in a handful of calls:
#   1. reproject *all* geometries to WGS84 with one GeoSeries.to_crs() call,
#   2. take every centroid and compute every UTM zone with numpy (no Python loop),
#   3. GROUP features by their target UTM CRS and reproject each group with one GeoSeries.to_crs()
#      call (one PROJ call per group, i.e. at most ~60 per source CRS, not one per feature).
# Each feature still receives its own projected geometry and measurement.


def reproject_many(geometries: Sequence[BaseGeometry], source_crs: CRS, target_crs: CRS) -> npt.NDArray[np.object_]:
    """Reproject geometries with a single vectorized PROJ call (a no-op if the CRSs are equal)."""
    series = gpd.GeoSeries(list(geometries), crs=source_crs)
    if source_crs != target_crs:
        series = series.to_crs(target_crs)
    return series.to_numpy()


def to_wgs84_many(geometries: Sequence[BaseGeometry | None], source_crs: CRS) -> list[BaseGeometry | None]:
    """Reproject geometries to WGS84 in one call; ``None``/empty/unprojectable entries yield ``None``.

    If the bulk call fails, falls back to per-geometry transforms so one bad geometry
    cannot take the others down with it.
    """
    out: list[BaseGeometry | None] = [None] * len(geometries)
    positions: list[int] = []
    usable: list[BaseGeometry] = []
    for i, geom in enumerate(geometries):
        if geom is not None and not geom.is_empty:
            positions.append(i)
            usable.append(geom)
    if not usable:
        return out
    try:
        reprojected = reproject_many(usable, source_crs, WGS84)
        finite = np.isfinite(shapely.bounds(reprojected)).all(axis=1)
    except Exception:  # noqa: BLE001 - PROJ/GEOS raise many types; isolate the failure per geometry instead
        logger.warning("Bulk WGS84 reprojection failed; retrying per geometry", exc_info=True)
        for position, geom in zip(positions, usable):
            try:
                out[position] = to_wgs84(geom, source_crs)
            except Exception:  # noqa: BLE001
                logger.warning("Could not reproject geometry to WGS84", extra={"position": position}, exc_info=True)
        return out
    for position, geom, ok in zip(positions, reprojected, finite):
        out[position] = geom if ok else None
    return out


def group_by_utm_epsg(wgs84_geometries: Sequence[BaseGeometry]) -> dict[int, list[int]]:
    """Group positions by the UTM/UPS EPSG code of each geometry's WGS84 centroid.

    Code ``0`` collects geometries whose centroid is not a valid lon/lat.
    """
    centroids = shapely.centroid(np.asarray(wgs84_geometries, dtype=object))
    codes = utm_epsgs_for_lonlat(shapely.get_x(centroids), shapely.get_y(centroids))
    return {int(code): np.flatnonzero(codes == code).tolist() for code in np.unique(codes)}


def _wgs84_geojson_many(geometries: Sequence[BaseGeometry]) -> list[dict[str, Any]]:
    """Serialize WGS84 geometries as 2D, rounded GeoJSON (vectorized)."""
    array = shapely.force_2d(np.asarray(geometries, dtype=object))
    rounded = shapely.transform(array, lambda coords: np.round(coords, GEOJSON_DECIMALS))
    return [json.loads(text) for text in shapely.to_geojson(rounded)]


def geojson_many(wgs84_geometries: Sequence[BaseGeometry | None]) -> list[dict[str, Any] | None]:
    """GeoJSON for each WGS84 geometry (``None`` stays ``None``); never raises."""
    out: list[dict[str, Any] | None] = [None] * len(wgs84_geometries)
    positions: list[int] = []
    usable: list[BaseGeometry] = []
    for i, geom in enumerate(wgs84_geometries):
        if geom is not None:
            positions.append(i)
            usable.append(geom)
    if not usable:
        return out
    try:
        for position, doc in zip(positions, _wgs84_geojson_many(usable)):
            out[position] = doc
    except Exception:  # noqa: BLE001 - display data must never fail the file
        logger.warning("Bulk GeoJSON serialization failed; retrying per geometry", exc_info=True)
        for position, geom in zip(positions, usable):
            try:
                out[position] = _wgs84_geojson_many([geom])[0]
            except Exception:  # noqa: BLE001
                logger.warning("Could not serialize geometry to GeoJSON", extra={"position": position}, exc_info=True)
    return out

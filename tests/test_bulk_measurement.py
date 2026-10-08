"""Bulk (vectorized, grouped-by-UTM-zone) measurement must match the per-feature path exactly."""

from __future__ import annotations

import uuid
from typing import Any

import geopandas as gpd
import numpy as np
import pytest
from pyproj import CRS, Transformer
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from app.services import crs_handler, measurement
from app.services.crs_handler import (
    WGS84,
    geojson_many,
    group_by_utm_epsg,
    to_wgs84_many,
    utm_epsg_for_lonlat,
    utm_epsgs_for_lonlat,
)
from app.services.file_parser import ParsedFeature, ParsedFile
from app.services.file_service import _build_feature_rows
from app.services.measurement import measure_geometries, measure_geometry

# One feature set spread over three UTM zones (43N, 33N, 56S) plus a polar one (UPS north).
ZONE_CENTRES = {32643: (77.2, 28.6), 32633: (15.0, 50.0), 32756: (151.2, -33.9)}


def _mixed_features() -> list[BaseGeometry | None]:
    feats: list[BaseGeometry | None] = []
    for lon, lat in ZONE_CENTRES.values():
        feats += [
            box(lon, lat, lon + 0.01, lat + 0.01),
            LineString([(lon, lat), (lon + 0.02, lat + 0.01)]),
            MultiPolygon([box(lon, lat, lon + 0.005, lat + 0.005), box(lon + 0.01, lat, lon + 0.015, lat + 0.005)]),
            Point(lon, lat),
        ]
    feats += [box(10, 86, 11, 86.5), GeometryCollection([Point(1, 1)]), None, Polygon()]
    return feats


def _assert_same(bulk: list[measurement.MeasurementResult], scalar: list[measurement.MeasurementResult]) -> None:
    assert len(bulk) == len(scalar)
    for b, s in zip(bulk, scalar):
        assert (b.type, b.unit, b.projected_crs) == (s.type, s.unit, s.projected_crs)
        if s.value is None:
            assert b.value is None
        else:
            assert b.value == pytest.approx(s.value, rel=1e-12)


def test_bulk_matches_per_feature_results_across_zones_and_types() -> None:
    feats = _mixed_features()
    bulk = measure_geometries(feats, WGS84, list(range(len(feats))))
    _assert_same(bulk, [measure_geometry(g, WGS84) for g in feats])

    assert {r.projected_crs for r in bulk if r.projected_crs} == {"EPSG:32643", "EPSG:32633", "EPSG:32756", "EPSG:32661"}


def test_bulk_matches_per_feature_for_a_projected_source_crs() -> None:
    web_mercator = CRS.from_epsg(3857)
    to_merc = Transformer.from_crs(WGS84, web_mercator, always_xy=True).transform
    feats = [shp_transform(to_merc, g) for g in _mixed_features()[:12]]
    bulk = measure_geometries(feats, web_mercator, list(range(len(feats))))
    _assert_same(bulk, [measure_geometry(g, web_mercator) for g in feats])


def test_each_feature_gets_its_own_zone_not_the_files() -> None:
    feats = [box(lon, lat, lon + 0.01, lat + 0.01) for lon, lat in ZONE_CENTRES.values()]
    results = measure_geometries(feats, WGS84, [0, 1, 2])
    assert [r.projected_crs for r in results] == [f"EPSG:{code}" for code in ZONE_CENTRES]


def test_one_projection_call_per_utm_group_not_per_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    """300 features in 3 zones must cost 3 PROJ calls (+1 for WGS84 if not supplied), not 300 or 600."""
    targets: list[str] = []
    original = gpd.GeoSeries.to_crs

    def spy(self: gpd.GeoSeries, crs: Any = None, *args: Any, **kwargs: Any) -> gpd.GeoSeries:
        targets.append(CRS.from_user_input(crs).to_string())
        return original(self, crs, *args, **kwargs)

    monkeypatch.setattr(gpd.GeoSeries, "to_crs", spy)
    rng = np.random.default_rng(0)
    feats: list[BaseGeometry | None] = []
    for lon, lat in ZONE_CENTRES.values():
        for dx, dy in rng.uniform(0, 0.3, size=(100, 2)):
            feats.append(box(lon + dx, lat + dy, lon + dx + 0.001, lat + dy + 0.001))

    wgs = to_wgs84_many(feats, WGS84)  # WGS84 source: no transform needed at all
    assert targets == []
    results = measure_geometries(feats, WGS84, list(range(300)), wgs84_geometries=wgs)
    assert sorted(targets) == sorted(f"EPSG:{code}" for code in ZONE_CENTRES)  # exactly one call per zone
    assert all(r.value and r.value > 0 for r in results)

    targets.clear()
    measure_geometries(feats, WGS84, list(range(300)))  # WGS84 source: still no extra WGS84 transform
    assert len(targets) == 3


def test_non_wgs84_source_costs_one_wgs84_call_plus_one_per_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = gpd.GeoSeries.to_crs

    def spy(self: gpd.GeoSeries, *args: Any, **kwargs: Any) -> gpd.GeoSeries:
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(gpd.GeoSeries, "to_crs", spy)
    merc = CRS.from_epsg(3857)
    to_merc = Transformer.from_crs(WGS84, merc, always_xy=True).transform
    feats = [shp_transform(to_merc, box(lon, lat, lon + 0.01, lat + 0.01)) for lon, lat in ZONE_CENTRES.values() for _ in range(50)]
    measure_geometries(feats, merc, list(range(len(feats))))
    assert calls == 1 + 3


def test_failing_group_falls_back_to_per_feature_without_losing_neighbours(monkeypatch: pytest.MonkeyPatch) -> None:
    feats = _mixed_features()
    expected = [measure_geometry(g, WGS84) for g in feats]

    real = measurement.reproject_many
    state = {"failed": 0}

    def flaky(geoms: Any, source: CRS, target: CRS) -> Any:
        if state["failed"] == 0:  # first group blows up
            state["failed"] += 1
            raise RuntimeError("simulated PROJ failure")
        return real(geoms, source, target)

    monkeypatch.setattr(measurement, "reproject_many", flaky)
    _assert_same(measure_geometries(feats, WGS84, list(range(len(feats)))), expected)
    assert state["failed"] == 1


def test_unprojectable_feature_is_null_and_does_not_affect_others() -> None:
    good = box(77.2, 28.6, 77.21, 28.61)
    absurd = Polygon([(1e9, 1e9), (1e9 + 1, 1e9), (1e9, 1e9 + 1)])
    results = measure_geometries([good, absurd, good], WGS84, [0, 1, 2])
    assert results[1].value is None
    assert results[0].value == pytest.approx(results[2].value) and results[0].value is not None


def test_empty_input_and_nothing_measurable() -> None:
    assert measure_geometries([], WGS84, []) == []
    only_points = measure_geometries([Point(1, 1), None], WGS84, [0, 1])
    assert all(r.value is None for r in only_points)


# ------------------------------------------------------------------------- vectorized zone lookup


def test_vectorized_zones_match_scalar_function() -> None:
    rng = np.random.default_rng(7)
    lons = np.concatenate([rng.uniform(-540, 540, 500), [-180.0, 180.0, 179.999999, 0.0, 6.0, -6.0]])
    lats = np.concatenate([rng.uniform(-90, 90, 500), [0.0, -0.0, 84.0, 84.0001, -80.0, -80.0001]])
    expected = [utm_epsg_for_lonlat(lo, la) for lo, la in zip(lons, lats)]
    assert utm_epsgs_for_lonlat(lons, lats).tolist() == expected


def test_invalid_positions_map_to_zero_and_scalar_raises() -> None:
    codes = utm_epsgs_for_lonlat([np.nan, 10.0, np.inf, 10.0], [10.0, np.nan, 10.0, 95.0])
    assert codes.tolist() == [0, 0, 0, 0]
    with pytest.raises(ValueError):
        utm_epsg_for_lonlat(float("nan"), 10.0)


def test_group_by_utm_epsg_returns_positions_per_zone() -> None:
    geoms = [Point(77.2, 28.6), Point(15, 50), Point(77.3, 28.7), Point(10, 86)]
    assert group_by_utm_epsg(geoms) == {32633: [1], 32643: [0, 2], 32661: [3]}


# ------------------------------------------------------------------------------------- GeoJSON


def test_geojson_many_keeps_none_drops_z_and_rounds() -> None:
    docs = geojson_many([Point(77.123456789, 28.987654321, 215.0), None, box(0, 0, 1, 1)])
    assert docs[0] == {"type": "Point", "coordinates": [77.123457, 28.987654]}
    assert docs[1] is None and docs[2]["type"] == "Polygon"
    assert geojson_many([]) == [] and geojson_many([None]) == [None]


def test_to_wgs84_many_handles_none_empty_and_non_finite() -> None:
    utm = CRS.from_epsg(32643)
    out = to_wgs84_many([box(500000, 3100000, 501000, 3101000), None, Polygon(), Point(1e30, 1e30)], utm)
    assert out[0] is not None and 74 < out[0].bounds[0] < 76
    assert out[1] is None and out[2] is None
    assert out[3] is None  # PROJ yields inf; reported as unavailable instead of poisoning the batch


# ------------------------------------------------------------------- end to end through file_service


def test_build_feature_rows_batches_by_crs_but_preserves_feature_order() -> None:
    utm = CRS.from_epsg(32643)  # two distinct CRS objects, interleaved in the file
    to_utm = Transformer.from_crs(WGS84, utm, always_xy=True).transform
    a, b = box(77.2, 28.6, 77.21, 28.61), box(77.3, 28.6, 77.31, 28.61)
    features = [
        ParsedFeature(0, a, "Polygon", a.wkt, WGS84, {"n": 0}),
        ParsedFeature(1, shp_transform(to_utm, b), "Polygon", b.wkt, utm, {"n": 1}),
        ParsedFeature(2, a, "Polygon", a.wkt, WGS84, {"n": 2}),
        ParsedFeature(3, None, "None", None, utm, {"n": 3}),
    ]
    rows = _build_feature_rows(uuid.uuid4(), ParsedFile("EPSG:4326, EPSG:32643", features))

    assert [r["feature_index"] for r in rows] == [0, 1, 2, 3]
    assert [r["crs"] for r in rows] == ["EPSG:4326", "EPSG:32643", "EPSG:4326", "EPSG:32643"]
    assert [r["properties"]["n"] for r in rows] == [0, 1, 2, 3]
    assert rows[0]["measurement_value"] == pytest.approx(rows[2]["measurement_value"])
    assert rows[1]["measurement_value"] == pytest.approx(rows[0]["measurement_value"], rel=1e-2)
    ring = rows[1]["geometry_geojson"]["coordinates"][0]
    assert min(lon for lon, _ in ring) == pytest.approx(77.3, abs=1e-4)  # reprojected back to lon/lat
    assert min(lat for _, lat in ring) == pytest.approx(28.6, abs=1e-4)
    assert rows[3]["measurement_value"] is None and rows[3]["geometry_geojson"] is None


def test_scalar_helpers_still_agree_with_bulk_helpers() -> None:
    g = box(77.2, 28.6, 77.21, 28.61)
    assert crs_handler.geometry_to_geojson(g, WGS84) == geojson_many([g])[0]

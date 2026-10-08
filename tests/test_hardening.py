"""Tests for CRS labelling, startup recovery, partial-read warnings, health, CORS, listing and GeoJSON."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import pytest
from fastapi import FastAPI
from pyproj import CRS
from shapely.geometry import Point, box

from app.core.exceptions import register_exception_handlers
from app.core.static import mount_frontend
from app.db.session import async_session_factory
from app.models.models import FileStatus, UploadedFile
from app.services import file_parser, file_service
from app.services.crs_handler import crs_label, geometry_to_geojson
from tests.test_files import KML_MIXED, _kml, _shapefile_zip, _upload

CUSTOM_PROJ = "+proj=tmerc +lat_0=0 +lon_0=76.3 +k=0.99987 +x_0=500000 +y_0=0 +ellps=WGS84 +units=m +no_defs"
# Looks like UTM 43N but is not: PROJ fuzzy-matches this to EPSG:32643 below 100 % confidence.
LOOKALIKE_PROJ = "+proj=tmerc +lat_0=0 +lon_0=75 +k=0.9996 +x_0=500000 +y_0=0 +ellps=WGS84 +units=m +no_defs"


# ------------------------------------------------------------------------ CRS labels


@pytest.mark.parametrize("epsg", [4326, 3857, 32643, 27700])
def test_exact_epsg_crs_is_labelled_with_code(epsg: int) -> None:
    assert crs_label(CRS.from_epsg(epsg)) == f"EPSG:{epsg}"


def test_custom_crs_is_labelled_custom_and_bounded() -> None:
    label = crs_label(CRS.from_proj4(CUSTOM_PROJ))
    assert label.startswith("CUSTOM:")
    assert len(label) == len("CUSTOM:") + 100


def test_lookalike_crs_is_never_reported_as_a_guessed_epsg_code() -> None:
    crs = CRS.from_proj4(LOOKALIKE_PROJ)
    assert crs.to_epsg() is not None  # the default 70 % confidence *would* have guessed
    assert crs_label(crs).startswith("CUSTOM:")


async def test_custom_crs_shapefile_is_processed_and_stored(client: httpx.AsyncClient, tmp_path: Path) -> None:
    gdf = gpd.GeoDataFrame(geometry=[box(500000, 3100000, 501000, 3101000)], crs=CUSTOM_PROJ)
    fid = (await _upload(client, "custom.zip", _shapefile_zip(tmp_path, gdf))).json()["id"]

    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "COMPLETED"
    assert info["crs"].startswith("CUSTOM:")
    item = (await client.get(f"/api/files/{fid}/measurements/")).json()["items"][0]
    assert item["crs"].startswith("CUSTOM:")
    assert item["measurement"]["unit"] == "m²"
    assert item["measurement"]["projected_crs"].startswith("EPSG:326")


def test_crs_columns_are_unbounded_text() -> None:
    """VARCHAR(n) would make PostgreSQL reject long labels while SQLite silently accepts them."""
    from sqlalchemy import Text

    from app.models.models import Feature

    for model, column in [(UploadedFile, "crs"), (Feature, "crs"), (Feature, "measurement_crs")]:
        assert isinstance(model.__table__.c[column].type, Text), f"{model.__name__}.{column}"


# ------------------------------------------------------------------ startup recovery


async def test_every_in_flight_job_is_failed_on_startup() -> None:
    ids = {status: uuid.uuid4() for status in FileStatus}
    async with async_session_factory() as s:
        for status, fid in ids.items():
            s.add(UploadedFile(id=fid, filename="x.kml", stored_path="x", file_size=1, status=status))
        await s.commit()

    async with async_session_factory() as s:
        assert await file_service.fail_interrupted_jobs(s) == 2  # PENDING + PROCESSING, no age threshold

    async with async_session_factory() as s:
        for status in (FileStatus.PENDING, FileStatus.PROCESSING):
            rec = await s.get(UploadedFile, ids[status])
            assert rec is not None
            assert rec.status == FileStatus.FAILED
            assert rec.error_message == "Service restarted before processing completed"
        for status in (FileStatus.COMPLETED, FileStatus.FAILED):
            rec = await s.get(UploadedFile, ids[status])
            assert rec is not None and rec.status == status and rec.error_message is None


async def test_interrupted_job_reports_reason_via_api(client: httpx.AsyncClient) -> None:
    fid = uuid.uuid4()
    async with async_session_factory() as s:
        s.add(UploadedFile(id=fid, filename="x.kml", stored_path="x", file_size=1, status=FileStatus.PROCESSING))
        await s.commit()
        await file_service.fail_interrupted_jobs(s)
    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "FAILED" and info["error"] == "Service restarted before processing completed"


# --------------------------------------------------------------------- KML warnings

TWO_FOLDERS = _kml(
    "<Folder><name>Good</name><Placemark><name>P1</name><Point><coordinates>77.0,28.0</coordinates></Point></Placemark></Folder>"
    "<Folder><name>Bad</name><Placemark><name>P2</name><Point><coordinates>77.1,28.1</coordinates></Point></Placemark></Folder>"
)


def _fail_layers(monkeypatch: pytest.MonkeyPatch, failing: set[str]) -> None:
    real_read_file = gpd.read_file

    def flaky(path: Any, **kwargs: Any) -> gpd.GeoDataFrame:
        if kwargs.get("layer") in failing:
            raise RuntimeError("simulated GDAL failure")
        return real_read_file(path, **kwargs)

    monkeypatch.setattr(file_parser.gpd, "read_file", flaky)


async def test_unreadable_kml_layer_yields_completed_file_with_warning(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_layers(monkeypatch, {"Bad"})
    fid = (await _upload(client, "folders.kml", TWO_FOLDERS)).json()["id"]

    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 1
    assert len(info["warnings"]) == 1 and "Bad" in info["warnings"][0]
    assert info["error"] is None


async def test_clean_file_has_empty_warnings(client: httpx.AsyncClient) -> None:
    fid = (await _upload(client, "ok.kml", KML_MIXED)).json()["id"]
    assert (await client.get(f"/api/files/{fid}/")).json()["warnings"] == []


async def test_all_kml_layers_unreadable_fails_with_reason(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_layers(monkeypatch, {"Good", "Bad"})
    fid = (await _upload(client, "folders.kml", TWO_FOLDERS)).json()["id"]
    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "FAILED" and "skipped" in info["error"]


# ------------------------------------------------------------------ health and CORS


async def test_health_returns_503_when_database_is_down(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        async def __aenter__(self) -> None:
            raise ConnectionRefusedError("db down")

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr("app.main.async_session_factory", lambda: Broken())
    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Database unavailable", "code": "DB_UNAVAILABLE"}


async def test_cors_headers_on_simple_and_preflight_requests(client: httpx.AsyncClient) -> None:
    simple = await client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert simple.headers["access-control-allow-origin"] == "*"

    preflight = await client.options(
        "/api/files/",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert "POST" in preflight.headers["access-control-allow-methods"]


def test_allowed_origins_parsing() -> None:
    from app.core.config import _parse_origins

    assert _parse_origins(None, "development") == ("*",)
    assert _parse_origins(None, "production") == ()
    assert _parse_origins(" https://a.com, https://b.com ,", "production") == ("https://a.com", "https://b.com")


# ------------------------------------------------------------------------- listing


async def test_file_listing_is_newest_first_and_paginated(client: httpx.AsyncClient) -> None:
    names = [f"f{i}.kml" for i in range(5)]
    for name in names:
        assert (await _upload(client, name, KML_MIXED)).status_code == 202

    page1 = (await client.get("/api/files/", params={"page_size": 2})).json()
    assert (page1["total"], page1["total_pages"], page1["page"], page1["page_size"]) == (5, 3, 1, 2)
    assert [i["filename"] for i in page1["items"]] == ["f4.kml", "f3.kml"]
    assert set(page1["items"][0]) == {"id", "filename", "status", "feature_count", "crs", "created_at"}
    assert page1["items"][0]["status"] == "COMPLETED" and page1["items"][0]["feature_count"] == 3

    page3 = (await client.get("/api/files/", params={"page": 3, "page_size": 2})).json()
    assert [i["filename"] for i in page3["items"]] == ["f0.kml"]


async def test_file_listing_defaults_and_limits(client: httpx.AsyncClient) -> None:
    empty = (await client.get("/api/files/")).json()
    assert empty == {"page": 1, "page_size": 20, "total": 0, "total_pages": 0, "items": []}

    too_big = await client.get("/api/files/", params={"page_size": 101})
    assert too_big.status_code == 422 and too_big.json()["code"] == "VALIDATION_ERROR"
    assert (await client.get("/api/files/", params={"page": 0})).status_code == 422


# ------------------------------------------------------------------------- GeoJSON


def test_geometry_to_geojson_reprojects_drops_z_and_rounds() -> None:
    result = geometry_to_geojson(Point(500000, 3100000), CRS.from_epsg(32643))
    assert result["type"] == "Point"
    lon, lat = result["coordinates"]
    assert (lon, lat) == pytest.approx((75.0, 28.0), abs=0.5)  # central meridian of zone 43 is 75°E
    assert lon == round(lon, 6) and len(result["coordinates"]) == 2


async def test_measurements_include_wgs84_geojson_for_projected_source(client: httpx.AsyncClient, tmp_path: Path) -> None:
    gdf = gpd.GeoDataFrame(geometry=[box(500000, 3100000, 501000, 3101000)], crs="EPSG:32643")
    fid = (await _upload(client, "utm.zip", _shapefile_zip(tmp_path, gdf))).json()["id"]
    item = (await client.get(f"/api/files/{fid}/measurements/")).json()["items"][0]

    assert item["geometry"].startswith("POLYGON ((50")  # WKT stays in the original CRS
    gj = item["geometry_geojson"]
    assert gj["type"] == "Polygon"
    for lon, lat in gj["coordinates"][0]:  # but GeoJSON is lon/lat degrees
        assert 74 < lon < 76 and 27 < lat < 29


async def test_geojson_is_2d_for_kml_with_z(client: httpx.AsyncClient) -> None:
    fid = (
        await _upload(
            client,
            "z.kml",
            _kml("<Placemark><Point><coordinates>77.0,28.0,215.5</coordinates></Point></Placemark>"),
        )
    ).json()["id"]
    item = (await client.get(f"/api/files/{fid}/measurements/")).json()["items"][0]
    assert item["geometry"].startswith("POINT Z")
    assert item["geometry_geojson"] == {"type": "Point", "coordinates": [77.0, 28.0]}


# ----------------------------------------------------------------- frontend serving


@pytest.fixture
def spa_client(tmp_path: Path) -> httpx.AsyncClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/api/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    assert mount_frontend(app, dist) is True
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://spa")


async def test_spa_serves_index_assets_and_falls_back(spa_client: httpx.AsyncClient) -> None:
    async with spa_client as c:
        assert "SPA" in (await c.get("/")).text
        assert "SPA" in (await c.get("/some/client/route")).text
        assert (await c.get("/assets/app.js")).text == "console.log(1)"
        assert (await c.get("/api/ping")).json() == {"ok": "yes"}


async def test_spa_fallback_never_masks_api_or_missing_assets(spa_client: httpx.AsyncClient) -> None:
    async with spa_client as c:
        api_404 = await c.get("/api/unknown")
        assert api_404.status_code == 404 and api_404.json()["code"] == "NOT_FOUND"
        assert (await c.get("/assets/missing.js")).status_code == 404


def test_mount_frontend_is_skipped_without_a_build(tmp_path: Path) -> None:
    assert mount_frontend(FastAPI(), tmp_path / "nope") is False

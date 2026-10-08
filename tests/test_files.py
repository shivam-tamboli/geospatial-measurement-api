"""Unit tests for CRS/measurement logic and integration tests for the REST API."""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

import geopandas as gpd
import httpx
import pytest
from pyproj import CRS
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import transform as shp_transform
from pyproj import Transformer

from app.services.crs_handler import utm_epsg_for_lonlat
from app.services.measurement import measure_geometry

WGS84 = CRS.from_epsg(4326)


# ----------------------------------------------------------------------------- helpers


def _utm_to_wgs84(geom, utm_epsg: int):
    t = Transformer.from_crs(CRS.from_epsg(utm_epsg), WGS84, always_xy=True)
    return shp_transform(t.transform, geom)


def _kml(placemarks: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + placemarks + "</Document></kml>"
    ).encode()


KML_MIXED = _kml(
    "<Placemark><name>Plot A</name><Polygon><outerBoundaryIs><LinearRing><coordinates>"
    "77.0,28.0 77.01,28.0 77.01,28.01 77.0,28.01 77.0,28.0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    "<Placemark><name>Road</name><LineString><coordinates>77.0,28.0 77.01,28.0</coordinates></LineString></Placemark>"
    "<Placemark><name>Well</name><Point><coordinates>77.005,28.005</coordinates></Point></Placemark>"
)


def _shapefile_zip(tmp_path: Path, gdf: gpd.GeoDataFrame | list[gpd.GeoDataFrame], include_prj: bool = True) -> bytes:
    """Zip one or more GeoDataFrames as Shapefiles (``layer0.shp``, ``layer1.shp`` …)."""
    out = tmp_path / "shp"
    out.mkdir()
    for i, layer in enumerate(gdf if isinstance(gdf, list) else [gdf]):
        layer.to_file(out / f"layer{i}.shp")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in out.iterdir():
            if f.suffix == ".prj" and not include_prj:
                continue
            zf.write(f, f.name)
    return buf.getvalue()


async def _upload(client: httpx.AsyncClient, name: str, data: bytes) -> httpx.Response:
    return await client.post("/api/files/", files={"file": (name, data)})


# ------------------------------------------------------------------------------ CRS


@pytest.mark.parametrize(
    ("lon", "lat", "expected"),
    [
        (77.0, 28.0, 32643),  # Delhi region, N hemisphere
        (151.2, -33.9, 32756),  # Sydney, S hemisphere
        (-0.1, 51.5, 32630),  # London
        (180.0, 10.0, 32601),  # +180 wraps to zone 1
        (-180.0, 10.0, 32601),
        (179.99, 10.0, 32660),
        (10.0, 85.0, 32661),  # polar → UPS north
        (10.0, -85.0, 32761),  # polar → UPS south
    ],
)
def test_utm_zone_selection(lon: float, lat: float, expected: int) -> None:
    assert utm_epsg_for_lonlat(lon, lat) == expected


# ------------------------------------------------------------------------ measurement


def test_polygon_area_is_in_square_metres() -> None:
    square = _utm_to_wgs84(box(500000, 3100000, 501000, 3101000), 32643)  # 1 km x 1 km
    result = measure_geometry(square, WGS84)
    assert result.type == "area"
    assert result.unit == "m²"
    assert result.projected_crs == "EPSG:32643"
    assert result.value == pytest.approx(1_000_000, rel=1e-3)  # degrees² would be ~1e-4


def test_linestring_length_in_metres() -> None:
    line = _utm_to_wgs84(LineString([(500000, 3100000), (500000, 3102000)]), 32643)
    result = measure_geometry(line, WGS84)
    assert result.type == "length" and result.unit == "m"
    assert result.value == pytest.approx(2000, rel=1e-3)


def test_multipolygon_sums_areas() -> None:
    mp = MultiPolygon([_utm_to_wgs84(box(500000, 3100000, 500100, 3100100), 32643)] * 2)
    assert measure_geometry(mp, WGS84).value == pytest.approx(20_000, rel=1e-3)


def test_non_wgs84_source_crs_is_reprojected() -> None:
    web_mercator = CRS.from_epsg(3857)
    t = Transformer.from_crs(WGS84, web_mercator, always_xy=True)
    poly = shp_transform(t.transform, _utm_to_wgs84(box(500000, 3100000, 501000, 3101000), 32643))
    assert measure_geometry(poly, web_mercator).value == pytest.approx(1_000_000, rel=1e-3)


@pytest.mark.parametrize("geom", [Point(77, 28), None, Polygon(), GeometryCollection([Point(77, 28)])])
def test_unmeasurable_geometries_return_null_without_raising(geom) -> None:
    result = measure_geometry(geom, WGS84)
    assert result.value is None and result.unit is None and result.type is None


def test_out_of_range_coordinates_do_not_crash() -> None:
    result = measure_geometry(Polygon([(1e9, 1e9), (1e9 + 1, 1e9), (1e9, 1e9 + 1)]), WGS84)
    assert result.value is None


# ----------------------------------------------------------------------------- API


async def test_kml_upload_end_to_end(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "survey.kml", KML_MIXED)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "PENDING" and body["filename"] == "survey.kml"

    info = (await client.get(f"/api/files/{body['id']}/")).json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 3
    assert info["crs"] == "EPSG:4326"

    page = (await client.get(f"/api/files/{body['id']}/measurements/")).json()
    assert page["total"] == 3
    by_type = {i["geometry_type"]: i for i in page["items"]}
    assert by_type["Polygon"]["measurement"]["unit"] == "m²"
    assert by_type["Polygon"]["measurement"]["value"] == pytest.approx(1_090_000, rel=0.01)
    assert by_type["Polygon"]["measurement"]["projected_crs"] == "EPSG:32643"
    assert by_type["Polygon"]["geometry"].startswith("POLYGON")
    assert by_type["Polygon"]["crs"] == "EPSG:4326"
    assert by_type["Polygon"]["properties"]["Name"] == "Plot A"
    assert by_type["LineString"]["measurement"]["unit"] == "m"
    assert by_type["Point"]["measurement"] == {"type": None, "value": None, "unit": None, "projected_crs": None}
    assert [i["feature_id"] for i in page["items"]] == [0, 1, 2]


async def test_shapefile_zip_upload_with_projected_crs(client: httpx.AsyncClient, tmp_path: Path) -> None:
    polygons = gpd.GeoDataFrame(
        {"name": ["a"], "n": [1]}, geometry=[box(500000, 3100000, 501000, 3101000)], crs="EPSG:32643"
    )
    lines = gpd.GeoDataFrame(
        {"name": ["b"], "n": [None]}, geometry=[LineString([(500000, 3100000), (500000, 3101500)])], crs="EPSG:32643"
    )
    resp = await _upload(client, "parcels.zip", _shapefile_zip(tmp_path, [polygons, lines]))
    assert resp.status_code == 202
    fid = resp.json()["id"]

    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "COMPLETED" and info["crs"] == "EPSG:32643"

    items = (await client.get(f"/api/files/{fid}/measurements/")).json()["items"]
    assert items[0]["measurement"]["value"] == pytest.approx(1_000_000, rel=1e-6)
    assert items[1]["measurement"]["value"] == pytest.approx(1500, rel=1e-6)
    assert items[1]["properties"] == {"name": "b", "n": None}  # NaN → null, JSON-safe
    assert [i["feature_id"] for i in items] == [0, 1]  # numbered consecutively across shapefiles


async def test_pagination(client: httpx.AsyncClient, tmp_path: Path) -> None:
    gdf = gpd.GeoDataFrame(geometry=[Point(77 + i / 100, 28) for i in range(7)], crs="EPSG:4326")
    fid = (await _upload(client, "pts.zip", _shapefile_zip(tmp_path, gdf))).json()["id"]

    p2 = (await client.get(f"/api/files/{fid}/measurements/", params={"page": 2, "page_size": 3})).json()
    assert (p2["total"], p2["total_pages"], p2["page"]) == (7, 3, 2)
    assert [i["feature_id"] for i in p2["items"]] == [3, 4, 5]
    p3 = (await client.get(f"/api/files/{fid}/measurements/", params={"page": 3, "page_size": 3})).json()
    assert [i["feature_id"] for i in p3["items"]] == [6]
    beyond = (await client.get(f"/api/files/{fid}/measurements/", params={"page": 9, "page_size": 3})).json()
    assert beyond["items"] == []


async def test_invalid_pagination_params_use_error_format(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/api/files/{uuid.uuid4()}/measurements/", params={"page": 0})
    assert resp.status_code == 422
    assert set(resp.json()) == {"detail", "code"} and resp.json()["code"] == "VALIDATION_ERROR"


async def test_invalid_file_type_400(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "notes.txt", b"hello")
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_FILE_TYPE"


async def test_extension_content_mismatch_400(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "fake.zip", b"this is not a zip")
    assert resp.status_code == 400 and resp.json()["code"] == "INVALID_FILE_TYPE"


async def test_empty_file_400(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "empty.kml", b"")
    assert resp.status_code == 400 and resp.json()["code"] == "EMPTY_FILE"


async def test_oversized_file_413(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "big.kml", b"<kml>" + b"x" * (2 * 1024 * 1024))  # limit is 1 MB in tests
    assert resp.status_code == 413 and resp.json()["code"] == "FILE_TOO_LARGE"


async def test_missing_file_field_uses_error_format(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/files/")
    assert resp.status_code == 422 and resp.json()["code"] == "VALIDATION_ERROR"


async def test_corrupt_zip_rejected_at_upload_422(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "bad.zip", b"PK\x03\x04 truncated garbage")
    assert resp.status_code == 422 and resp.json()["code"] == "CORRUPT_FILE"


async def test_non_kml_xml_rejected_at_upload_422(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "x.kml", b"<?xml version='1.0'?><html></html>")
    assert resp.status_code == 422 and resp.json()["code"] == "CORRUPT_FILE"


async def test_unparseable_kml_fails_in_background_and_measurements_422(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "broken.kml", b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>')
    assert resp.status_code == 202  # passes the cheap check; fails during real parse
    fid = resp.json()["id"]

    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "FAILED" and info["error"]

    m = await client.get(f"/api/files/{fid}/measurements/")
    assert m.status_code == 422 and m.json()["code"] == "FILE_PROCESSING_FAILED"
    assert "Traceback" not in m.text


async def test_shapefile_without_prj_fails_with_clear_message(client: httpx.AsyncClient, tmp_path: Path) -> None:
    gdf = gpd.GeoDataFrame(geometry=[Point(1, 1)], crs="EPSG:4326")
    fid = (await _upload(client, "noprj.zip", _shapefile_zip(tmp_path, gdf, include_prj=False))).json()["id"]
    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "FAILED" and ".prj" in info["error"]


async def test_zip_slip_is_rejected(client: httpx.AsyncClient) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.shp", b"x")
    fid = (await _upload(client, "evil.zip", buf.getvalue())).json()["id"]
    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "FAILED" and "unsafe" in info["error"]


async def test_unknown_file_404(client: httpx.AsyncClient) -> None:
    for path in ("", "measurements/"):
        resp = await client.get(f"/api/files/{uuid.uuid4()}/{path}")
        assert resp.status_code == 404
        assert resp.json()["code"] == "FILE_NOT_FOUND"


async def test_unknown_route_uses_error_format(client: httpx.AsyncClient) -> None:
    resp = await client.get("/nope")
    assert resp.status_code == 404 and resp.json()["code"] == "NOT_FOUND"


async def test_measurements_before_ready_409(client: httpx.AsyncClient) -> None:
    from app.db.session import async_session_factory
    from app.models.models import FileStatus, UploadedFile

    fid = uuid.uuid4()
    async with async_session_factory() as s:
        s.add(UploadedFile(id=fid, filename="x.kml", stored_path="x", file_size=1, status=FileStatus.PROCESSING))
        await s.commit()
    resp = await client.get(f"/api/files/{fid}/measurements/")
    assert resp.status_code == 409 and resp.json()["code"] == "FILE_NOT_READY"


async def test_path_traversal_in_filename_is_neutralised(client: httpx.AsyncClient) -> None:
    resp = await _upload(client, "../../etc/passwd.kml", KML_MIXED)
    assert resp.status_code == 202 and resp.json()["filename"] == "passwd.kml"


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}

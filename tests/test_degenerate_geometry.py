"""A feature whose geometry Shapely cannot build (e.g. a one-point LineString) must not take its layer down."""

from __future__ import annotations

import zipfile
from pathlib import Path

import fiona
import httpx
import pytest
import shapely
from shapely.geometry import LineString, Point, box

from app.services import file_parser, measurement
from app.services.crs_handler import WGS84
from app.services.file_parser import parse_geospatial_file
from app.services.measurement import NO_MEASUREMENT, measure_geometries, measure_geometry
from tests.test_files import _kml, _upload

LIMIT = 10**9
BAD_LINE_WARNING = "Feature {i}: LineString has fewer than 2 points — skipped (geometry set to null)"


def line(name: str, coords: str) -> str:
    return f"<Placemark><name>{name}</name><LineString><coordinates>{coords}</coordinates></LineString></Placemark>"


def point(name: str) -> str:
    return f"<Placemark><name>{name}</name><Point><coordinates>77,28</coordinates></Point></Placemark>"


def folder(name: str, body: str) -> str:
    return f"<Folder><name>{name}</name>{body}</Folder>"


def parse_kml(tmp_path: Path, body: str) -> file_parser.ParsedFile:
    path = tmp_path / "doc.kml"
    path.write_bytes(_kml(body))
    return parse_geospatial_file(path, ".kml", LIMIT, 100)


GOOD1, GOOD2 = line("good1", "77,28 77.01,28"), line("good2", "78,28 78.01,28")


# ---------------------------------------------------------------- premise: Shapely cannot hold such a line


def test_shapely_cannot_represent_a_one_point_linestring() -> None:
    """Documents why the fix is at read time: no such geometry ever exists to inspect after reading."""
    with pytest.raises(shapely.errors.GEOSException):
        LineString([(1, 1)])
    with pytest.raises(shapely.errors.GEOSException):
        shapely.from_wkt("LINESTRING (1 1)")


# ------------------------------------------------------------------------- 1. the layer survives


def test_kml_layer_with_a_single_coordinate_linestring_still_succeeds(tmp_path: Path) -> None:
    parsed = parse_kml(tmp_path, folder("F", GOOD1 + line("bad", "77,28") + point("pt") + GOOD2))

    by_name = {f.properties["Name"]: f for f in parsed.features}
    assert [f.properties["Name"] for f in parsed.features] == ["good1", "bad", "pt", "good2"]  # nothing dropped or reordered
    assert by_name["bad"].geometry is None and by_name["bad"].wkt is None  # only this geometry is skipped
    assert by_name["bad"].geometry_type == "LineString"  # it keeps its real type, not "None"
    assert by_name["good1"].wkt == "LINESTRING (77 28, 77.01 28)"
    assert by_name["good2"].wkt == "LINESTRING (78 28, 78.01 28)"
    assert by_name["pt"].wkt == "POINT (77 28)"
    assert parsed.warnings == [BAD_LINE_WARNING.format(i=1)]  # the warning carries the feature's index


def test_unparseable_vertex_that_leaves_a_one_point_line_is_handled_the_same_way(tmp_path: Path) -> None:
    """GDAL drops the 'abc,def' vertex silently, leaving a single point."""
    parsed = parse_kml(tmp_path, folder("F", line("bad", "abc,def 1,2") + point("pt")))
    assert [f.geometry is None for f in parsed.features] == [True, False]
    assert parsed.warnings == [BAD_LINE_WARNING.format(i=0)]


def test_a_file_whose_only_feature_is_bad_completes_with_a_null_geometry(tmp_path: Path) -> None:
    parsed = parse_kml(tmp_path, folder("F", line("bad", "77,28")))
    assert len(parsed.features) == 1 and parsed.features[0].geometry is None
    assert parsed.warnings == [BAD_LINE_WARNING.format(i=0)]


def test_bad_feature_in_one_folder_does_not_touch_the_other_folder(tmp_path: Path) -> None:
    parsed = parse_kml(tmp_path, folder("A", GOOD1 + line("bad", "77,28")) + folder("B", GOOD2 + point("pt")))
    assert sorted(f.properties["Name"] for f in parsed.features) == ["bad", "good1", "good2", "pt"]
    assert sum(f.geometry is None for f in parsed.features) == 1
    assert len(parsed.warnings) == 1 and "fewer than 2 points" in parsed.warnings[0]


def test_feature_numbering_stays_consecutive_across_layers_when_a_geometry_is_nulled(tmp_path: Path) -> None:
    parsed = parse_kml(tmp_path, folder("A", line("bad", "77,28") + GOOD1) + folder("B", GOOD2))
    assert [f.index for f in parsed.features] == [0, 1, 2]
    bad = next(f for f in parsed.features if f.geometry is None)
    assert parsed.warnings == [BAD_LINE_WARNING.format(i=bad.index)]


def test_shapefile_with_a_one_vertex_polyline_still_succeeds(tmp_path: Path) -> None:
    out = tmp_path / "shp"
    out.mkdir()
    schema = {"geometry": "LineString", "properties": {"n": "int"}}
    with fiona.open(out / "l.shp", "w", driver="ESRI Shapefile", crs="EPSG:4326", schema=schema) as dst:
        for n, coords in enumerate([[(77, 28), (77.01, 28)], [(77, 28)], [(78, 28), (78.01, 28)]], start=1):
            dst.write({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": {"n": n}})
    archive = tmp_path / "l.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for f in out.iterdir():
            zf.write(f, f.name)

    parsed = parse_geospatial_file(archive, ".zip", LIMIT, 100)
    assert [(f.properties["n"], f.geometry is None) for f in parsed.features] == [(1, False), (2, True), (3, False)]
    assert parsed.warnings == [BAD_LINE_WARNING.format(i=1)]


def test_warnings_are_capped_so_a_bad_file_cannot_flood_the_record(tmp_path: Path) -> None:
    parsed = parse_kml(tmp_path, folder("F", "".join(line(f"bad{i}", "77,28") for i in range(25)) + point("pt")))
    assert len(parsed.features) == 26 and sum(f.geometry is None for f in parsed.features) == 25
    assert len(parsed.warnings) == 11  # 10 verbatim + 1 summary
    assert parsed.warnings[0] == BAD_LINE_WARNING.format(i=0)
    assert parsed.warnings[-1] == "...and 15 more features had geometries that could not be built."


def test_a_clean_file_never_takes_the_per_feature_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("tolerant reader must only run after a geometry-building failure")

    monkeypatch.setattr(file_parser, "_read_features_tolerant", forbidden)
    parsed = parse_kml(tmp_path, folder("F", GOOD1 + point("pt")))
    assert parsed.warnings == [] and len(parsed.features) == 2


def test_genuine_driver_failures_are_not_mistaken_for_a_bad_geometry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ValueError / Shapely errors trigger the retry; anything else still reports the layer as unreadable."""
    real = file_parser.gpd.read_file

    def failing(path: object, **kwargs: object) -> object:
        if kwargs.get("layer") == "Bad":
            raise RuntimeError("simulated driver failure")
        return real(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_parser.gpd, "read_file", failing)
    parsed = parse_kml(tmp_path, folder("Good", point("g")) + folder("Bad", point("b")))
    assert [f.properties["Name"] for f in parsed.features] == ["g"]
    assert len(parsed.warnings) == 1 and "Bad" in parsed.warnings[0]


async def test_end_to_end_file_completes_and_only_the_bad_feature_has_no_measurement(client: httpx.AsyncClient) -> None:
    body = folder("F", GOOD1 + line("bad", "77,28") + point("pt") + GOOD2)
    fid = (await _upload(client, "degenerate.kml", _kml(body))).json()["id"]

    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "COMPLETED" and info["feature_count"] == 4  # before the fix: FAILED, no features at all
    assert info["warnings"] == [BAD_LINE_WARNING.format(i=1)]

    items = {i["properties"]["Name"]: i for i in (await client.get(f"/api/files/{fid}/measurements/")).json()["items"]}
    assert items["bad"]["geometry"] is None and items["bad"]["geometry_geojson"] is None
    assert items["bad"]["geometry_type"] == "LineString" and items["bad"]["measurement"]["value"] is None
    for name in ("good1", "good2"):
        assert items[name]["measurement"]["type"] == "length" and 900 < items[name]["measurement"]["value"] < 1100
    assert items["pt"]["measurement"]["value"] is None


# ---------------------------------------------------------- 2. measurement.py called directly


class _OnePointLine:
    """Stand-in for a line with a single point, which Shapely itself refuses to construct."""

    geom_type = "LineString"
    is_empty = False
    is_valid = True
    coords = [(77.0, 28.0)]


def test_measuring_a_single_point_linestring_returns_no_measurement(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`measure_geometry` already swallows any failure, so the result alone proves little: check the guard
    stopped it *before* reprojection and said why, instead of failing inside PROJ and logging a traceback."""
    projected: list[object] = []
    monkeypatch.setattr(measurement, "project_for_measurement", lambda *a, **k: projected.append(a))

    with caplog.at_level("WARNING", logger="app.services.measurement"):
        result = measure_geometry(_OnePointLine(), WGS84)  # type: ignore[arg-type]

    assert result == NO_MEASUREMENT
    assert result.value is None and result.type is None and result.unit is None and result.projected_crs is None
    assert projected == []  # never handed to PROJ
    assert "fewer than 2 points" in caplog.text


def test_bulk_measurement_nulls_a_single_point_line_and_still_measures_its_neighbours() -> None:
    good = LineString([(77, 28), (77.01, 28)])
    results = measure_geometries([good, _OnePointLine(), good, Point(1, 1), box(77, 28, 77.01, 28.01)], WGS84, [0, 1, 2, 3, 4])  # type: ignore[list-item]
    assert results[1] == NO_MEASUREMENT and results[3] == NO_MEASUREMENT
    assert results[0].type == "length" and results[0].value == pytest.approx(results[2].value)
    assert results[4].type == "area"


def test_degenerate_but_constructible_lines_are_unaffected_by_the_guard() -> None:
    """Two identical points and an empty line are legal Shapely geometries; the guard must not change their handling."""
    assert measure_geometry(LineString([(77, 28), (77, 28)]), WGS84).value == 0.0
    assert measure_geometry(LineString(), WGS84) == NO_MEASUREMENT

"""KML layout handling (flat vs foldered, GDAL naming quirks) and unparseable point coordinates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import pytest

from app.services import file_parser
from app.services.file_parser import parse_geospatial_file
from tests.test_files import _kml, _upload
from tests.test_hardening import TWO_FOLDERS

LIMIT = 10**9


def P(name: str, lon: float, lat: float = 28.0) -> str:
    return f"<Placemark><name>{name}</name><Point><coordinates>{lon},{lat}</coordinates></Point></Placemark>"


def BAD(name: str, coords: str, extra: str = "") -> str:
    return f"<Placemark><name>{name}</name>{extra}<Point><coordinates>{coords}</coordinates></Point></Placemark>"


def folder(name: str | None, body: str) -> str:
    return f"<Folder>{'<name>' + name + '</name>' if name else ''}{body}</Folder>"


def parse(tmp_path: Path, body: str) -> file_parser.ParsedFile:
    path = tmp_path / "doc.kml"
    path.write_bytes(_kml(body))
    return parse_geospatial_file(path, ".kml", LIMIT, 100)


def names(parsed: file_parser.ParsedFile) -> list[str]:
    return sorted(f.properties["Name"] for f in parsed.features)


# ------------------------------------------------------------------ flat / foldered structure


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    [
        ("flat, placemarks directly in Document", P("a", 77) + P("b", 78), ["a", "b"]),
        ("single named Folder", folder("F", P("a", 77) + P("b", 78)), ["a", "b"]),
        ("single UNNAMED Folder", folder(None, P("a", 77) + P("b", 78)), ["a", "b"]),
        ("two UNNAMED Folders", folder(None, P("a1", 77)) + folder(None, P("b1", 78) + P("b2", 79)), ["a1", "b1", "b2"]),
        ("loose placemarks + a named Folder", P("top", 70) + folder("F", P("a", 77)), ["a", "top"]),
        ("loose placemarks + two UNNAMED Folders", P("top", 70) + folder(None, P("a", 77)) + folder(None, P("b", 78)), ["a", "b", "top"]),
        ("named + unnamed Folder mix", folder("Named", P("n", 77)) + folder(None, P("u1", 78) + P("u2", 79)), ["n", "u1", "u2"]),
        ("nested Folders", folder("Outer", P("o", 76) + folder("Inner", P("i", 77))), ["i", "o"]),
    ],
)
def test_every_kml_layout_is_read_completely(tmp_path: Path, label: str, body: str, expected: list[str]) -> None:
    parsed = parse(tmp_path, body)
    assert names(parsed) == expected, label
    assert parsed.warnings == []
    assert [f.index for f in parsed.features] == list(range(len(expected)))  # numbered consecutively


def _simulate_gdal_310(monkeypatch: pytest.MonkeyPatch, path: Path, unopenable_positions: set[int]) -> list[Any]:
    """Make the listed names of some layers fail by name with 'Null layer', as GDAL >= 3.10 does.

    Layers at ``unopenable_positions`` are listed under a made-up name that ``read_file`` rejects; the other
    layers keep their real names. Reading by position is passed straight through to the real reader. The
    returned list records every ``layer=`` argument used.
    """
    real_names = fiona_listlayers(path)
    fake = [f"unopenable-{i}" if i in unopenable_positions else name for i, name in enumerate(real_names)]
    real_read_file = gpd.read_file
    used: list[Any] = []

    def read_file(p: Any, **kwargs: Any) -> gpd.GeoDataFrame:
        layer = kwargs.get("layer")
        used.append(layer)
        if isinstance(layer, str) and layer.startswith("unopenable"):
            raise ValueError(f"Null layer: <closed Collection '{p}:{layer}'>")
        return real_read_file(p, **kwargs)

    monkeypatch.setattr(file_parser.gpd, "read_file", read_file)
    monkeypatch.setattr(file_parser.fiona, "listlayers", lambda _p: fake)
    return used


def fiona_listlayers(path: Path) -> list[str]:
    import fiona

    return list(fiona.listlayers(str(path)))


@pytest.mark.parametrize("unopenable", [{0}, {1}, {0, 1}, {1, 2}, {0, 1, 2}], ids=lambda s: "unopenable=" + ",".join(map(str, sorted(s))))
def test_mixed_openable_and_unopenable_layer_names_never_duplicate_or_drop_a_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unopenable: set[int]
) -> None:
    """Regression: reading some layers by name and the rest by position returned one layer twice and lost another."""
    body = P("top", 70) + folder("F", P("a", 77)) + folder(None, P("b", 78))
    path = tmp_path / "doc.kml"
    path.write_bytes(_kml(body))
    used = _simulate_gdal_310(monkeypatch, path, unopenable)

    parsed = parse_geospatial_file(path, ".kml", LIMIT, 100)

    assert names(parsed) == ["a", "b", "top"]  # each placemark exactly once
    assert parsed.warnings == []
    assert used.count(None) >= 1  # position 0 is read with no layer argument
    assert all(layer is None or isinstance(layer, int) for layer in used[-3:])  # the successful pass is purely positional


def test_when_every_name_opens_nothing_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "doc.kml"
    path.write_bytes(_kml(folder("F1", P("a", 77)) + folder("F2", P("b", 78))))
    used = _simulate_gdal_310(monkeypatch, path, set())
    parsed = parse_geospatial_file(path, ".kml", LIMIT, 100)
    assert names(parsed) == ["a", "b"] and all(isinstance(layer, str) for layer in used)  # read by name, as before


def test_a_genuinely_unreadable_layer_still_becomes_a_warning_not_a_positional_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gpd.read_file

    def flaky(path: Any, **kwargs: Any) -> gpd.GeoDataFrame:
        if kwargs.get("layer") == "Bad":
            raise RuntimeError("simulated driver failure")  # not a 'Null layer' ValueError
        return real(path, **kwargs)

    monkeypatch.setattr(file_parser.gpd, "read_file", flaky)
    path = tmp_path / "t.kml"
    path.write_bytes(TWO_FOLDERS)
    parsed = parse_geospatial_file(path, ".kml", LIMIT, 100)
    assert names(parsed) == ["P1"] and len(parsed.warnings) == 1 and "Bad" in parsed.warnings[0]


async def test_flat_kml_completes_through_the_api(client: httpx.AsyncClient) -> None:
    fid = (await _upload(client, "flat.kml", _kml(P("a", 77) + P("b", 78)))).json()["id"]
    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "COMPLETED" and info["feature_count"] == 2 and info["warnings"] == []


# ------------------------------------------------------------- unparseable point coordinates


@pytest.mark.parametrize(
    "coords",
    ["", "   ", "abc,def", "77,abc", "77", "nan,nan", "inf,28", ",28", "77, 28"],
    ids=["empty", "blank", "letters", "half-numeric", "single-number", "nan", "inf", "missing-lon", "space-in-tuple"],
)
def test_unparseable_point_becomes_null_geometry_with_a_warning(tmp_path: Path, coords: str) -> None:
    parsed = parse(tmp_path, folder("F", BAD("bad", coords) + P("good", 77)))
    by_name = {f.properties["Name"]: f for f in parsed.features}

    assert by_name["bad"].geometry is None and by_name["bad"].wkt is None  # not POINT (0 0)
    assert by_name["bad"].geometry_type == "Point"
    assert by_name["good"].wkt == "POINT (77 28)"  # neighbours untouched
    assert parsed.warnings == ["1 feature had empty or unparseable coordinates; the geometry of it was set to null."]


def test_placemark_without_a_coordinates_element_is_nulled(tmp_path: Path) -> None:
    parsed = parse(tmp_path, folder("F", "<Placemark><name>x</name><Point></Point></Placemark>" + P("ok", 77)))
    assert {f.properties["Name"]: f.geometry for f in parsed.features}["x"] is None


def test_warning_counts_every_affected_feature(tmp_path: Path) -> None:
    parsed = parse(tmp_path, folder("F", BAD("a", "") + BAD("b", "abc,def") + BAD("c", "77,abc") + P("ok", 77)))
    assert parsed.warnings == ["3 features had empty or unparseable coordinates; the geometry of each was set to null."]
    assert sum(f.geometry is None for f in parsed.features) == 3


def test_a_genuine_zero_zero_point_is_kept(tmp_path: Path) -> None:
    parsed = parse(tmp_path, folder("F", P("null-island", 0, 0) + BAD("bad", "") + P("good", 77)))
    by_name = {f.properties["Name"]: f for f in parsed.features}
    assert by_name["null-island"].wkt == "POINT (0 0)"  # legitimate, must survive
    assert by_name["bad"].geometry is None
    assert parsed.warnings == ["1 feature had empty or unparseable coordinates; the geometry of it was set to null."]


def test_bad_point_sharing_a_name_with_a_good_one_nulls_the_right_one(tmp_path: Path) -> None:
    """Same (name, description) key: matched by document order, and the zero-coordinate check protects the good one."""
    parsed = parse(tmp_path, folder("F", P("dup", 77) + BAD("dup", "") + P("dup", 78)))
    wkts = [f.wkt for f in parsed.features]
    assert wkts.count(None) == 1 and "POINT (77 28)" in wkts and "POINT (78 28)" in wkts


def test_matching_uses_description_too_and_handles_cdata(tmp_path: Path) -> None:
    body = folder(
        "F",
        BAD("same", "", "<description><![CDATA[<b>broken</b> one]]></description>")
        + "<Placemark><name>same</name><description>fine</description><Point><coordinates>77,28</coordinates></Point></Placemark>",
    )
    parsed = parse(tmp_path, body)
    by_desc = {f.properties["Description"]: f for f in parsed.features}
    assert by_desc["fine"].wkt == "POINT (77 28)"
    assert [f for f in parsed.features if f.geometry is None][0].properties["Description"] != "fine"


def test_bad_altitude_alone_is_not_treated_as_unparseable(tmp_path: Path) -> None:
    parsed = parse(tmp_path, folder("F", BAD("alt", "77,28,abc") + P("ok", 78)))
    assert parsed.warnings == [] and all(f.geometry is not None for f in parsed.features)


def test_scanner_ignores_xml_namespace_prefixes(tmp_path: Path) -> None:
    """GDAL itself cannot open a prefixed KML, so the scanner is checked directly."""
    text = (
        '<?xml version="1.0"?><k:kml xmlns:k="http://www.opengis.net/kml/2.2"><k:Document><k:Folder>'
        "<k:Placemark><k:name>bad</k:name><k:Point><k:coordinates></k:coordinates></k:Point></k:Placemark>"
        "<k:Placemark><k:name>ok</k:name><k:Point><k:coordinates>77,28</k:coordinates></k:Point></k:Placemark>"
        "<k:Placemark><k:name>line</k:name><k:LineString><k:coordinates>1,2 3,4</k:coordinates></k:LineString></k:Placemark>"
        "</k:Folder></k:Document></k:kml>"
    )
    path = tmp_path / "ns.kml"
    path.write_text(text)
    assert file_parser._scan_kml_points(path) == {("bad", ""): [True], ("ok", ""): [False]}  # the LineString is not a Point


def test_scanner_survives_malformed_xml(tmp_path: Path) -> None:
    path = tmp_path / "broken.kml"
    path.write_text("<kml><Document><Placemark><name>x</name><Point>")
    assert file_parser._scan_kml_points(path) == {}


def test_clean_kml_produces_no_warning_and_polygons_are_untouched(tmp_path: Path) -> None:
    poly = "<Placemark><name>p</name><Polygon><outerBoundaryIs><LinearRing><coordinates>77,28 77.1,28 77.1,28.1 77,28.1 77,28</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    parsed = parse(tmp_path, folder("F", poly + P("a", 77)))
    assert parsed.warnings == [] and all(f.geometry is not None for f in parsed.features)


async def test_unparseable_point_end_to_end(client: httpx.AsyncClient) -> None:
    body = folder("F", BAD("bad", "abc,def") + P("good", 77) + BAD("bad2", ""))
    fid = (await _upload(client, "bad-coords.kml", _kml(body))).json()["id"]

    info = (await client.get(f"/api/files/{fid}/")).json()
    assert info["status"] == "COMPLETED" and info["feature_count"] == 3  # features are kept, only geometry is nulled
    assert info["warnings"] == ["2 features had empty or unparseable coordinates; the geometry of each was set to null."]

    items = {i["properties"]["Name"]: i for i in (await client.get(f"/api/files/{fid}/measurements/")).json()["items"]}
    for name in ("bad", "bad2"):
        assert items[name]["geometry"] is None and items[name]["geometry_geojson"] is None
        assert items[name]["measurement"]["value"] is None
    assert items["good"]["geometry"] == "POINT (77 28)" and items["good"]["geometry_geojson"]["coordinates"] == [77.0, 28.0]

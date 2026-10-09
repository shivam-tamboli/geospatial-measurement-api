"""Batched processing (bounded memory, one transaction) and the measurements page-size rules."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.models.models import Feature, FileStatus, UploadedFile
from app.services import file_service
from tests.test_files import _kml, _upload


def _placemarks(n: int) -> str:
    """n features cycling polygon / line / point, spread over several UTM zones."""
    out = []
    for i in range(n):
        lon, lat = 10 + (i % 7) * 6.5, 40 + (i % 3)
        if i % 3 == 0:
            out.append(f"<Placemark><name>f{i}</name><Polygon><outerBoundaryIs><LinearRing><coordinates>{lon},{lat} {lon+.01},{lat} {lon+.01},{lat+.01} {lon},{lat+.01} {lon},{lat}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>")
        elif i % 3 == 1:
            out.append(f"<Placemark><name>f{i}</name><LineString><coordinates>{lon},{lat} {lon+.02},{lat}</coordinates></LineString></Placemark>")
        else:
            out.append(f"<Placemark><name>f{i}</name><Point><coordinates>{lon},{lat}</coordinates></Point></Placemark>")
    return "<Folder><name>F</name>" + "".join(out) + "</Folder>"


async def _stored_file(tmp_path: Path, n: int) -> uuid.UUID:
    """Insert a PENDING file record for a KML on disk, so `process_file` can be driven with custom settings."""
    path = tmp_path / f"{uuid.uuid4()}.kml"
    path.write_bytes(_kml(_placemarks(n)))
    fid = uuid.uuid4()
    async with async_session_factory() as s:
        s.add(UploadedFile(id=fid, filename="batch.kml", stored_path=str(path), file_size=path.stat().st_size, status=FileStatus.PENDING))
        await s.commit()
    return fid


async def _rows(fid: uuid.UUID) -> list[dict[str, Any]]:
    async with async_session_factory() as s:
        feats = (await s.execute(select(Feature).where(Feature.file_id == fid).order_by(Feature.feature_index))).scalars().all()
        return [
            {c.name: getattr(f, c.name) for c in Feature.__table__.columns if c.name not in ("id", "file_id")} for f in feats
        ]


def _settings(batch: int):
    return dataclasses.replace(get_settings(), processing_batch_size=batch)


# ---------------------------------------------------------------------------------- batching


def _code_defaults(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import _load_settings

    for name in ("DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "PROCESSING_BATCH_SIZE"):
        monkeypatch.delenv(name, raising=False)
    return _load_settings()


def test_batch_size_defaults_to_50(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _code_defaults(monkeypatch).processing_batch_size == 50


def test_batch_size_of_zero_or_less_is_clamped_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import _load_settings

    monkeypatch.setenv("PROCESSING_BATCH_SIZE", "0")
    assert _load_settings().processing_batch_size == 1  # 0 would make the loop spin forever


async def test_features_are_processed_in_bounded_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real = file_service._build_feature_rows

    def spy(file_id: uuid.UUID, features: list[Any]) -> list[dict[str, Any]]:
        sizes.append(len(features))
        return real(file_id, features)

    monkeypatch.setattr(file_service, "_build_feature_rows", spy)
    fid = await _stored_file(tmp_path, 23)
    await file_service.process_file(fid, settings=_settings(5))

    assert sizes == [5, 5, 5, 5, 3]  # never more than the batch size, remainder last
    async with async_session_factory() as s:
        rec = await s.get(UploadedFile, fid)
        assert rec.status == FileStatus.COMPLETED and rec.feature_count == 23
        assert await s.scalar(select(func.count()).select_from(Feature).where(Feature.file_id == fid)) == 23


@pytest.mark.parametrize("batch", [1, 2, 7, 1000])
async def test_results_do_not_depend_on_the_batch_size(tmp_path: Path, batch: int) -> None:
    """Batching changes memory use only: every stored value must match a single all-at-once run."""
    reference_id, batched_id = await _stored_file(tmp_path, 30), await _stored_file(tmp_path, 30)
    await file_service.process_file(reference_id, settings=_settings(10_000))
    await file_service.process_file(batched_id, settings=_settings(batch))

    reference, batched = await _rows(reference_id), await _rows(batched_id)
    assert len(batched) == 30 and [r["feature_index"] for r in batched] == list(range(30))
    assert batched == reference  # geometry, GeoJSON, measurement value/unit/zone, properties: all identical


async def test_failure_part_way_leaves_no_partial_features(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Batches are inserted as they are built, but in one transaction: a later failure must roll all of them back."""
    real = file_service._build_feature_rows
    calls = 0

    def explode_on_third(file_id: uuid.UUID, features: list[Any]) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("boom")
        return real(file_id, features)

    monkeypatch.setattr(file_service, "_build_feature_rows", explode_on_third)
    fid = await _stored_file(tmp_path, 20)
    await file_service.process_file(fid, settings=_settings(5))  # must not raise

    assert calls == 3  # two batches had already been inserted when it failed
    async with async_session_factory() as s:
        rec = await s.get(UploadedFile, fid)
        assert rec.status == FileStatus.FAILED and rec.feature_count is None and rec.error_message
        assert await s.scalar(select(func.count()).select_from(Feature).where(Feature.file_id == fid)) == 0


async def test_a_file_that_fits_in_one_batch_is_one_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real = file_service._build_feature_rows
    monkeypatch.setattr(file_service, "_build_feature_rows", lambda fid, feats: (sizes.append(len(feats)), real(fid, feats))[1])
    await file_service.process_file(await _stored_file(tmp_path, 4), settings=_settings(50))
    assert sizes == [4]


# ----------------------------------------------------------------------------------- paging


async def _completed(client: httpx.AsyncClient, n: int) -> str:
    fid = (await _upload(client, "paging.kml", _kml(_placemarks(n)))).json()["id"]
    return fid


def test_page_size_limits_default_to_20_and_100(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _code_defaults(monkeypatch)
    assert (s.default_page_size, s.max_page_size) == (20, 100)


async def test_measurements_default_page_size_is_20(client: httpx.AsyncClient) -> None:
    fid = await _completed(client, 45)
    page = (await client.get(f"/api/files/{fid}/measurements/")).json()
    assert page["page_size"] == 20 and len(page["items"]) == 20
    assert (page["total"], page["total_pages"]) == (45, 3)
    last = (await client.get(f"/api/files/{fid}/measurements/", params={"page": 3})).json()
    assert len(last["items"]) == 5 and last["items"][0]["feature_id"] == 40


async def test_page_size_100_is_accepted(client: httpx.AsyncClient) -> None:
    fid = await _completed(client, 120)
    resp = await client.get(f"/api/files/{fid}/measurements/", params={"page_size": 100})
    assert resp.status_code == 200 and len(resp.json()["items"]) == 100 and resp.json()["total_pages"] == 2


@pytest.mark.parametrize("too_big", [101, 200, 500, 501, 10_000])
async def test_page_size_above_100_is_rejected_with_400_not_capped(client: httpx.AsyncClient, too_big: int) -> None:
    fid = await _completed(client, 5)
    resp = await client.get(f"/api/files/{fid}/measurements/", params={"page_size": too_big})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "page_size must be at most 100.", "code": "PAGE_SIZE_TOO_LARGE"}


async def test_the_size_check_comes_before_the_file_lookup(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/api/files/{uuid.uuid4()}/measurements/", params={"page_size": 101})
    assert resp.status_code == 400 and resp.json()["code"] == "PAGE_SIZE_TOO_LARGE"


@pytest.mark.parametrize("bad", [0, -1])
async def test_page_size_below_one_is_still_a_validation_error(client: httpx.AsyncClient, bad: int) -> None:
    fid = await _completed(client, 3)
    resp = await client.get(f"/api/files/{fid}/measurements/", params={"page_size": bad})
    assert resp.status_code == 422 and resp.json()["code"] == "VALIDATION_ERROR"


async def test_response_format_is_unchanged(client: httpx.AsyncClient) -> None:
    fid = await _completed(client, 3)
    page = (await client.get(f"/api/files/{fid}/measurements/")).json()
    assert set(page) == {"file_id", "page", "page_size", "total", "total_pages", "items"}
    assert set(page["items"][0]) == {"feature_id", "geometry_type", "geometry", "geometry_geojson", "crs", "properties", "measurement"}

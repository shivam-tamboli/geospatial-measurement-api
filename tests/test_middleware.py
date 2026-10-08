"""Tests for the Content-Length guard (MaxBodySizeMiddleware)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.middleware import MULTIPART_OVERHEAD_BYTES, MaxBodySizeMiddleware

MAX_BYTES = 1000
LIMIT = MAX_BYTES + MULTIPART_OVERHEAD_BYTES


class _Recorder:
    """Stub downstream app that records whether it was reached."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _forbidden_receive() -> Message:
    raise AssertionError("the request body must not be read when Content-Length is over the limit")


async def _call(app: MaxBodySizeMiddleware, headers: list[tuple[bytes, bytes]], scope_type: str = "http") -> list[Message]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: dict[str, Any] = {"type": scope_type, "method": "POST", "path": "/api/files/", "headers": headers}
    await app(scope, _forbidden_receive, send)
    return sent


def _body(sent: list[Message]) -> dict[str, str]:
    return json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


async def test_declared_oversize_is_rejected_without_reading_the_body() -> None:
    inner = _Recorder()
    sent = await _call(MaxBodySizeMiddleware(inner, MAX_BYTES), [(b"content-length", str(LIMIT + 1).encode())])

    assert sent[0]["status"] == 413
    assert _body(sent) == {"detail": "File too large", "code": "FILE_TOO_LARGE"}
    assert inner.called is False  # and _forbidden_receive was never awaited, or the test would have failed


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", str(LIMIT).encode())],  # exactly at the limit (limit + multipart allowance)
        [(b"content-length", b"0")],
        [],  # chunked transfer: no Content-Length, the service-level check is the fallback
        [(b"transfer-encoding", b"chunked")],
        [(b"content-length", b"not-a-number")],  # malformed: not this middleware's job to police
        [(b"content-length", b"-5")],
    ],
)
async def test_requests_that_are_not_provably_oversized_pass_through(headers: list[tuple[bytes, bytes]]) -> None:
    inner = _Recorder()
    sent = await _call(MaxBodySizeMiddleware(inner, MAX_BYTES), headers)
    assert inner.called is True and sent[0]["status"] == 204


async def test_non_http_scopes_are_ignored() -> None:
    inner = _Recorder()
    await _call(MaxBodySizeMiddleware(inner, MAX_BYTES), [(b"content-length", b"999999999")], scope_type="websocket")
    assert inner.called is True


# ---------------------------------------------------------------- through the real application


def _upload_dir_files() -> list[str]:
    directory = get_settings().upload_dir
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


async def test_oversized_content_length_gets_413_before_anything_is_stored(client: httpx.AsyncClient) -> None:
    before = _upload_dir_files()
    # The body is empty on purpose: only the *declared* length matters, and the guard must not need the body.
    resp = await client.post(
        "/api/files/",
        headers={"content-length": str(50 * 1024 * 1024), "content-type": "multipart/form-data; boundary=x"},
        content=b"",
    )
    assert resp.status_code == 413
    assert resp.json() == {"detail": "File too large", "code": "FILE_TOO_LARGE"}
    assert _upload_dir_files() == before
    assert (await client.get("/api/files/")).json()["total"] == 0


async def test_413_carries_cors_headers_so_browsers_can_read_it(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/files/",
        headers={
            "origin": "http://localhost:5173",
            "content-length": str(50 * 1024 * 1024),
            "content-type": "multipart/form-data; boundary=x",
        },
        content=b"",
    )
    assert resp.status_code == 413
    assert resp.headers["access-control-allow-origin"] == "*"


async def test_chunked_upload_without_content_length_falls_back_to_the_service_limit(client: httpx.AsyncClient) -> None:
    """No Content-Length (chunked): the middleware lets it through; the streaming check still rejects it."""
    boundary = "testboundary"
    payload = b"<kml>" + b"x" * (2 * 1024 * 1024)  # tests run with MAX_UPLOAD_SIZE_MB=1

    async def chunks() -> AsyncIterator[bytes]:
        yield f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.kml"\r\n\r\n'.encode()
        yield payload
        yield f"\r\n--{boundary}--\r\n".encode()

    before = _upload_dir_files()
    request = client.build_request(
        "POST", "/api/files/", content=chunks(), headers={"content-type": f"multipart/form-data; boundary={boundary}"}
    )
    assert "content-length" not in request.headers  # confirms this really exercises the fallback path
    resp = await client.send(request)

    assert resp.status_code == 413 and resp.json()["code"] == "FILE_TOO_LARGE"
    assert _upload_dir_files() == before  # partial file cleaned up


async def test_upload_just_under_the_limit_is_still_accepted(client: httpx.AsyncClient) -> None:
    """The multipart allowance means a file near the limit is not rejected by the header check."""
    kml = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><Point><coordinates>77,28</coordinates></Point></Placemark>'
    kml += b"<!--" + b"x" * (1024 * 1024 - len(kml) - 40) + b"--></Document></kml>"
    assert len(kml) < 1024 * 1024
    resp = await client.post("/api/files/", files={"file": ("near-limit.kml", kml)})
    assert resp.status_code == 202

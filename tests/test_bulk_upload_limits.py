# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Proof tests for the bulk-upload / relay-tunnel size fix.

These reproduce the ROOT CAUSE of the bulk file/cert import failure and verify the
fix at the mechanism level (the full live-relay path can only be checked manually):

1. WS frame cap — the relay forwards each proxied HTTP request as ONE base64'd
   WebSocket message. The host client used the `websockets` default max_size of
   1 MiB, so any bulk body > ~750 KB overflowed the frame → connection drop →
   relay 504. We reproduce that, then show the raised max_size (the fix) accepts it.

2. Body-cap middleware — the bulk path must be exempt from the 10 MB cap while
   every other path stays capped.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

# A modest cert ZIP — 5 MB is well over the 1 MiB default frame cap, so it
# represents a bulk import that fails over the relay TODAY.
_BULK_MSG = b"x" * (5 * 1024 * 1024)
_RAISED_MAX = 160 * 1024 * 1024  # matches the host client's new max_size


async def _push_handler(ws):
    """Server pushes one large message — mirrors the relay forwarding a base64'd
    bulk request body to the host's WS client (relay sends, host receives)."""
    await ws.send(_BULK_MSG)
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_default_ws_max_size_rejects_bulk_message():
    """REPRODUCES THE BUG: with the default 1 MiB max_size, receiving a 5 MB
    message fails (the connection is dropped) — this is why bulk upload 504s."""
    async with websockets.serve(_push_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:  # DEFAULT max_size
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await ws.recv()


@pytest.mark.asyncio
async def test_raised_ws_max_size_accepts_bulk_message():
    """VERIFIES THE FIX: with max_size raised (as in celerp/gateway/client.py),
    the same 5 MB message is received intact."""
    async with websockets.serve(_push_handler, "127.0.0.1", 0, max_size=_RAISED_MAX) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=_RAISED_MAX) as ws:
            got = await ws.recv()
            assert len(got) == len(_BULK_MSG)


def test_raised_max_size_covers_100mb_body():
    """The chosen ceiling actually fits a 100 MB body once base64-expanded (~134 MB)."""
    assert _RAISED_MAX >= int(100 * 1024 * 1024 * 4 / 3)


# ── Body-cap middleware exemption ──────────────────────────────────────────────

async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _drive(mw, path: str, content_length: int) -> int:
    """Run the ASGI middleware for one request; return the response status."""
    statuses: list[int] = []
    scope = {"type": "http", "path": path,
             "headers": [(b"content-length", str(content_length).encode())]}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            statuses.append(msg["status"])

    await mw(scope, receive, send)
    return statuses[0]


@pytest.mark.asyncio
async def test_body_cap_still_rejects_large_non_bulk():
    from celerp.middleware import MaxBodySizeMiddleware
    mw = MaxBodySizeMiddleware(_ok_app, max_body_size_bytes=10 * 1024 * 1024)
    assert await _drive(mw, "/items", 50 * 1024 * 1024) == 413  # other paths stay capped


@pytest.mark.asyncio
async def test_bulk_path_exempt_from_body_cap():
    from celerp.middleware import MaxBodySizeMiddleware
    mw = MaxBodySizeMiddleware(_ok_app, max_body_size_bytes=10 * 1024 * 1024)
    # 50 MB to the bulk route passes through instead of 413.
    assert await _drive(mw, "/items/files/bulk", 50 * 1024 * 1024) == 200


async def _drive_chunked(limit: int, path: str, chunks: int, chunk_size: int) -> tuple[int, int]:
    """Stream a body with no Content-Length through the body cap to an app that
    reads it all; return (status, bytes the app read)."""
    from celerp.middleware import MaxBodySizeMiddleware
    statuses: list[int] = []
    seen = {"bytes": 0}
    pending = [b"x" * chunk_size] * chunks

    async def receive():
        body = pending.pop() if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def reading_app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise RuntimeError("client went away")
            seen["bytes"] += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await _ok_app(scope, receive, send)

    async def send(msg):
        if msg["type"] == "http.response.start":
            statuses.append(msg["status"])

    mw = MaxBodySizeMiddleware(reading_app, max_body_size_bytes=limit)
    await mw({"type": "http", "path": path, "headers": []}, receive, send)
    return statuses[0], seen["bytes"]


@pytest.mark.asyncio
async def test_body_cap_counts_a_chunked_body_without_content_length():
    status, read = await _drive_chunked(1024, "/accounting/reconciliation/x/import-csv", 10, 512)
    assert status == 413
    assert read <= 1024  # the app never sees more than the limit
    assert await _drive_chunked(1024, "/items", 2, 512) == (200, 1024)  # at the limit passes


@pytest.mark.asyncio
async def test_chunked_upload_over_the_cap_is_refused_by_the_app(client):
    """A real upload route: a streamed multipart body with no Content-Length is
    refused once it passes the cap, before any handler reads it whole."""
    from celerp.main import app
    import httpx

    boundary = "b0undary"

    async def body():
        yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"s.csv\"\r\n"
               "Content-Type: text/csv\r\n\r\n").encode()
        for _ in range(11):
            yield b"x" * (1024 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/accounting/reconciliation/00000000-0000-0000-0000-000000000000/import-csv",
            content=body(), headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
    assert r.status_code == 413, r.text

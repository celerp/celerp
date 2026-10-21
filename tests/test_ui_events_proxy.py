# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The page's live-events stream closes its connection to the API when the page goes
away, whether it leaves while an event is on its way or while the stream is connecting,
instead of leaving the connection open until garbage collection."""
from __future__ import annotations

import asyncio
import gc
import warnings

import pytest
from anyio._backends._asyncio import AsyncIOBackend
from fasthtml.common import FastHTML

from test_helpers import make_test_token
from ui.routes import events


@pytest.mark.parametrize("leaves", ["while_an_event_is_sent", "while_connecting"])
async def test_leaving_the_page_closes_its_connection_to_the_api(monkeypatch, leaves):
    upstream_closed = asyncio.Event()

    async def api(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                     b"transfer-encoding: chunked\r\n\r\na\r\ndata: hi\n\n\r\n")
        await writer.drain()
        await reader.read()
        upstream_closed.set()
        writer.close()

    server = await asyncio.start_server(api, "127.0.0.1", 0)
    monkeypatch.setattr("ui.config.API_BASE", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    connected, left = asyncio.Event(), asyncio.Event()
    if leaves == "while_connecting":
        connect = AsyncIOBackend.connect_tcp.__func__

        async def slow_to_finish_connecting(cls, *args, **kwargs):
            stream = await connect(cls, *args, **kwargs)
            connected.set()
            await asyncio.sleep(0.5)
            return stream

        monkeypatch.setattr(AsyncIOBackend, "connect_tcp", classmethod(slow_to_finish_connecting))
    app = FastHTML()
    events.setup_routes(app)
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if b"data: hi" in message.get("body", b""):
            connected.set()
            # The browser leaves while this chunk is still being written to it.
            await asyncio.sleep(3600)

    # The server's own ASGI spec version, under which Starlette ends the stream by
    # cancelling it when the browser leaves.
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/events/stream", "raw_path": b"/events/stream",
             "query_string": b"", "root_path": "", "server": ("ui", 80), "client": ("127.0.0.1", 1),
             "headers": [(b"host", b"ui"), (b"cookie", f"celerp_token={make_test_token()}".encode())]}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        stream = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(connected.wait(), 5)
        left.set()
        await asyncio.wait_for(stream, 5)
        await asyncio.wait_for(upstream_closed.wait(), 5)
        del stream
        gc.collect()
    server.close()
    await server.wait_closed()

    assert not [w for w in caught if issubclass(w.category, ResourceWarning)], [str(w.message) for w in caught]

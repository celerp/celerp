# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The docs and lists CSV export client methods must stream, not buffer.

Prod incident context (#319): the UI client read each export fully into memory with
`.content` and the UI route re-sent that buffer, so a large export sat twice in the UI
process and pinned a pooled connection for the whole read - a contributor to the
connection-pool pressure the stability work targets. The backend already streams these
exports row by row; the UI must hand the chunks straight through.

These assert the OBSERVABLE streaming behaviour of the client method: it returns a
(chunk_iterator, headers) pair, the body is NOT read until the caller iterates, and the
streamed bytes match the backend body intact - never a symbol-existence or type-name
surface check."""
from __future__ import annotations

import httpx
import pytest

import ui.api_client as api


def _mock_transport(expected_path, chunks, started):
    async def _body():
        started["read"] = True
        for ch in chunks:
            yield ch

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path, request.url.path
        return httpx.Response(
            200,
            headers={
                "content-type": "text/csv",
                "content-length": str(sum(len(c) for c in chunks)),
            },
            content=_body(),
        )

    return httpx.MockTransport(handler)


async def _assert_streams_lazily(monkeypatch, export_coro, expected_path):
    chunks = [b"col_a,col_b\n"] + [b"row-%d,value\n" % i for i in range(500)]
    total = sum(len(c) for c in chunks)
    started = {"read": False}
    # These exports stream on the bulk transport (a large export must never hold an
    # interactive slot), so the mock stands in for the bulk pool the helper selects.
    monkeypatch.setattr(
        api, "_get_bulk_transport", lambda: _mock_transport(expected_path, chunks, started))

    result = await export_coro()
    # A buffered export reads the whole CSV into a bytes object; a streaming one returns
    # (iterator, headers). The buffered shape is the exact regression under test.
    assert not isinstance(result, (bytes, bytearray)), (
        "the export was buffered into bytes; it must stream a (chunk_iterator, headers) pair")
    stream, headers = result
    assert started["read"] is False, (
        "the export body must not be read into UI memory before the caller iterates; "
        "a buffered export drains the whole response first")
    body = b"".join([chunk async for chunk in stream])
    assert started["read"] is True, "iterating the stream must actually pull the backend body"
    assert body == b"".join(chunks), "the streamed bytes must match the backend export intact"
    assert headers.get("content-length") == str(total), (
        "Content-Length must be forwarded so the browser shows real download progress")


@pytest.mark.asyncio
async def test_export_docs_csv_streams_and_does_not_buffer(monkeypatch):
    await _assert_streams_lazily(
        monkeypatch, lambda: api.export_docs_csv("tok", {}), "/docs/export/csv")


@pytest.mark.asyncio
async def test_export_lists_csv_streams_and_does_not_buffer(monkeypatch):
    await _assert_streams_lazily(
        monkeypatch, lambda: api.export_lists_csv("tok", {}), "/lists/export/csv")


@pytest.mark.asyncio
async def test_backend_exports_use_one_streamed_statement():
    from types import SimpleNamespace
    import importlib
    routes = importlib.import_module("celerp_docs.routes")

    class Result:
        def __init__(self, rows):
            self.rows = rows
            self.closed = False
        def __aiter__(self):
            async def gen():
                for row in self.rows:
                    yield row
            return gen()
        async def close(self):
            self.closed = True

    class Session:
        def __init__(self):
            self.stream_calls = 0
            self.execute_calls = 0
            self.results = []
        async def stream(self, stmt):
            self.stream_calls += 1
            keys = list(stmt.selected_columns.keys())
            row = tuple(["doc:1"] + [None] * (len(keys) - 1)) if "entity_id" in keys else SimpleNamespace(**{k: None for k in keys})
            result = Result([row])
            self.results.append(result)
            return result
        async def execute(self, stmt):
            self.execute_calls += 1
            raise AssertionError("export must use one streamed statement")

    s = Session()
    response = await routes.export_docs_csv(filters=routes.DocListFilters(), cols=None, company_id="00000000-0000-0000-0000-000000000001", session=s)
    _ = [chunk async for chunk in response.body_iterator]
    assert s.stream_calls == 1 and s.execute_calls == 0 and s.results[0].closed

    s = Session()
    response = await routes.export_lists_csv(filters=routes.ListIndexFilters(), cols=None, company_id="00000000-0000-0000-0000-000000000001", session=s)
    _ = [chunk async for chunk in response.body_iterator]
    assert s.stream_calls == 1 and s.execute_calls == 0 and s.results[0].closed


class _Upstream(httpx.AsyncByteStream):
    """An export body that records whether it was closed."""

    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b"col_a,col_b\n"

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_leaving_before_the_first_chunk_closes_the_export_upstream(monkeypatch, spec_version):
    """The browser leaves before any of the export reaches it: the response the export
    opened to the API is still closed, so it does not hold one of the two bulk slots."""
    from starlette.requests import ClientDisconnect

    from ui.streaming import ClosingStreamingResponse

    upstream = _Upstream()
    monkeypatch.setattr(api, "_get_bulk_transport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/csv"}, stream=upstream)))
    stream, _headers = await api.export_docs_csv("tok", {})

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise OSError("the browser left")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}}
    try:
        await ClosingStreamingResponse(stream, media_type="text/csv")(scope, receive, send)
    except (ClientDisconnect, OSError):
        pass
    assert upstream.closed

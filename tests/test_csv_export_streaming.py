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

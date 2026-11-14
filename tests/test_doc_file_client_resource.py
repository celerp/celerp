# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The UI's document file calls reach the owning resource: a List's files go to /lists, a
document's to /docs, and each call hands back what the API answered."""
from contextlib import asynccontextmanager

import httpx
import pytest

import ui.api_client as api


@pytest.fixture
def seen(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=b"%PDF-1.0", headers={"content-type": "application/pdf"})
        return httpx.Response(200, json={"path": request.url.path, "method": request.method})

    @asynccontextmanager
    async def client(token, timeout=10.0):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://api") as c:
            yield c

    monkeypatch.setattr(api, "_api_client", client)
    monkeypatch.setattr(api, "_bulk_api_client", client)
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_each_file_call_goes_to_its_resource_and_returns_the_reply(seen, resource):
    up = await api.upload_doc_file("t", "e1", b"%PDF-1.0", "a.pdf", "application/pdf", "note", "receipt", resource=resource)
    tag = await api.tag_doc_file("t", "e1", "f1", "receipt", resource=resource)
    desc = await api.patch_doc_file_description("t", "e1", "f1", "note", resource=resource)
    gone = await api.delete_doc_file("t", "e1", "f1", resource=resource)
    got = await api.download_doc_file("t", "e1", "f1", resource=resource)
    base = f"/{resource}/e1/files"
    assert up == {"path": base, "method": "POST"}
    assert tag == {"path": f"{base}/f1/tag", "method": "PATCH"}
    assert desc == {"path": f"{base}/f1/description", "method": "PATCH"}
    assert gone == {"path": f"{base}/f1", "method": "DELETE"}
    assert got.content == b"%PDF-1.0"
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", base), ("PATCH", f"{base}/f1/tag"), ("PATCH", f"{base}/f1/description"),
        ("DELETE", f"{base}/f1"), ("GET", f"{base}/f1")]
    assert b'name="document_tag"\r\n\r\nreceipt' in seen[0].content
    assert seen[1].content == b"document_tag=receipt" and seen[2].content == b"description=note"


@pytest.mark.asyncio
async def test_a_refused_file_call_raises_the_api_error(monkeypatch):
    @asynccontextmanager
    async def client(token, timeout=10.0):
        transport = httpx.MockTransport(lambda r: httpx.Response(403, json={"detail": "no"}))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
            yield c

    monkeypatch.setattr(api, "_api_client", client)
    with pytest.raises(api.APIError):
        await api.delete_doc_file("t", "e1", "f1", resource="lists")

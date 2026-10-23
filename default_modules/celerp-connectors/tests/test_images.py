# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for connector image/file helpers (celerp/connectors/images.py)."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from celerp.connectors.images import (
    _is_image_mime,
    build_platform_cert_payload,
    build_platform_image_payload,
    download_and_emit_file,
)


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_is_image_mime():
    assert _is_image_mime("image/png")
    assert not _is_image_mime("application/pdf")


def test_build_image_payload_hero_and_additional():
    files = [
        {"url": "h", "is_hero": True, "mime": "image/png", "document_tag": "product_images"},
        {"url": "a", "is_hero": False, "mime": "image/jpeg", "document_tag": "product_images"},
        {"url": "doc", "is_hero": False, "mime": "application/pdf", "document_tag": "product_images"},
    ]
    p = build_platform_image_payload(files)
    assert p["hero_url"] == "h"
    assert p["additional_urls"] == ["a"]  # pdf excluded (not image)


def test_build_cert_payload():
    files = [
        {"url": "c", "filename": "cert.pdf", "document_tag": "certificates"},
        {"url": "i", "filename": "x.jpg", "document_tag": "product_images"},
    ]
    p = build_platform_cert_payload(files)
    assert p == {"certificates": [{"name": "cert.pdf", "url": "c"}]}


# ── download_and_emit_file ───────────────────────────────────────────────────

def _proj(files):
    p = MagicMock()
    p.state = {"files": files}
    return p


def _sess(projection):
    s = MagicMock()
    s.get = AsyncMock(return_value=projection)
    return s


_ARGS = ("co", "item:1", "https://x.test/img.jpg", "f.jpg", "product_images", True)
_FROM = {"platform": "woocommerce", "store": "https://x.test", "remote_id": 5}


@pytest.fixture(autouse=True)
def _public_url_validation():
    with patch(
        "celerp.services.outbound_url.validate_public_base_url",
        new=AsyncMock(side_effect=lambda url, **_: url),
    ):
        yield


@pytest.mark.asyncio
async def test_download_emit_no_projection_returns_false():
    assert await download_and_emit_file(_sess(None), *_ARGS, **_FROM) is None


@pytest.mark.asyncio
async def test_download_emit_already_stored_returns_false():
    sess = _sess(_proj([{"url": "/static/x.jpg", "remote": {"woocommerce": {
        "id": "5", "src": "https://x.test/old.jpg", "store": "https://x.test"}}}]))
    assert await download_and_emit_file(sess, *_ARGS, **_FROM) is None


@pytest.mark.asyncio
async def test_download_emit_download_failure_returns_false():
    sess = _sess(_proj([]))
    with respx.mock:
        respx.get("https://x.test/img.jpg").mock(return_value=httpx.Response(500))
        assert await download_and_emit_file(sess, *_ARGS, **_FROM) is None


@pytest.mark.asyncio
async def test_download_emit_store_failure_returns_false():
    sess = _sess(_proj([]))
    with respx.mock, \
         patch("celerp.services.attachments.store_upload", new=AsyncMock(side_effect=RuntimeError("disk full"))):
        respx.get("https://x.test/img.jpg").mock(
            return_value=httpx.Response(200, content=b"abc", headers={"content-type": "image/jpeg"}))
        assert await download_and_emit_file(sess, *_ARGS, **_FROM) is None


@pytest.mark.asyncio
async def test_download_emit_success_emits_event():
    sess = _sess(_proj([]))
    meta = {"id": "f1", "filename": "f.jpg", "mime": "image/jpeg", "size": 3, "url": "/files/f1"}
    emit = AsyncMock()
    with respx.mock, \
         patch("celerp.services.attachments.store_upload", new=AsyncMock(return_value=meta)), \
         patch("celerp.services.attachments.emit_event", new=emit):
        respx.get("https://x.test/img.jpg").mock(
            return_value=httpx.Response(200, content=b"abc", headers={"content-type": "image/jpeg"}))
        result = await download_and_emit_file(sess, *_ARGS, **_FROM)
    assert result.startswith("connector-file:woocommerce:item:1:")
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["event_type"] == "item.file.attached"
    assert kwargs["data"]["file_id"] == "f1"
    assert kwargs["data"]["is_hero"] is True
    assert kwargs["data"]["remote"] == {"woocommerce": {
        "id": "5", "src": "https://x.test/img.jpg", "store": "https://x.test"}}
    assert kwargs["actor_id"] is None


@pytest.mark.asyncio
async def test_download_emit_guesses_mime_when_header_missing():
    """No content-type header → fall back to mimetypes.guess_type from filename."""
    sess = _sess(_proj([]))
    meta = {"id": "f2", "filename": "f.jpg", "mime": "image/jpeg", "size": 3, "url": "/x"}
    captured = {}

    async def _store(company_id, upload):
        captured["ct"] = upload.headers.get("content-type")
        return meta

    with respx.mock, \
         patch("celerp.services.attachments.store_upload", new=_store), \
         patch("celerp.services.attachments.emit_event", new=AsyncMock()):
        respx.get("https://x.test/img.jpg").mock(return_value=httpx.Response(200, content=b"abc"))  # no content-type
        result = await download_and_emit_file(sess, *_ARGS, **_FROM)
    assert result
    assert captured["ct"] == "image/jpeg"  # guessed from f.jpg


# ── inbound pulls dedupe by the store's image id ─────────────────────────────

from connector_test_support import add_item, connector_session, item_state, seed_company  # noqa: E402,F401


@pytest.mark.asyncio
async def test_inbound_image_pull_is_idempotent(connector_session):
    import itertools

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.woocommerce import WooCommerceConnector

    session = connector_session
    cid = await seed_company(session, "ImagePull")
    await add_item(session, cid, "item:img", "IMG-1")
    ctx = ConnectorContext(company_id=str(cid), access_token="k:s", store_handle="https://store.example.com")
    product = {"images": [
        {"id": 11, "src": "https://cdn.example.com/a.jpg", "name": "a"},
        {"id": 12, "src": "https://cdn.example.com/b.jpg", "name": "b"},
    ], "meta_data": []}
    ids = itertools.count(1)

    async def store(company_id, upload):
        n = next(ids)
        return {"id": f"f{n}", "filename": upload.filename, "mime": "image/jpeg", "size": 3,
                "url": f"/static/attachments/{company_id}/f{n}.jpg"}

    with respx.mock, patch("celerp.services.attachments.store_upload", new=store):
        respx.get(url__regex=r"https://cdn\.example\.com/.*").mock(
            return_value=httpx.Response(200, content=b"abc", headers={"content-type": "image/jpeg"}))
        await WooCommerceConnector()._pull_product_files(ctx, product, "item:img")
        await WooCommerceConnector()._pull_product_files(ctx, product, "item:img")

    files = (await item_state(session, cid, "item:img"))["files"]
    assert len(files) == 2
    assert [f["remote"]["woocommerce"]["id"] for f in files] == ["11", "12"]

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
    download_and_emit_file,
)


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_is_image_mime():
    assert _is_image_mime("image/png")
    assert not _is_image_mime("application/pdf")


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


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_outbound_never_sends_relative_image_src(connector_session, monkeypatch):
    """A Celerp image goes to the store through a short-lived public link; once the
    store returns its own id for it, later pushes send that id. Without a link nothing
    relative is ever sent: the image waits on a person."""
    import json

    monkeypatch.setattr("celerp.services.attachments.company_file_size", AsyncMock(return_value=1024))

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp.services.field_fingerprints import fingerprint
    from celerp.connectors.product_links import external_link_for_state
    from connector_test_support import connect

    session = connector_session
    cid = await seed_company(session, "MintedImage")
    files = [{"id": "f9", "filename": "hero.png", "mime": "image/png", "is_hero": True,
              "document_tag": "product_images", "url": f"/static/attachments/{cid}/f9.png"}]
    values = {"sku": "M-1", "name": "Mint", "description": "", "sale_price": 1.0}
    await add_item(session, cid, "item:m", "M-1", name="Mint", description="", sale_price=1.0, files=files,
                   external_links={"woocommerce": {
                       "product_id": "605", "sync_enabled": True, "remote_deleted": False,
                       "field_fingerprints": {k: fingerprint(v) for k, v in values.items()}}})
    await connect(session, cid, "woocommerce", {"products": "outbound", "stock": "off", "orders": "off",
                                                "customers": "off"})
    ctx = ConnectorContext(company_id=str(cid), access_token="ck:cs", store_handle="https://store.example.com")

    with respx.mock:
        put = respx.put("https://store.example.com/wp-json/wc/v3/products/605").mock(
            return_value=httpx.Response(200, json={"id": 605, "images": [{"id": 777}]}))
        with patch("celerp.services.relay_share.mint_media_url", new=AsyncMock(return_value=None)):
            unshared = await WooCommerceConnector().sync_products_out(ctx)
        assert not put.called
        assert [e["kind"] for e in unshared.attention] == ["image"]

        with patch("celerp.services.relay_share.mint_media_url",
                   new=AsyncMock(return_value="https://share.example.com/m/tok/f9.png")):
            await WooCommerceConnector().sync_products_out(ctx)
        sent = json.loads(put.calls.last.request.content)
        assert sent == {"images": [{"src": "https://share.example.com/m/tok/f9.png"}]}
        link = external_link_for_state(await item_state(session, cid, "item:m"), "woocommerce")
        assert link["media_ids"] == {"f9": "777"}
    for call in put.calls:
        for image in json.loads(call.request.content).get("images", []):
            assert not str(image.get("src", "")).startswith("/")


# ── short-lived image links for a store to fetch ──────────────────────────────

@pytest.mark.asyncio
async def test_mint_media_url_asks_the_relay_with_the_image_name(media_tokens_in_test_transaction):
    from celerp.services.relay_share import mint_media_url

    image = {"id": "f1", "mime": "image/jpeg", "url": "/static/attachments/c1/f1.jpg"}
    with patch("celerp.gateway.state.relay_http_url", return_value="https://relay.test"), \
            patch("celerp.gateway.state.relay_session_headers", return_value={"X-Session-Token": "s"}), \
            respx.mock:
        mint = respx.post("https://relay.test/media/mint").mock(
            return_value=httpx.Response(200, json={"url": "https://share.example.com/m/e/f1.jpg"}))
        assert await mint_media_url("c1", image) == "https://share.example.com/m/e/f1.jpg"
        sent = __import__("json").loads(mint.calls[0].request.content)
        assert sent["filename"] == "f1.jpg"
        # The relay wraps only its own token shape (relay/services/connector_media.py MEDIA_TOKEN).
        assert __import__("re").fullmatch(r"[A-Za-z0-9_-]{43}", sent["media_token"])
        mint.mock(return_value=httpx.Response(402))
        assert await mint_media_url("c1", image) is None
    assert await mint_media_url("c1", {"id": "d", "mime": "application/pdf", "url": "/x.pdf"}) is None


@pytest.mark.asyncio
async def test_an_image_above_the_relays_limit_is_never_linked_and_says_why(media_tokens_in_test_transaction):
    """The relay advertises the largest image a store can fetch (hello_ack tunnel limits);
    a larger one gets no link, and the reason names its size and the limit."""
    import uuid

    from celerp.config import settings
    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.images import store_images
    from celerp.gateway import state

    cid = str(uuid.uuid4())
    folder = settings.data_dir / "static" / "attachments" / cid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "big.png").write_bytes(b"x" * 3 * 1024 * 1024)
    (folder / "small.png").write_bytes(b"x" * 1024)
    files = [{"id": "big", "mime": "image/png", "is_hero": True, "url": f"/static/attachments/{cid}/big.png"},
             {"id": "small", "mime": "image/png", "document_tag": "product_images",
              "url": f"/static/attachments/{cid}/small.png"}]
    ctx = ConnectorContext(company_id=cid, access_token="t", store_handle="https://store.example.com")
    mint = AsyncMock(return_value="https://share.example.com/m/e/x.png")
    with patch.object(state, "_tunnel_limits", {"media_max_bytes": 2 * 1024 * 1024}), \
            patch("celerp.services.relay_share.mint_media_url", new=mint):
        images, reason = await store_images(ctx, "woocommerce", "WooCommerce", files, {})
    assert images == [] and not mint.called
    assert "big" in reason and "3" in reason and "2" in reason and "WooCommerce" in reason

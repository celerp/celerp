# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Outbound sync method tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from celerp.connectors.base import ConnectorBase, ConnectorContext, SyncDirection, SyncEntity
from celerp.connectors.shopify import ShopifyConnector
from celerp.connectors.quickbooks import QuickBooksConnector


# -- Base class NotImplementedError --

@pytest.mark.asyncio
async def test_base_sync_products_out_raises():
    class Minimal(ConnectorBase):
        name = "minimal"
        display_name = "Minimal"
        entity_flows = {}
        category = None
        conflict_strategy = {}
        async def sync_products(self, ctx, since=None): ...
        async def sync_orders(self, ctx, since=None): ...

    ctx = ConnectorContext(company_id="co", access_token="tok")
    with pytest.raises(NotImplementedError, match="does not support outbound product sync"):
        await Minimal().sync_products_out(ctx)


@pytest.mark.asyncio
async def test_base_sync_invoices_out_raises():
    class Minimal(ConnectorBase):
        name = "minimal"
        display_name = "Minimal"
        entity_flows = {}
        category = None
        conflict_strategy = {}
        async def sync_products(self, ctx, since=None): ...
        async def sync_orders(self, ctx, since=None): ...

    ctx = ConnectorContext(company_id="co", access_token="tok")
    with pytest.raises(NotImplementedError, match="does not support outbound invoice sync"):
        await Minimal().sync_invoices_out(ctx)


# -- Shopify sync_inventory (outbound) --

@pytest.fixture
def shopify():
    return ShopifyConnector()


@pytest.fixture
def ctx_shopify():
    return ConnectorContext(
        company_id="test-co",
        access_token="shpat_test",
        store_handle="test-store.myshopify.com",
    )


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_shopify_sync_inventory_out_success(shopify, ctx_shopify):
    items = [
        {"sku": "A1", "shopify_variant_id": 111, "shopify_location_id": 999, "quantity": 10},
        {"sku": "A2", "shopify_variant_id": 222, "shopify_location_id": 999, "quantity": 5},
    ]
    with patch("celerp.connectors.upsert.list_items_with_external_id", new=AsyncMock(return_value=items)):
        with respx.mock:
            respx.post("https://test-store.myshopify.com/admin/api/2024-01/inventory_levels/set.json").mock(
                return_value=httpx.Response(200, json={"inventory_level": {}})
            )
            result = await shopify.sync_inventory_out(ctx_shopify)

    assert result.updated == 2
    assert result.entity == SyncEntity.INVENTORY
    assert result.direction == SyncDirection.OUTBOUND
    assert result.errors is None


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_shopify_sync_inventory_out_skips_missing_ids(shopify, ctx_shopify):
    items = [
        {"sku": "A1", "shopify_variant_id": None, "shopify_location_id": 999, "quantity": 10},
        {"sku": "A2", "shopify_variant_id": 222, "shopify_location_id": 999, "quantity": 5},
    ]
    with patch("celerp.connectors.upsert.list_items_with_external_id", new=AsyncMock(return_value=items)):
        with respx.mock:
            respx.post("https://test-store.myshopify.com/admin/api/2024-01/inventory_levels/set.json").mock(
                return_value=httpx.Response(200, json={"inventory_level": {}})
            )
            result = await shopify.sync_inventory_out(ctx_shopify)

    assert result.skipped == 1
    assert result.updated == 1


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_shopify_sync_inventory_out_error_accumulation(shopify, ctx_shopify):
    items = [
        {"sku": "A1", "shopify_variant_id": 111, "shopify_location_id": 999, "quantity": 10},
        {"sku": "A2", "shopify_variant_id": 222, "shopify_location_id": 999, "quantity": 5},
    ]
    call_count = 0

    async def mock_side_effect(request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.HTTPStatusError("429", request=None, response=httpx.Response(429))
        return httpx.Response(200, json={"inventory_level": {}})

    with patch("celerp.connectors.upsert.list_items_with_external_id", new=AsyncMock(return_value=items)):
        with respx.mock:
            respx.post("https://test-store.myshopify.com/admin/api/2024-01/inventory_levels/set.json").mock(
                side_effect=mock_side_effect
            )
            result = await shopify.sync_inventory_out(ctx_shopify)

    # one error, one success - continues past failure
    assert result.updated == 1
    assert len(result.errors) == 1


# -- Shopify sync_products_out --

@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_shopify_sync_products_out_success(shopify, ctx_shopify):
    items = [
        {"sku": "W1", "shopify_product_id": "777", "name": "Widget", "sale_price": 9.99},
    ]
    with patch("celerp.connectors.upsert.list_items_modified_since_last_sync", new=AsyncMock(return_value=items)):
        with respx.mock:
            respx.put("https://test-store.myshopify.com/admin/api/2024-01/products/777.json").mock(
                return_value=httpx.Response(200, json={"product": {"id": 777}})
            )
            result = await shopify.sync_products_out(ctx_shopify)

    assert result.updated == 1
    assert result.entity == SyncEntity.PRODUCTS
    assert result.direction == SyncDirection.OUTBOUND


@pytest.mark.asyncio
async def test_shopify_sync_products_out_skips_no_product_id(shopify, ctx_shopify):
    items = [{"sku": "X1", "shopify_product_id": None, "name": "No ID"}]
    with patch("celerp.connectors.upsert.list_items_modified_since_last_sync", new=AsyncMock(return_value=items)):
        result = await shopify.sync_products_out(ctx_shopify)

    assert result.skipped == 1
    assert result.updated == 0


# -- QuickBooks sync_invoices_out --

@pytest.fixture
def qb():
    return QuickBooksConnector()


@pytest.fixture
def ctx_quickbooks():
    return ConnectorContext(
        company_id="test-co",
        access_token="qb_test_token",
        store_handle="1234567890",
        extra={"realm_id": "1234567890"},
    )


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_quickbooks_sync_invoices_out_success(qb, ctx_quickbooks):
    invoices = [
        {"ref_id": "INV-001", "customer_external_id": "cust-1", "line_items": [
            {"description": "Widget", "quantity": 2, "unit_price": 10.0, "total": 20.0}
        ]},
    ]
    with patch("celerp.connectors.upsert.list_unsynced_invoices", new=AsyncMock(return_value=invoices)):
        with respx.mock:
            respx.post("https://quickbooks.api.intuit.com/v3/company/1234567890/invoice").mock(
                return_value=httpx.Response(200, json={"Invoice": {"Id": "qb-inv-1"}})
            )
            result = await qb.sync_invoices_out(ctx_quickbooks)

    assert result.created == 1
    assert result.entity == SyncEntity.INVOICES
    assert result.direction == SyncDirection.OUTBOUND
    assert result.errors is None


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
async def test_quickbooks_sync_invoices_out_error_accumulation(qb, ctx_quickbooks):
    invoices = [
        {"ref_id": "INV-001", "customer_external_id": "c1", "line_items": []},
        {"ref_id": "INV-002", "customer_external_id": "c2", "line_items": []},
    ]
    call_count = 0

    async def side_effect(request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.HTTPStatusError("500", request=None, response=httpx.Response(500))
        return httpx.Response(200, json={"Invoice": {"Id": "ok"}})

    with patch("celerp.connectors.upsert.list_unsynced_invoices", new=AsyncMock(return_value=invoices)):
        with respx.mock:
            respx.post("https://quickbooks.api.intuit.com/v3/company/1234567890/invoice").mock(
                side_effect=side_effect
            )
            result = await qb.sync_invoices_out(ctx_quickbooks)

    assert result.created == 1
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_quickbooks_sync_invoices_out_missing_realm(qb):
    ctx = ConnectorContext(company_id="co", access_token="tok", store_handle=None, extra={})
    result = await qb.sync_invoices_out(ctx)
    assert result.errors
    assert "realm_id" in result.errors[0]


# -- WooCommerce sync_products_out: changed fields only, images apart --

import json as _json  # noqa: E402

from connector_test_support import add_item, connect, connector_session, item_state, seed_company  # noqa: E402,F401
from ui.i18n import t  # noqa: E402

_WOO_API = "https://store.example.com/wp-json/wc/v3"
_OUT = {"products": "outbound", "stock": "off", "orders": "off", "customers": "off"}


def _agreed(**values) -> dict:
    from celerp.services.field_fingerprints import fingerprint
    return {key: fingerprint(value) for key, value in values.items()}


async def _linked(session, company: str, entity_id: str, product_id: str, *, agreed: dict | None = None,
                  files: list | None = None, **item):
    cid = await seed_company(session, company)
    link = {"product_id": product_id, "sync_enabled": True, "remote_deleted": False}
    if agreed is not None:
        link["field_fingerprints"] = agreed
    await add_item(session, cid, entity_id, item.pop("sku"), external_links={"woocommerce": link},
                   files=files or [], **item)
    await connect(session, cid, "woocommerce", _OUT)
    return cid, ConnectorContext(company_id=str(cid), access_token="ck:cs",
                                 store_handle="https://store.example.com")


def _store_put(product_id: str, images_status: int = 200, images_body=None):
    def reply(request):
        body = _json.loads(request.content)
        if "images" in body or "image" in body:
            if images_status != 200:
                return httpx.Response(images_status, json=images_body)
            return httpx.Response(200, json={"id": int(product_id), "date_modified_gmt": "2026-09-01T10:00:01",
                                             "images": [{"id": 900 + i} for i, _ in enumerate(body["images"])]})
        return httpx.Response(200, json={"id": int(product_id), "date_modified_gmt": "2026-09-01T10:00:00"})
    return respx.put(f"{_WOO_API}/products/{product_id}").mock(side_effect=reply)


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
@respx.mock
async def test_unchanged_product_not_sent(connector_session):
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp.connectors.product_links import external_link_for_state

    session = connector_session
    values = {"sku": "U-1", "name": "Mug", "description": "Blue", "sale_price": 10.0}
    cid, ctx = await _linked(session, "Unchanged", "item:u", "601", agreed={**_agreed(**values), "images": _agreed(i=[])["i"]},
                             **values)
    put = _store_put("601")

    result = await WooCommerceConnector().sync_products_out(ctx)
    assert not put.called
    assert (result.updated, result.skipped) == (0, 1)

    from test_helpers import provision_company_books  # noqa: F401
    from celerp.events.engine import emit_event
    await emit_event(session, company_id=cid, entity_id="item:u", entity_type="item", event_type="item.updated",
                     data={"fields_changed": {"name": {"old": "Mug", "new": "Big mug"}}}, actor_id=None,
                     location_id=None, source="api", idempotency_key="rename-u", metadata_={})
    await session.commit()  # a person's edit is its own request
    result = await WooCommerceConnector().sync_products_out(ctx)
    assert [_json.loads(c.request.content) for c in put.calls] == [{"name": "Big mug"}]
    assert result.updated == 1
    link = external_link_for_state(await item_state(session, cid, "item:u"), "woocommerce")
    assert link["field_fingerprints"]["name"] == _agreed(name="Big mug")["name"]

    # Sent once, agreed since: the next run sends nothing.
    await WooCommerceConnector().sync_products_out(ctx)
    assert len(put.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_made_up_sku_is_not_pushed(connector_session):
    """An item that was given a made-up SKU on import keeps it in Celerp but never
    sends it to the store, unless a person edits it."""
    from celerp.connectors.woocommerce import WooCommerceConnector

    session = connector_session
    values = {"name": "Untitled", "description": "", "sale_price": 1.0}
    _, ctx = await _linked(session, "MadeUp", "item:w", "602", sku="WC-602",
                           agreed={**_agreed(**values), "images": _agreed(i=[])["i"]}, **values)
    put = _store_put("602")
    result = await WooCommerceConnector().sync_products_out(ctx)
    assert not put.called and result.skipped == 1


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
@respx.mock
async def test_field_update_succeeds_when_image_rejected(connector_session, monkeypatch):
    from celerp.connectors.woocommerce import WooCommerceConnector

    session = connector_session
    monkeypatch.setattr("celerp.services.relay_share.mint_media_url",
                        AsyncMock(return_value="https://share.example.com/m/tok/f1.jpg"))
    files = [{"id": "f1", "filename": "f1.jpg", "mime": "image/jpeg", "is_hero": True,
              "document_tag": "product_images", "url": "/static/attachments/c/f1.jpg"}]
    _, ctx = await _linked(session, "ImageRejected", "item:r", "603", sku="R-1", name="Lamp", files=files)
    put = _store_put("603", images_status=400, images_body={
        "code": "woocommerce_product_image_upload_error", "message": "Error getting remote image."})

    result = await WooCommerceConnector().sync_products_out(ctx)

    bodies = [_json.loads(c.request.content) for c in put.calls]
    assert "images" not in bodies[0] and bodies[0]["name"] == "Lamp"
    assert bodies[1] == {"images": [{"src": "https://share.example.com/m/tok/f1.jpg"}]}
    assert result.updated == 1 and not result.errors
    assert [(e["id"], e["kind"]) for e in result.attention] == [("603", "image")]
    assert "Error getting remote image." in result.attention[0]["reason"]


@pytest.mark.usefixtures("inside_execution")
@pytest.mark.asyncio
@respx.mock
async def test_pulled_image_sent_by_id(connector_session, monkeypatch):
    from celerp.connectors.woocommerce import WooCommerceConnector

    session = connector_session
    mint = AsyncMock()
    monkeypatch.setattr("celerp.services.relay_share.mint_media_url", mint)
    files = [{"id": "f2", "filename": "a.jpg", "mime": "image/jpeg", "is_hero": True,
              "document_tag": "product_images", "url": "/static/attachments/c/f2.jpg",
              "remote": {"woocommerce": {"id": "11", "src": "https://cdn.example.com/a.jpg",
                                         "store": "https://store.example.com"}}}]
    values = {"sku": "P-1", "name": "Pulled", "description": "", "sale_price": 1.0}
    _, ctx = await _linked(session, "PulledImage", "item:p", "604", agreed=_agreed(**values),
                           files=files, **values)
    put = _store_put("604")

    await WooCommerceConnector().sync_products_out(ctx)

    assert [_json.loads(c.request.content) for c in put.calls] == [{"images": [{"id": 11}]}]
    mint.assert_not_awaited()

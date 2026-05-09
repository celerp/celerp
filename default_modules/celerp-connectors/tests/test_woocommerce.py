# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""WooCommerce connector tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from datetime import datetime, timezone
import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from celerp.connectors.woocommerce import WooCommerceConnector, _base_url, _auth
from celerp.connectors.base import ConnectorContext, SyncEntity, SyncDirection


@pytest.fixture
def woo():
    return WooCommerceConnector()


@pytest.fixture
def ctx():
    return ConnectorContext(
        company_id="test-co",
        access_token="ck_testkey:cs_testsecret",
        store_handle="https://store.example.com",
    )


@pytest.fixture
def mock_upsert_item():
    with patch("celerp.connectors.upsert.upsert_item", new_callable=AsyncMock, return_value=True) as m:
        yield m


@pytest.fixture
def mock_upsert_order():
    with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, return_value=True) as m:
        yield m


@pytest.fixture
def mock_upsert_contact():
    with patch("celerp.connectors.upsert.upsert_contact_from_woocommerce", new_callable=AsyncMock, return_value=True) as m:
        yield m


# -- Auth helpers --

def test_auth_parses_key_secret(ctx):
    assert _auth(ctx) == ("ck_testkey", "cs_testsecret")


def test_auth_missing_raises(ctx):
    ctx.access_token = "nocohere"
    with pytest.raises(ValueError, match="consumer_key:consumer_secret"):
        _auth(ctx)


def test_base_url(ctx):
    assert _base_url(ctx) == "https://store.example.com/wp-json/wc/v3"


def test_base_url_strips_trailing_slash(ctx):
    ctx.store_handle = "https://store.example.com/"
    assert _base_url(ctx) == "https://store.example.com/wp-json/wc/v3"


def test_base_url_missing_handle_raises():
    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle=None)
    with pytest.raises(ValueError):
        _base_url(ctx)


# -- Pagination --

@pytest.mark.asyncio
async def test_pagination_multiple_pages(woo, ctx, mock_upsert_item):
    """X-WP-TotalPages=2 should trigger a second request."""
    page1 = [{"id": 1, "name": "P1", "sku": "SKU-1", "regular_price": "10.00"}]
    page2 = [{"id": 2, "name": "P2", "sku": "SKU-2", "regular_price": "20.00"}]
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/products")
        route.side_effect = [
            httpx.Response(200, json=page1, headers={"X-WP-TotalPages": "2"}),
            httpx.Response(200, json=page2, headers={"X-WP-TotalPages": "2"}),
        ]
        result = await woo.sync_products(ctx)
    assert result.created == 2
    assert len(route.calls) == 2


@pytest.mark.asyncio
async def test_pagination_single_page(woo, ctx, mock_upsert_item):
    """Single page (no header or TotalPages=1) makes exactly one request."""
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[
                {"id": 1, "name": "P1", "sku": "SKU-1", "regular_price": "5.00"}
            ])  # no X-WP-TotalPages header -> defaults to 1
        )
        result = await woo.sync_products(ctx)
    assert result.created == 1
    assert len(route.calls) == 1


# -- sync_products --

@pytest.mark.asyncio
async def test_sync_products_creates_items(woo, ctx, mock_upsert_item):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[
                {"id": 10, "name": "Widget", "sku": "WDG-001", "regular_price": "9.99"}
            ])
        )
        result = await woo.sync_products(ctx)
    assert result.created == 1
    assert result.entity == SyncEntity.PRODUCTS
    mock_upsert_item.assert_called_once()


@pytest.mark.asyncio
async def test_sync_products_fallback_sku(woo, ctx, mock_upsert_item):
    """When sku is blank, fall back to WC-{id}."""
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[
                {"id": 42, "name": "No SKU Product", "sku": "", "regular_price": "5.00"}
            ])
        )
        result = await woo.sync_products(ctx)
    assert result.created == 1
    item_arg = mock_upsert_item.call_args[0][1]
    assert item_arg.sku == "WC-42"


@pytest.mark.asyncio
async def test_sync_products_uses_basic_auth(woo, ctx, mock_upsert_item):
    """Verify Basic Auth credentials are sent in request."""
    captured = {}
    with respx.mock:
        def capture(request):
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=[])
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(side_effect=capture)
        await woo.sync_products(ctx)
    import base64
    expected = base64.b64encode(b"ck_testkey:cs_testsecret").decode()
    assert captured["auth"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_sync_products_incremental(woo, ctx, mock_upsert_item):
    """Since parameter adds modified_after to request."""
    since = datetime(2026, 1, 15, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[])
        )
        await woo.sync_products(ctx, since=since)
    assert "modified_after" in str(route.calls[0].request.url)


@pytest.mark.asyncio
async def test_sync_products_api_error(woo, ctx):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(500)
        )
        result = await woo.sync_products(ctx)
    assert result.errors
    assert result.created == 0


# -- sync_orders --

@pytest.mark.asyncio
async def test_sync_orders_creates(woo, ctx, mock_upsert_order):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[{"id": 1001, "status": "processing"}])
        )
        result = await woo.sync_orders(ctx)
    assert result.created == 1
    assert result.entity == SyncEntity.ORDERS
    mock_upsert_order.assert_called_once()


@pytest.mark.asyncio
async def test_sync_orders_error_accumulation(woo, ctx):
    """All order errors must be captured, not just the first."""
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[
                {"id": 1}, {"id": 2}, {"id": 3}
            ])
        )
        with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, side_effect=ValueError("boom")):
            result = await woo.sync_orders(ctx)
    assert result.errors is not None
    assert len(result.errors) == 3


@pytest.mark.asyncio
async def test_sync_orders_incremental(woo, ctx, mock_upsert_order):
    since = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(200, json=[])
        )
        await woo.sync_orders(ctx, since=since)
    assert "modified_after" in str(route.calls[0].request.url)


# -- sync_contacts --

@pytest.mark.asyncio
async def test_sync_contacts_creates(woo, ctx, mock_upsert_contact):
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/customers").mock(
            return_value=httpx.Response(200, json=[
                {"id": 5, "email": "test@example.com", "first_name": "Alice", "last_name": "Smith"}
            ])
        )
        result = await woo.sync_contacts(ctx)
    assert result.created == 1
    assert result.entity == SyncEntity.CONTACTS


@pytest.mark.asyncio
async def test_sync_contacts_incremental(woo, ctx, mock_upsert_contact):
    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://store.example.com/wp-json/wc/v3/customers").mock(
            return_value=httpx.Response(200, json=[])
        )
        await woo.sync_contacts(ctx, since=since)
    assert "modified_after" in str(route.calls[0].request.url)


# -- sync_inventory_out --

@pytest.mark.asyncio
async def test_sync_inventory_out_pushes_stock(woo, ctx):
    items = [{"woocommerce_product_id": 99, "sku": "WDG-001", "quantity": 42}]
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=items):
        with respx.mock:
            route = respx.put("https://store.example.com/wp-json/wc/v3/products/99").mock(
                return_value=httpx.Response(200, json={"id": 99})
            )
            result = await woo.sync_inventory_out(ctx)
    assert result.updated == 1
    assert result.entity == SyncEntity.INVENTORY
    sent = route.calls[0].request
    import json
    body = json.loads(sent.content)
    assert body["stock_quantity"] == 42
    assert body["manage_stock"] is True


@pytest.mark.asyncio
async def test_sync_inventory_out_skips_missing_id(woo, ctx):
    items = [{"sku": "NO-ID", "quantity": 10}]  # no woocommerce_product_id
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=items):
        with respx.mock:
            result = await woo.sync_inventory_out(ctx)
    assert result.skipped == 1
    assert result.updated == 0


@pytest.mark.asyncio
async def test_sync_inventory_out_error_accumulation(woo, ctx):
    items = [
        {"woocommerce_product_id": 1, "quantity": 5},
        {"woocommerce_product_id": 2, "quantity": 3},
    ]
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=items):
        with respx.mock:
            respx.put("https://store.example.com/wp-json/wc/v3/products/1").mock(
                return_value=httpx.Response(500)
            )
            respx.put("https://store.example.com/wp-json/wc/v3/products/2").mock(
                return_value=httpx.Response(500)
            )
            result = await woo.sync_inventory_out(ctx)
    assert result.errors is not None
    assert len(result.errors) == 2

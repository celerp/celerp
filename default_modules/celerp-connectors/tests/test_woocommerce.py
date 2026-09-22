# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""WooCommerce connector tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from datetime import datetime, timezone
import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from celerp.connectors.woocommerce import WooCommerceConnector, _base_url, _auth
from celerp.connectors.base import ConnectorContext, SyncEntity


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
    with patch("celerp_inventory.services.upsert_external_product", new_callable=AsyncMock, return_value=("created", "item:resolved")) as m:
        yield m


@pytest.fixture
def mock_upsert_order():
    with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new_callable=AsyncMock, return_value="created") as m:
        yield m


@pytest.fixture
def mock_upsert_contact():
    with patch("celerp.connectors.upsert.upsert_contact_from_woocommerce", new_callable=AsyncMock, return_value="created") as m:
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
async def test_sync_products_imports_variations(woo, ctx, mock_upsert_item):
    """A variable product imports each variation as its own item, not the priceless parent."""
    with respx.mock:
        respx.get("https://store.example.com/wp-json/wc/v3/products").mock(
            return_value=httpx.Response(200, json=[{"id": 20, "name": "Shirt", "type": "variable"}])
        )
        respx.get("https://store.example.com/wp-json/wc/v3/products/20/variations").mock(
            return_value=httpx.Response(200, json=[
                {"id": 201, "sku": "SHIRT-RED-L", "price": "19.99",
                 "attributes": [{"option": "Red"}, {"option": "L"}]},
                {"id": 202, "sku": "SHIRT-BLU-M", "price": "19.99",
                 "attributes": [{"option": "Blue"}, {"option": "M"}]},
            ])
        )
        result = await woo.sync_products(ctx)
    assert result.created == 2   # two variations; parent not imported as a sellable item
    identities = {(call.kwargs["product_id"], call.kwargs["variation_id"]) for call in mock_upsert_item.call_args_list}
    assert identities == {("20", "201"), ("20", "202")}


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
    assert mock_upsert_item.call_args.kwargs["sku"] == "WC-42"


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
async def test_sync_inventory_out_uses_variation_endpoint(woo, ctx):
    item = {
        "sku": "V-1", "quantity": 7,
        "woocommerce_product_id": "20", "woocommerce_variation_id": "201",
        "external_link": {"product_id": "20", "variation_id": "201", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/20/variations/201"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_inventory_out(ctx)
    assert result.updated == 1
    assert route.calls[0].request.content == b'{"stock_quantity":7}'


@pytest.mark.asyncio
async def test_sync_inventory_out_refuses_fractional_without_write(woo, ctx):
    item = {
        "sku": "F-1", "quantity": 1.5, "woocommerce_product_id": "10",
        "external_link": {"product_id": "10", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put("https://store.example.com/wp-json/wc/v3/products/10")
            result = await woo.sync_inventory_out(ctx)
    assert result.updated == 0
    assert result.errors and "fractional stock" in result.errors[0]
    assert len(route.calls) == 0


@pytest.mark.asyncio
async def test_sync_inventory_out_refuses_parent_managed_variation(woo, ctx):
    item = {
        "sku": "V-1", "quantity": 3,
        "woocommerce_product_id": "20", "woocommerce_variation_id": "201",
        "external_link": {"product_id": "20", "variation_id": "201", "manage_stock": "parent"},
    }
    with patch("celerp.connectors.upsert.list_items_with_external_id", new_callable=AsyncMock, return_value=[item]):
        result = await woo.sync_inventory_out(ctx)
    assert result.updated == 0
    assert result.errors and "managed by its parent" in result.errors[0]


# -- sync_products_out --

@pytest.mark.asyncio
async def test_sync_products_out_uses_nested_variation_endpoint(woo, ctx):
    item = {
        "sku": "SHIRT-RED", "name": "Shirt - Red", "description": "Red shirt",
        "sale_price": 19.99, "files": [],
        "woocommerce_product_id": "20", "woocommerce_variation_id": "201",
        "external_link": {"product_id": "20", "variation_id": "201", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_modified_since_last_sync", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/20/variations/201"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_products_out(ctx)
    assert result.updated == 1
    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload["sku"] == "SHIRT-RED"
    assert payload["regular_price"] == "19.99"
    assert payload["description"] == "Red shirt"
    assert "name" not in payload


@pytest.mark.asyncio
async def test_sync_products_out_simple_product_includes_core_fields(woo, ctx):
    item = {
        "sku": "W-1", "name": "Widget", "description": "Useful",
        "sale_price": 12.5, "files": [], "woocommerce_product_id": "10",
        "external_link": {"product_id": "10", "manage_stock": True},
    }
    with patch("celerp.connectors.upsert.list_items_modified_since_last_sync", new_callable=AsyncMock, return_value=[item]):
        with respx.mock:
            route = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/10"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_products_out(ctx)
    assert result.updated == 1
    payload = __import__("json").loads(route.calls[0].request.content)
    assert payload == {
        "sku": "W-1", "description": "Useful",
        "regular_price": "12.5", "name": "Widget",
    }



def test_woocommerce_commercial_fingerprint_ignores_status_only_changes():
    from celerp_docs.doc_service import _woocommerce_commercial_fingerprint
    base = {
        "currency": "USD", "status": "processing", "total": "12.00", "total_tax": "2.00",
        "line_items": [{"product_id": 1, "variation_id": 0, "sku": "A", "quantity": 1, "total": "10.00", "total_tax": "2.00"}],
        "shipping_lines": [], "fee_lines": [],
    }
    changed_status = {**base, "status": "completed"}
    assert _woocommerce_commercial_fingerprint(base) == _woocommerce_commercial_fingerprint(changed_status)


def test_woocommerce_commercial_fingerprint_detects_financial_change():
    from celerp_docs.doc_service import _woocommerce_commercial_fingerprint
    base = {
        "currency": "USD", "total": "10.00", "total_tax": "0",
        "line_items": [{"product_id": 1, "variation_id": 0, "sku": "A", "quantity": 1, "total": "10.00"}],
        "shipping_lines": [], "fee_lines": [],
    }
    changed = {**base, "total": "11.00"}
    assert _woocommerce_commercial_fingerprint(base) != _woocommerce_commercial_fingerprint(changed)


@pytest.mark.asyncio
async def test_register_webhooks_rolls_back_partial_creation(woo, ctx):
    with respx.mock:
        first = respx.post("https://store.example.com/wp-json/wc/v3/webhooks").mock(
            side_effect=[
                httpx.Response(201, json={"id": 11}),
                httpx.Response(403, json={"message": "read only"}),
            ]
        )
        cleanup = respx.delete(
            "https://store.example.com/wp-json/wc/v3/webhooks/11"
        ).mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(httpx.HTTPStatusError):
            await woo.register_webhooks(ctx, "https://relay.test/hook", secret="s")
    assert len(first.calls) == 2
    assert len(cleanup.calls) == 1


@pytest.mark.asyncio
async def test_deregister_webhooks_tolerates_missing_hook(woo, ctx):
    with respx.mock:
        respx.delete("https://store.example.com/wp-json/wc/v3/webhooks/11").mock(
            return_value=httpx.Response(404)
        )
        await woo.deregister_webhooks(ctx, ["11"])


@pytest.mark.asyncio
async def test_sync_inventory_identity_out_pushes_only_selected_product(woo, ctx):
    items = [
        {
            "quantity": 2, "woocommerce_product_id": "10",
            "external_link": {"product_id": "10", "manage_stock": True},
        },
        {
            "quantity": 9, "woocommerce_product_id": "11",
            "external_link": {"product_id": "11", "manage_stock": True},
        },
    ]
    with patch(
        "celerp.connectors.upsert.list_items_with_external_id",
        new_callable=AsyncMock, return_value=items,
    ):
        with respx.mock:
            selected = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/10"
            ).mock(return_value=httpx.Response(200, json={}))
            other = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/11"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await woo.sync_inventory_identity_out(ctx, "10")
    assert result.updated == 1
    assert len(selected.calls) == 1
    assert len(other.calls) == 0

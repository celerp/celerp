# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shopify connector tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from datetime import datetime, timezone
import pytest
import respx
import httpx

from celerp.connectors.shopify import ShopifyConnector, _next_page_url
from celerp.connectors.base import SyncEntity, SyncDirection


@pytest.fixture
def shopify():
    return ShopifyConnector()


# -- Pagination helper --

def test_next_page_url_with_next():
    link = '<https://store.myshopify.com/admin/api/2024-01/products.json?page_info=abc>; rel="next"'
    assert _next_page_url(link) == "https://store.myshopify.com/admin/api/2024-01/products.json?page_info=abc"


def test_next_page_url_no_next():
    link = '<https://store.myshopify.com/admin/api/2024-01/products.json?page_info=abc>; rel="previous"'
    assert _next_page_url(link) is None


def test_next_page_url_empty():
    assert _next_page_url("") is None


# -- sync_products --

@pytest.mark.asyncio
async def test_sync_products_creates_items(shopify, ctx_shopify, mock_upsert_item):
    with respx.mock:
        respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
            return_value=httpx.Response(200, json={"products": [
                {"id": 1, "title": "Widget", "variants": [
                    {"id": 10, "sku": "WDG-001", "title": "Default Title", "price": "9.99", "inventory_quantity": 5}
                ]}
            ]})
        )
        result = await shopify.sync_products(ctx_shopify)
    assert result.created == 1
    assert result.entity == SyncEntity.PRODUCTS
    mock_upsert_item.assert_called_once()


@pytest.mark.asyncio
async def test_sync_products_skips_no_sku(shopify, ctx_shopify, mock_upsert_item):
    with respx.mock:
        respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
            return_value=httpx.Response(200, json={"products": [
                {"id": 1, "title": "Widget", "variants": [
                    {"id": 10, "sku": "", "title": "Default Title", "price": "9.99"}
                ]}
            ]})
        )
        result = await shopify.sync_products(ctx_shopify)
    assert result.skipped == 1
    assert result.created == 0
    mock_upsert_item.assert_not_called()


@pytest.mark.asyncio
async def test_sync_products_variant_naming(shopify, ctx_shopify, mock_upsert_item):
    """Default Title variant uses product title only; named variants get appended."""
    with respx.mock:
        respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
            return_value=httpx.Response(200, json={"products": [
                {"id": 1, "title": "T-Shirt", "variants": [
                    {"id": 10, "sku": "TS-RED", "title": "Red / Large", "price": "25.00"},
                    {"id": 11, "sku": "TS-DEF", "title": "Default Title", "price": "25.00"},
                ]}
            ]})
        )
        result = await shopify.sync_products(ctx_shopify)
    assert result.created == 2
    calls = mock_upsert_item.call_args_list
    # First variant: has named variant
    item1 = calls[0][0][1]  # second positional arg
    assert "Red / Large" in item1.name
    # Second variant: default title, should use product name only
    item2 = calls[1][0][1]
    assert item2.name == "T-Shirt"


@pytest.mark.asyncio
async def test_sync_products_api_error(shopify, ctx_shopify):
    with respx.mock:
        respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
            return_value=httpx.Response(500)
        )
        result = await shopify.sync_products(ctx_shopify)
    assert result.errors
    assert result.created == 0


@pytest.mark.asyncio
async def test_sync_products_incremental(shopify, ctx_shopify, mock_upsert_item):
    """Since parameter adds updated_at_min to request."""
    since = datetime(2026, 1, 15, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
            return_value=httpx.Response(200, json={"products": []})
        )
        await shopify.sync_products(ctx_shopify, since=since)
    # Verify updated_at_min was in the request params
    request = route.calls[0].request
    assert "updated_at_min" in str(request.url)


# -- sync_orders --

@pytest.mark.asyncio
async def test_sync_orders_no_double_question_mark(shopify, ctx_shopify, mock_upsert_order):
    """Regression: pagination must not create double-? URL."""
    with respx.mock:
        route = respx.get("https://test-store.myshopify.com/admin/api/2024-01/orders.json").mock(
            return_value=httpx.Response(200, json={"orders": []})
        )
        await shopify.sync_orders(ctx_shopify)
    request = route.calls[0].request
    url_str = str(request.url)
    assert url_str.count("?") == 1, f"Double ? in URL: {url_str}"


@pytest.mark.asyncio
async def test_sync_orders_error_accumulation(shopify, ctx_shopify):
    """All order errors must be captured, not just the first."""
    with respx.mock:
        respx.get("https://test-store.myshopify.com/admin/api/2024-01/orders.json").mock(
            return_value=httpx.Response(200, json={"orders": [
                {"id": 1, "name": "#1001"},
                {"id": 2, "name": "#1002"},
                {"id": 3, "name": "#1003"},
            ]})
        )
        from unittest.mock import AsyncMock, patch
        with patch("celerp.connectors.upsert.upsert_order_from_shopify", new_callable=AsyncMock, side_effect=ValueError("test error")):
            result = await shopify.sync_orders(ctx_shopify)
    assert result.errors is not None
    assert len(result.errors) == 3, f"Expected 3 errors, got {len(result.errors)}: {result.errors}"

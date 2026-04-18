# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Outbound sync method tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from celerp.connectors.base import ConnectorBase, ConnectorContext, SyncDirection, SyncEntity, SyncResult
from celerp.connectors.shopify import ShopifyConnector
from celerp.connectors.quickbooks import QuickBooksConnector
from celerp.connectors.xero import XeroConnector


# -- Base class NotImplementedError --

@pytest.mark.asyncio
async def test_base_sync_products_out_raises():
    class Minimal(ConnectorBase):
        name = "minimal"
        display_name = "Minimal"
        supported_entities = []
        direction = SyncDirection.INBOUND
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
        supported_entities = []
        direction = SyncDirection.INBOUND
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
            result = await shopify.sync_inventory(ctx_shopify)

    assert result.updated == 2
    assert result.entity == SyncEntity.INVENTORY
    assert result.direction == SyncDirection.OUTBOUND
    assert result.errors is None


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
            result = await shopify.sync_inventory(ctx_shopify)

    assert result.skipped == 1
    assert result.updated == 1


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
            result = await shopify.sync_inventory(ctx_shopify)

    # one error, one success - continues past failure
    assert result.updated == 1
    assert len(result.errors) == 1


# -- Shopify sync_products_out --

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
        store_handle="realm-123",
        extra={"realm_id": "realm-123"},
    )


@pytest.mark.asyncio
async def test_quickbooks_sync_invoices_out_success(qb, ctx_quickbooks):
    invoices = [
        {"ref_id": "INV-001", "customer_external_id": "cust-1", "line_items": [
            {"description": "Widget", "quantity": 2, "unit_price": 10.0, "total": 20.0}
        ]},
    ]
    with patch("celerp.connectors.upsert.list_unsynced_invoices", new=AsyncMock(return_value=invoices)):
        with respx.mock:
            respx.post("https://quickbooks.api.intuit.com/v3/company/realm-123/invoice").mock(
                return_value=httpx.Response(200, json={"Invoice": {"Id": "qb-inv-1"}})
            )
            result = await qb.sync_invoices_out(ctx_quickbooks)

    assert result.created == 1
    assert result.entity == SyncEntity.INVOICES
    assert result.direction == SyncDirection.OUTBOUND
    assert result.errors is None


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
            respx.post("https://quickbooks.api.intuit.com/v3/company/realm-123/invoice").mock(
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


# -- Xero sync_invoices_out --

@pytest.fixture
def xero():
    return XeroConnector()


@pytest.fixture
def ctx_xero():
    return ConnectorContext(
        company_id="test-co",
        access_token="xero_test_token",
        store_handle="tenant-abc",
        extra={"tenant_id": "tenant-abc"},
    )


@pytest.mark.asyncio
async def test_xero_sync_invoices_out_success(xero, ctx_xero):
    invoices = [
        {"ref_id": "INV-X1", "customer_external_id": "contact-uuid-1", "line_items": [
            {"description": "Service", "quantity": 1, "unit_price": 100.0, "total": 100.0}
        ]},
    ]
    with patch("celerp.connectors.upsert.list_unsynced_invoices", new=AsyncMock(return_value=invoices)):
        with respx.mock:
            respx.put("https://api.xero.com/api.xro/2.0/Invoices").mock(
                return_value=httpx.Response(200, json={"Invoices": [{"InvoiceID": "xero-1"}]})
            )
            result = await xero.sync_invoices_out(ctx_xero)

    assert result.created == 1
    assert result.entity == SyncEntity.INVOICES
    assert result.direction == SyncDirection.OUTBOUND
    assert result.errors is None


@pytest.mark.asyncio
async def test_xero_sync_invoices_out_error_accumulation(xero, ctx_xero):
    invoices = [
        {"ref_id": "INV-X1", "customer_external_id": "c1", "line_items": []},
        {"ref_id": "INV-X2", "customer_external_id": "c2", "line_items": []},
    ]
    call_count = 0

    async def side_effect(request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.HTTPStatusError("403", request=None, response=httpx.Response(403))
        return httpx.Response(200, json={"Invoices": [{"InvoiceID": "ok"}]})

    with patch("celerp.connectors.upsert.list_unsynced_invoices", new=AsyncMock(return_value=invoices)):
        with respx.mock:
            respx.put("https://api.xero.com/api.xro/2.0/Invoices").mock(side_effect=side_effect)
            result = await xero.sync_invoices_out(ctx_xero)

    assert result.created == 1
    assert len(result.errors) == 1

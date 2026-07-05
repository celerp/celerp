# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests for connector correctness fixes:
  - money(): a real 0 price is kept as 0.0, never collapsed to None
  - Xero /Items is fetched once (non-paginating endpoint must not loop)
  - QuickBooks realmId is validated as numeric
"""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from celerp.connectors.util import money
from celerp.connectors.base import ConnectorContext
from celerp.connectors.woocommerce import WooCommerceConnector
from celerp.connectors.quickbooks import QuickBooksConnector
from celerp.connectors.xero import XeroConnector


# ── money() ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("abc", None),
    (0, 0.0), ("0", 0.0), ("0.00", 0.0),   # a real zero stays zero, not None
    (5, 5.0), ("5.50", 5.5), (5.5, 5.5),
])
def test_money_keeps_zero(raw, expected):
    assert money(raw) == expected


# ── price 0 -> 0.0 across connectors ─────────────────────────────────────────

def _captured_price(mock_upsert):
    return mock_upsert.call_args[0][1].sale_price


@pytest.mark.asyncio
async def test_woocommerce_zero_price_kept():
    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle="https://shop.test")
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value="created")) as up:
        respx.get("https://shop.test/wp-json/wc/v3/products").mock(return_value=httpx.Response(
            200, json=[{"id": 1, "sku": "FREE", "name": "Free", "regular_price": "0"}],
            headers={"X-WP-TotalPages": "1"}))
        await WooCommerceConnector().sync_products(ctx)
    assert _captured_price(up) == 0.0


@pytest.mark.asyncio
async def test_quickbooks_zero_price_kept():
    ctx = ConnectorContext(company_id="co", access_token="t", store_handle="1234567890")
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value="created")) as up:
        respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {"Item": [
                {"Id": "1", "Name": "Free", "Sku": "FREE", "Type": "Inventory", "UnitPrice": 0}]}}))
        await QuickBooksConnector().sync_products(ctx)
    assert _captured_price(up) == 0.0


@pytest.mark.asyncio
async def test_xero_zero_price_kept():
    ctx = ConnectorContext(company_id="co", access_token="t", store_handle="tenant-abc",
                           extra={"tenant_id": "tenant-abc"})
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value="created")) as up:
        respx.get("https://api.xero.com/api.xro/2.0/Items").mock(return_value=httpx.Response(
            200, json={"Items": [{"ItemID": "i1", "Code": "FREE", "Name": "Free",
                                  "SalesDetails": {"UnitPrice": 0}}]}))
        await XeroConnector().sync_products(ctx)
    assert _captured_price(up) == 0.0


# ── Xero /Items must be fetched once (non-paginating endpoint) ────────────────

@pytest.mark.asyncio
async def test_xero_items_fetched_once_not_looped():
    """A full page (100) from the non-paginating /Items endpoint must not trigger
    another request — the old loop re-fetched the same set forever."""
    ctx = ConnectorContext(company_id="co", access_token="t", store_handle="tenant-abc",
                           extra={"tenant_id": "tenant-abc"})
    items = [{"ItemID": f"i{i}", "Code": f"C{i}", "Name": f"N{i}"} for i in range(100)]
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value="created")):
        route = respx.get("https://api.xero.com/api.xro/2.0/Items").mock(
            return_value=httpx.Response(200, json={"Items": items}))
        result = await XeroConnector().sync_products(ctx)
    assert route.call_count == 1            # fetched exactly once
    assert result.created == 100            # all items synced, no duplicates


# ── QuickBooks realmId validation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quickbooks_non_numeric_realm_rejected():
    ctx = ConnectorContext(company_id="co", access_token="t", store_handle="evil.com/x?")
    with pytest.raises(ValueError, match="numeric"):
        await QuickBooksConnector().sync_products(ctx)


# ── WooCommerce pagination without the X-WP-TotalPages header ─────────────────

@pytest.mark.asyncio
async def test_woocommerce_paginates_without_total_pages_header():
    """No X-WP-TotalPages header must page until a short page, not truncate at 1."""
    ctx = ConnectorContext(company_id="co", access_token="k:s", store_handle="https://shop.test")
    page1 = [{"id": i, "sku": f"S{i}", "name": "x", "regular_price": "1"} for i in range(100)]
    page2 = [{"id": 100, "sku": "S100", "name": "x", "regular_price": "1"}]
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value="created")):
        route = respx.get("https://shop.test/wp-json/wc/v3/products").mock(side_effect=[
            httpx.Response(200, json=page1),   # full page, no header
            httpx.Response(200, json=page2),   # short page -> stop
        ])
        result = await WooCommerceConnector().sync_products(ctx)
    assert route.call_count == 2
    assert result.created == 101


# ── store_url validation helper (WC7) ────────────────────────────────────────

def test_store_url_error_rules(monkeypatch):
    from ui.routes.settings_connectors import _store_url_error
    monkeypatch.delenv("CELERP_ALLOW_HTTP_STORE", raising=False)
    assert _store_url_error("https://shop.test", "woocommerce") is None
    assert _store_url_error("http://shop.test", "woocommerce") == "connectors.store_url_must_use_https"
    assert _store_url_error("", "woocommerce") == "connectors.store_url_required"
    assert _store_url_error("", "someother") is None       # only woo requires it
    # http allowed under the dev override
    monkeypatch.setenv("CELERP_ALLOW_HTTP_STORE", "1")
    assert _store_url_error("http://shop.test", "woocommerce") is None


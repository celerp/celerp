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
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=True)) as up:
        respx.get("https://shop.test/wp-json/wc/v3/products").mock(return_value=httpx.Response(
            200, json=[{"id": 1, "sku": "FREE", "name": "Free", "regular_price": "0"}],
            headers={"X-WP-TotalPages": "1"}))
        await WooCommerceConnector().sync_products(ctx)
    assert _captured_price(up) == 0.0


@pytest.mark.asyncio
async def test_quickbooks_zero_price_kept():
    ctx = ConnectorContext(company_id="co", access_token="t", store_handle="1234567890")
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=True)) as up:
        respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {"Item": [
                {"Id": "1", "Name": "Free", "Sku": "FREE", "Type": "Inventory", "UnitPrice": 0}]}}))
        await QuickBooksConnector().sync_products(ctx)
    assert _captured_price(up) == 0.0


@pytest.mark.asyncio
async def test_xero_zero_price_kept():
    ctx = ConnectorContext(company_id="co", access_token="t", store_handle="tenant-abc",
                           extra={"tenant_id": "tenant-abc"})
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=True)) as up:
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
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=True)):
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
    with respx.mock, patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=True)):
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


# ── QB6: run_sync pulls incrementally from the last successful watermark ───────

@pytest.mark.asyncio
async def test_run_sync_uses_last_success_watermark_when_since_none():
    from datetime import datetime, timezone
    from celerp.connectors import sync_runner
    from celerp.connectors.base import SyncDirection, SyncResult, SyncEntity

    wm = datetime(2026, 6, 1, tzinfo=timezone.utc)
    captured = {}

    class _Stub:
        name = "stub"
        direction = SyncDirection.BOTH
        async def sync_orders(self, ctx, since=None):
            captured["since"] = since
            return SyncResult(entity=SyncEntity.ORDERS, direction=SyncDirection.BOTH)

    sess = MagicMock(); sess.add = MagicMock(); sess.commit = AsyncMock()
    cm = MagicMock(); cm.__aenter__ = AsyncMock(return_value=sess); cm.__aexit__ = AsyncMock(return_value=False)
    ctx = ConnectorContext(company_id="co", access_token="t")
    with patch.object(sync_runner, "_last_success_watermark", new=AsyncMock(return_value=wm)), \
         patch("celerp.db.get_session_ctx", return_value=cm):
        await sync_runner.run_sync(_Stub(), ctx, "orders")
    assert captured["since"] == wm   # incremental: the connector got the watermark


@pytest.mark.asyncio
async def test_run_sync_records_failure_on_exception():
    from celerp.connectors import sync_runner
    from celerp.connectors.base import SyncDirection

    class _Boom:
        name = "boom"
        direction = SyncDirection.BOTH
        async def sync_orders(self, ctx, since=None):
            raise RuntimeError("api 500")

    sess = MagicMock(); sess.add = MagicMock(); sess.commit = AsyncMock()
    cm = MagicMock(); cm.__aenter__ = AsyncMock(return_value=sess); cm.__aexit__ = AsyncMock(return_value=False)
    ctx = ConnectorContext(company_id="co", access_token="t")
    with patch.object(sync_runner, "_last_success_watermark", new=AsyncMock(return_value=None)), \
         patch("celerp.db.get_session_ctx", return_value=cm):
        res = await sync_runner.run_sync(_Boom(), ctx, "orders")
    assert res.errors and "api 500" in res.errors[0]
    assert sess.add.call_args[0][0].status == "failed"   # a failed audit row was recorded


@pytest.mark.asyncio
async def test_run_sync_handles_not_implemented():
    from celerp.connectors import sync_runner
    from celerp.connectors.base import SyncDirection

    class _NI:
        name = "ni"
        direction = SyncDirection.BOTH
        async def sync_contacts(self, ctx, since=None):
            raise NotImplementedError("nope")

    sess = MagicMock(); sess.add = MagicMock(); sess.commit = AsyncMock()
    cm = MagicMock(); cm.__aenter__ = AsyncMock(return_value=sess); cm.__aexit__ = AsyncMock(return_value=False)
    ctx = ConnectorContext(company_id="co", access_token="t")
    with patch.object(sync_runner, "_last_success_watermark", new=AsyncMock(return_value=None)), \
         patch("celerp.db.get_session_ctx", return_value=cm):
        res = await sync_runner.run_sync(_NI(), ctx, "contacts")
    assert res.errors and "does not support" in res.errors[0]


@pytest.mark.asyncio
async def test_run_sync_outbound_skips_watermark():
    from celerp.connectors import sync_runner
    from celerp.connectors.base import SyncDirection, SyncResult, SyncEntity

    called = {"wm": False}

    async def _wm(*a, **k):
        called["wm"] = True
        return None

    class _Stub:
        name = "stub"
        direction = SyncDirection.BOTH
        async def sync_invoices_out(self, ctx):
            return SyncResult(entity=SyncEntity.INVOICES, direction=SyncDirection.OUTBOUND)

    sess = MagicMock(); sess.add = MagicMock(); sess.commit = AsyncMock()
    cm = MagicMock(); cm.__aenter__ = AsyncMock(return_value=sess); cm.__aexit__ = AsyncMock(return_value=False)
    ctx = ConnectorContext(company_id="co", access_token="t")
    with patch.object(sync_runner, "_last_success_watermark", new=_wm), \
         patch("celerp.db.get_session_ctx", return_value=cm):
        await sync_runner.run_sync(_Stub(), ctx, "invoices_out")
    assert called["wm"] is False   # outbound never derives an inbound watermark

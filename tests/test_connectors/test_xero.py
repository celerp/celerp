# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Xero connector tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from datetime import datetime, timezone
import pytest
import respx
import httpx

from celerp.connectors.xero import XeroConnector
from celerp.connectors.base import SyncEntity


@pytest.fixture
def xero():
    return XeroConnector()


@pytest.mark.asyncio
async def test_sync_products_maps_fields(xero, ctx_xero, mock_upsert_item):
    with respx.mock:
        respx.get("https://api.xero.com/api.xro/2.0/Items").mock(
            return_value=httpx.Response(200, json={"Items": [
                {"ItemID": "abc", "Code": "XR-001", "Name": "Xero Widget",
                 "SalesDetails": {"UnitPrice": 15.0}, "PurchaseDetails": {"UnitPrice": 8.0}}
            ]})
        )
        result = await xero.sync_products(ctx_xero)
    assert result.created == 1
    item = mock_upsert_item.call_args[0][1]
    assert item.sku == "XR-001"


@pytest.mark.asyncio
async def test_sync_products_skips_no_code(xero, ctx_xero, mock_upsert_item):
    with respx.mock:
        respx.get("https://api.xero.com/api.xro/2.0/Items").mock(
            return_value=httpx.Response(200, json={"Items": [
                {"ItemID": "abc", "Code": "", "Name": "No Code Item"}
            ]})
        )
        result = await xero.sync_products(ctx_xero)
    assert result.skipped == 1
    mock_upsert_item.assert_not_called()


@pytest.mark.asyncio
async def test_sync_orders_filters_accrec(xero, ctx_xero, mock_upsert_invoice_xero):
    with respx.mock:
        respx.get("https://api.xero.com/api.xro/2.0/Invoices").mock(
            return_value=httpx.Response(200, json={"Invoices": [
                {"InvoiceID": "i1", "InvoiceNumber": "INV-001", "Type": "ACCREC"},
                {"InvoiceID": "i2", "InvoiceNumber": "BILL-001", "Type": "ACCPAY"},
            ]})
        )
        result = await xero.sync_orders(ctx_xero)
    assert result.created == 1
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_tenant_id_in_headers(xero, ctx_xero, mock_upsert_item):
    with respx.mock:
        route = respx.get("https://api.xero.com/api.xro/2.0/Items").mock(
            return_value=httpx.Response(200, json={"Items": []})
        )
        await xero.sync_products(ctx_xero)
    request = route.calls[0].request
    assert request.headers["Xero-Tenant-Id"] == "tenant-abc"


@pytest.mark.asyncio
async def test_sync_products_incremental(xero, ctx_xero, mock_upsert_item):
    since = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://api.xero.com/api.xro/2.0/Items").mock(
            return_value=httpx.Response(200, json={"Items": []})
        )
        await xero.sync_products(ctx_xero, since=since)
    request = route.calls[0].request
    assert "If-Modified-Since" in request.headers

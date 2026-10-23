# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""QuickBooks connector tests."""
from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from datetime import datetime, timezone
import pytest
import respx
import httpx

from celerp.connectors.quickbooks import QuickBooksConnector
from celerp.connectors.base import SyncEntity


@pytest.fixture
def qb():
    return QuickBooksConnector()


@pytest.mark.asyncio
async def test_sync_products_filters_by_type(qb, ctx_quickbooks, mock_upsert_item):
    with respx.mock:
        respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {"Item": [
                {"Id": "1", "Name": "Widget", "Sku": "WDG", "Type": "Inventory", "UnitPrice": 10},
                {"Id": "2", "Name": "Category", "Type": "Category"},  # should be skipped
            ]}})
        )
        result = await qb.sync_products(ctx_quickbooks)
    assert result.created == 1
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_sync_products_sku_fallback(qb, ctx_quickbooks, mock_upsert_item):
    """Falls back to Name when Sku is empty."""
    with respx.mock:
        respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {"Item": [
                {"Id": "1", "Name": "Consulting", "Type": "Service"},
            ]}})
        )
        result = await qb.sync_products(ctx_quickbooks)
    assert result.created == 1
    item = mock_upsert_item.call_args[0][1]
    assert item.sku == "Consulting"


@pytest.mark.asyncio
async def test_sync_products_api_error(qb, ctx_quickbooks):
    with respx.mock:
        respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
            return_value=httpx.Response(500)
        )
        result = await qb.sync_products(ctx_quickbooks)
    assert result.errors


@pytest.mark.asyncio
async def test_sync_products_incremental(qb, ctx_quickbooks, mock_upsert_item):
    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with respx.mock:
        route = respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {}})
        )
        await qb.sync_products(ctx_quickbooks, since=since)
    request = route.calls[0].request
    # QB embeds the since condition in the SQL query parameter
    assert "LastUpdatedTime" in str(request.url)


@pytest.mark.asyncio
async def test_missing_realm_id(qb):
    ctx = type("Ctx", (), {"company_id": "co", "access_token": "tok", "store_handle": "", "extra": None})()
    with pytest.raises(ValueError, match="realmId"):
        await qb.sync_products(ctx)


@pytest.mark.asyncio
async def test_invoice_query_is_valid_with_since_and_start_date(qb, ctx_quickbooks):
    """An incremental invoice pull is a valid query, and invoices dated before the
    connection's earliest order date are not fetched."""
    from unittest.mock import AsyncMock, patch

    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with respx.mock, patch("celerp.connectors.upsert.upsert_invoice_from_quickbooks", new=AsyncMock()):
        route = respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json={"QueryResponse": {}}))
        await qb.sync_orders(ctx_quickbooks, since=since,
                             orders_from=datetime(2026, 9, 1, tzinfo=timezone.utc))
    query = route.calls[0].request.url.params["query"]
    assert query.startswith(
        f"SELECT * FROM Invoice WHERE MetaData.LastUpdatedTime > '{since.isoformat()}' "
        "AND TxnDate >= '2026-09-01'")

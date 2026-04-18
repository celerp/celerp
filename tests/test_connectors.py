# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Tests for celerp/connectors/* and /connectors router.

Shopify HTTP calls are mocked with respx; no real network needed.
DB upsert helpers are mocked to isolate connector logic.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from celerp.connectors.base import ConnectorContext, SyncEntity, SyncDirection
from celerp.connectors.shopify import ShopifyConnector, _next_page_url
import celerp.connectors as connector_registry


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx():
    return ConnectorContext(
        company_id="test-company",
        access_token="shpat_test123",
        store_handle="test-store.myshopify.com",
    )


@pytest.fixture
def shopify():
    return ShopifyConnector()


# ── Registry tests ────────────────────────────────────────────────────────────

def test_registry_get_shopify():
    c = connector_registry.get("shopify")
    assert c.name == "shopify"


def test_registry_get_unknown():
    with pytest.raises(KeyError, match="Unknown connector"):
        connector_registry.get("nonexistent")


def test_all_connectors_returns_list():
    result = connector_registry.all_connectors()
    assert len(result) >= 1
    names = [c.name for c in result]
    assert "shopify" in names


# ── ShopifyConnector metadata ─────────────────────────────────────────────────

def test_shopify_metadata(shopify):
    assert shopify.name == "shopify"
    assert shopify.display_name == "Shopify"
    assert SyncEntity.PRODUCTS in shopify.supported_entities
    assert SyncEntity.ORDERS in shopify.supported_entities
    assert SyncEntity.CONTACTS in shopify.supported_entities
    assert shopify.direction == SyncDirection.BIDIRECTIONAL


# ── _next_page_url ────────────────────────────────────────────────────────────

def test_next_page_url_present():
    header = '<https://test.myshopify.com/admin/api/2024-01/products.json?page_info=abc>; rel="next"'
    url = _next_page_url(header)
    assert url == "https://test.myshopify.com/admin/api/2024-01/products.json?page_info=abc"


def test_next_page_url_absent():
    header = '<https://test.myshopify.com/admin/api/2024-01/products.json?page_info=abc>; rel="previous"'
    assert _next_page_url(header) is None


def test_next_page_url_empty():
    assert _next_page_url("") is None


# ── sync_products ─────────────────────────────────────────────────────────────

SHOPIFY_PRODUCTS = {
    "products": [
        {
            "id": 111,
            "title": "Blue Sapphire Ring",
            "variants": [
                {
                    "id": 1001,
                    "sku": "BSR-001",
                    "title": "Default Title",
                    "price": "1200.00",
                    "inventory_quantity": 5,
                },
                {
                    "id": 1002,
                    "sku": "BSR-001-L",
                    "title": "Large",
                    "price": "1250.00",
                    "inventory_quantity": 2,
                },
            ],
        },
        {
            "id": 222,
            "title": "No SKU Product",
            "variants": [
                {
                    "id": 2001,
                    "sku": "",
                    "title": "Default Title",
                    "price": "99.00",
                    "inventory_quantity": 0,
                }
            ],
        },
    ]
}


@pytest.mark.asyncio
@respx.mock
async def test_sync_products_creates_items(shopify, ctx):
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
        return_value=httpx.Response(200, json=SHOPIFY_PRODUCTS)
    )

    with patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=True)):
        result = await shopify.sync_products(ctx)

    assert result.ok
    assert result.created == 2   # 2 variants with SKUs
    assert result.skipped == 1   # 1 variant without SKU
    assert result.entity == SyncEntity.PRODUCTS


@pytest.mark.asyncio
@respx.mock
async def test_sync_products_skips_duplicate(shopify, ctx):
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
        return_value=httpx.Response(200, json=SHOPIFY_PRODUCTS)
    )

    # upsert returns False = already exists
    with patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=False)):
        result = await shopify.sync_products(ctx)

    assert result.created == 0
    assert result.skipped == 3   # 2 dupes + 1 no-sku


@pytest.mark.asyncio
@respx.mock
async def test_sync_products_api_error(shopify, ctx):
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
        return_value=httpx.Response(401, json={"errors": "Invalid API key"})
    )

    result = await shopify.sync_products(ctx)
    assert not result.ok
    assert result.errors
    assert "401" in result.errors[0]


@pytest.mark.asyncio
@respx.mock
async def test_sync_products_variant_name_includes_variant_title(shopify, ctx):
    """Variant title (not 'Default Title') should be appended to product name."""
    products = {
        "products": [{
            "id": 1,
            "title": "Ring",
            "variants": [{"id": 1, "sku": "R-L", "title": "Large", "price": "100", "inventory_quantity": 1}],
        }]
    }
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
        return_value=httpx.Response(200, json=products)
    )

    captured = []

    async def capture_upsert(_company_id, item):
        captured.append(item)
        return True

    with patch("celerp.connectors.upsert.upsert_item", new=capture_upsert):
        await shopify.sync_products(ctx)

    assert len(captured) == 1
    assert "Large" in captured[0].name


# ── sync_orders ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_sync_orders_creates_docs(shopify, ctx):
    orders = {"orders": [{"id": 9001, "name": "#1001", "financial_status": "paid", "line_items": []}]}
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/orders.json").mock(
        return_value=httpx.Response(200, json=orders)
    )

    with patch("celerp.connectors.upsert.upsert_order_from_shopify", new=AsyncMock(return_value=True)):
        result = await shopify.sync_orders(ctx)

    assert result.ok
    assert result.created == 1
    assert result.entity == SyncEntity.ORDERS


# ── sync_contacts ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_sync_contacts(shopify, ctx):
    customers = {"customers": [{"id": 1, "email": "buyer@example.com", "first_name": "Alice", "last_name": "Smith"}]}
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/customers.json").mock(
        return_value=httpx.Response(200, json=customers)
    )

    with patch("celerp.connectors.upsert.upsert_contact_from_shopify", new=AsyncMock(return_value=True)):
        result = await shopify.sync_contacts(ctx)

    assert result.ok
    assert result.created == 1


# ── store_handle validation ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_products_missing_store_handle(shopify):
    ctx_no_handle = ConnectorContext(
        company_id="c",
        access_token="tok",
        store_handle=None,
    )
    result = await shopify.sync_products(ctx_no_handle)
    assert not result.ok
    assert result.errors


# ── /connectors router (HTTP) ─────────────────────────────────────────────────
# Use the shared async `client` + `session` fixtures from conftest.py.
# All connector HTTP endpoints are cloud-gated via require_session_token.
# Tests inject a known token via monkeypatch on settings.gateway_session_token.

_FAKE_SESSION_TOKEN = "test-session-token-abc123"


@pytest.fixture(autouse=False)
def patch_session_token():
    """Inject a known session token into gw_state so require_session_token passes."""
    import celerp.gateway.state as gw_state
    gw_state.set_session_token(_FAKE_SESSION_TOKEN)
    yield
    gw_state.set_session_token("")


@pytest.mark.asyncio
async def test_connectors_require_session_token(client):
    """Without X-Session-Token the connector endpoint returns 401."""
    resp = await client.post("/auth/register", json={
        "email": "connector_gate_test@test.com",
        "password": "testpass123",
        "name": "Test User",
        "company_name": "Gate Test Co",
    })
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    resp = await client.get("/connectors/", headers=headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_connectors(client, patch_session_token):
    resp = await client.post("/auth/register", json={
        "email": "connector_http_test@test.com",
        "password": "testpass123",
        "name": "Test User",
        "company_name": "Connector Test Co",
    })
    assert resp.status_code == 200, resp.text
    headers = {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-Session-Token": _FAKE_SESSION_TOKEN,
    }

    resp = await client.get("/connectors/", headers=headers)
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert "shopify" in names


@pytest.mark.asyncio
async def test_sync_unknown_connector(client, patch_session_token):
    resp = await client.post("/auth/register", json={
        "email": "connector_http_test2@test.com",
        "password": "testpass123",
        "name": "Test User",
        "company_name": "Connector Test Co 2",
    })
    headers = {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-Session-Token": _FAKE_SESSION_TOKEN,
    }

    resp = await client.post("/connectors/nonexistent/sync", headers=headers, json={
        "entity": "products",
        "access_token": "tok",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sync_unsupported_entity(client, patch_session_token):
    resp = await client.post("/auth/register", json={
        "email": "connector_http_test3@test.com",
        "password": "testpass123",
        "name": "Test User",
        "company_name": "Connector Test Co 3",
    })
    headers = {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-Session-Token": _FAKE_SESSION_TOKEN,
    }

    resp = await client.post("/connectors/shopify/sync", headers=headers, json={
        "entity": "invoices",  # not in Shopify.supported_entities
        "access_token": "tok",
    })
    assert resp.status_code == 400


# ── QuickBooks connector tests ────────────────────────────────────────────────

@pytest.fixture
def qb():
    from celerp.connectors.quickbooks import QuickBooksConnector
    return QuickBooksConnector()


@pytest.fixture
def qb_ctx():
    return ConnectorContext(
        company_id="test-company",
        access_token="qb_access_token_test",
        store_handle="1234567890",  # realmId
    )


def test_quickbooks_metadata(qb):
    assert qb.name == "quickbooks"
    assert qb.display_name == "QuickBooks"
    assert SyncEntity.PRODUCTS in qb.supported_entities
    assert SyncEntity.ORDERS in qb.supported_entities
    assert SyncEntity.CONTACTS in qb.supported_entities
    assert qb.direction == SyncDirection.BIDIRECTIONAL


def test_registry_get_quickbooks():
    c = connector_registry.get("quickbooks")
    assert c.name == "quickbooks"


@pytest.mark.asyncio
@respx.mock
async def test_quickbooks_sync_products_success(qb, qb_ctx):
    """sync_products creates items for Inventory/Service QB items."""
    mock_response = {
        "QueryResponse": {
            "Item": [
                {"Id": "1", "Name": "Widget", "Sku": "WGT-001", "Type": "Inventory",
                 "UnitPrice": 25.00, "PurchaseCost": 10.00},
                {"Id": "2", "Name": "Consulting", "Sku": "", "Type": "Service",
                 "UnitPrice": 150.00},
            ]
        }
    }
    respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    with patch("celerp.connectors.upsert.upsert_item", new_callable=AsyncMock) as mock_upsert:
        mock_upsert.return_value = True
        result = await qb.sync_products(qb_ctx)

    # Widget has explicit SKU. Consulting has no Sku but has a Name - Name is used as SKU fallback.
    assert result.created == 2
    assert result.skipped == 0
    assert result.ok


@pytest.mark.asyncio
@respx.mock
async def test_quickbooks_sync_products_api_error(qb, qb_ctx):
    """API error is captured in result.errors."""
    respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized"})
    )
    result = await qb.sync_products(qb_ctx)
    assert not result.ok
    assert result.errors


@pytest.mark.asyncio
@respx.mock
async def test_quickbooks_sync_orders(qb, qb_ctx):
    """sync_orders processes invoices."""
    mock_response = {
        "QueryResponse": {
            "Invoice": [
                {"Id": "100", "DocNumber": "INV-001",
                 "CustomerRef": {"name": "ACME Corp"},
                 "TotalAmt": 500.00, "Balance": 500.00},
            ]
        }
    }
    respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
        return_value=httpx.Response(200, json=mock_response)
    )
    with patch("celerp.connectors.upsert.upsert_invoice_from_quickbooks", new_callable=AsyncMock) as mock_up:
        mock_up.return_value = True
        result = await qb.sync_orders(qb_ctx)
    assert result.created == 1
    assert result.ok


@pytest.mark.asyncio
@respx.mock
async def test_quickbooks_sync_contacts(qb, qb_ctx):
    """sync_contacts processes customers."""
    mock_response = {
        "QueryResponse": {
            "Customer": [
                {"Id": "200", "DisplayName": "ACME Corp", "PrimaryEmailAddr": {"Address": "acme@example.com"}},
            ]
        }
    }
    respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
        return_value=httpx.Response(200, json=mock_response)
    )
    with patch("celerp.connectors.upsert.upsert_contact_from_quickbooks", new_callable=AsyncMock) as mock_up:
        mock_up.return_value = True
        result = await qb.sync_contacts(qb_ctx)
    assert result.created == 1
    assert result.ok


def test_quickbooks_requires_realm_id(qb):
    """ConnectorContext without store_handle raises ValueError in _base_url."""
    from celerp.connectors.quickbooks import _base_url
    ctx_no_realm = ConnectorContext(company_id="c", access_token="tok", store_handle=None)
    with pytest.raises(ValueError, match="realmId"):
        _base_url(ctx_no_realm)


# ── Xero connector tests ──────────────────────────────────────────────────────

@pytest.fixture
def xero():
    from celerp.connectors.xero import XeroConnector
    return XeroConnector()


@pytest.fixture
def xero_ctx():
    return ConnectorContext(
        company_id="test-company",
        access_token="xero_access_token_test",
        store_handle="tenant-uuid-1234",  # Xero tenant_id
    )


def test_xero_metadata(xero):
    assert xero.name == "xero"
    assert xero.display_name == "Xero"
    assert SyncEntity.PRODUCTS in xero.supported_entities
    assert SyncEntity.ORDERS in xero.supported_entities
    assert SyncEntity.CONTACTS in xero.supported_entities
    assert xero.direction == SyncDirection.BIDIRECTIONAL


def test_registry_get_xero():
    c = connector_registry.get("xero")
    assert c.name == "xero"


def test_all_connectors_includes_qb_and_xero():
    names = [c.name for c in connector_registry.all_connectors()]
    assert "quickbooks" in names
    assert "xero" in names


@pytest.mark.asyncio
@respx.mock
async def test_xero_sync_products_success(xero, xero_ctx):
    """sync_products creates items for Xero Items with a Code."""
    respx.get("https://api.xero.com/api.xro/2.0/Items").mock(
        return_value=httpx.Response(200, json={
            "Items": [
                {"ItemID": "aaa-111", "Code": "WIDGET-001", "Name": "Widget",
                 "SalesDetails": {"UnitPrice": 30.00},
                 "PurchaseDetails": {"UnitPrice": 12.00}},
                {"ItemID": "bbb-222", "Code": "", "Name": "No-Code Item"},
            ]
        })
    )
    with patch("celerp.connectors.upsert.upsert_item", new_callable=AsyncMock) as mock_up:
        mock_up.return_value = True
        result = await xero.sync_products(xero_ctx)
    assert result.created == 1
    assert result.skipped == 1
    assert result.ok


@pytest.mark.asyncio
@respx.mock
async def test_xero_sync_products_api_error(xero, xero_ctx):
    respx.get("https://api.xero.com/api.xro/2.0/Items").mock(
        return_value=httpx.Response(403, json={"Type": "ValidationException"})
    )
    result = await xero.sync_products(xero_ctx)
    assert not result.ok
    assert result.errors


@pytest.mark.asyncio
@respx.mock
async def test_xero_sync_orders_filters_non_accrec(xero, xero_ctx):
    """sync_orders only imports ACCREC (sales) invoices, skips ACCPAY (bills)."""
    respx.get("https://api.xero.com/api.xro/2.0/Invoices").mock(
        return_value=httpx.Response(200, json={
            "Invoices": [
                {"InvoiceID": "inv-1", "Type": "ACCREC", "InvoiceNumber": "INV-001",
                 "Contact": {"Name": "ACME"}, "Total": 100.0},
                {"InvoiceID": "inv-2", "Type": "ACCPAY", "InvoiceNumber": "BILL-001",
                 "Contact": {"Name": "Vendor"}, "Total": 50.0},
            ]
        })
    )
    with patch("celerp.connectors.upsert.upsert_invoice_from_xero", new_callable=AsyncMock) as mock_up:
        mock_up.return_value = True
        result = await xero.sync_orders(xero_ctx)
    assert result.created == 1
    assert result.skipped == 1
    assert result.ok


@pytest.mark.asyncio
@respx.mock
async def test_xero_sync_contacts(xero, xero_ctx):
    respx.get("https://api.xero.com/api.xro/2.0/Contacts").mock(
        return_value=httpx.Response(200, json={
            "Contacts": [
                {"ContactID": "c-1", "Name": "ACME Corp", "EmailAddress": "acme@example.com"},
            ]
        })
    )
    with patch("celerp.connectors.upsert.upsert_contact_from_xero", new_callable=AsyncMock) as mock_up:
        mock_up.return_value = True
        result = await xero.sync_contacts(xero_ctx)
    assert result.created == 1
    assert result.ok


@pytest.mark.asyncio
async def test_xero_has_sync_invoices_out(xero, xero_ctx):
    """Xero implements sync_invoices_out (not NotImplementedError)."""
    assert hasattr(xero, "sync_invoices_out")
    # Method is implemented (not the base stub)
    from celerp.connectors.base import ConnectorBase
    assert type(xero).sync_invoices_out is not ConnectorBase.sync_invoices_out

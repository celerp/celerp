# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Tests for celerp/connectors/* - comprehensive coverage.

Covers: base enums, direction filtering, registry, sync_runner with direction,
webhook registration/validation, outbound queue, daily scheduler,
per-platform sync (Shopify, QB, Xero, WC), and HTTP endpoints.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

from celerp.connectors.base import (
    ConnectorContext,
    SyncEntity,
    SyncDirection,
    SyncFrequency,
    ConnectorCategory,
    entity_allowed,
)
from celerp.connectors.shopify import ShopifyConnector, _next_page_url
from celerp.connectors.webhooks import WebhookEvent, topic_to_entity, handle_webhook
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


# ── Base: SyncDirection enum ──────────────────────────────────────────────────

def test_sync_direction_values():
    assert SyncDirection.INBOUND.value == "inbound"
    assert SyncDirection.OUTBOUND.value == "outbound"
    assert SyncDirection.BOTH.value == "both"


def test_sync_frequency_values():
    assert SyncFrequency.REALTIME.value == "realtime"
    assert SyncFrequency.MANUAL.value == "manual"
    assert SyncFrequency.DAILY.value == "daily"


def test_connector_category_values():
    assert ConnectorCategory.WEBSITE.value == "website"
    assert ConnectorCategory.ACCOUNTING.value == "accounting"


# ── entity_allowed ────────────────────────────────────────────────────────────

def test_entity_allowed_both_allows_all():
    assert entity_allowed("products", SyncDirection.BOTH)
    assert entity_allowed("products_out", SyncDirection.BOTH)
    assert entity_allowed("invoices_out", SyncDirection.BOTH)


def test_entity_allowed_inbound_blocks_outbound():
    assert entity_allowed("products", SyncDirection.INBOUND)
    assert entity_allowed("orders", SyncDirection.INBOUND)
    assert not entity_allowed("products_out", SyncDirection.INBOUND)
    assert not entity_allowed("invoices_out", SyncDirection.INBOUND)
    assert not entity_allowed("inventory_out", SyncDirection.INBOUND)


def test_entity_allowed_outbound_blocks_inbound():
    assert not entity_allowed("products", SyncDirection.OUTBOUND)
    assert not entity_allowed("orders", SyncDirection.OUTBOUND)
    assert entity_allowed("products_out", SyncDirection.OUTBOUND)
    assert entity_allowed("invoices_out", SyncDirection.OUTBOUND)
    assert entity_allowed("inventory_out", SyncDirection.OUTBOUND)


# ── Registry tests ────────────────────────────────────────────────────────────

def test_registry_get_shopify():
    c = connector_registry.get("shopify")
    assert c.name == "shopify"


def test_registry_get_unknown():
    with pytest.raises(KeyError, match="Unknown connector"):
        connector_registry.get("nonexistent")


def test_all_connectors_returns_list():
    result = connector_registry.all_connectors()
    assert len(result) >= 4
    names = [c.name for c in result]
    assert "shopify" in names
    assert "woocommerce" in names
    assert "quickbooks" in names
    assert "xero" in names


# ── ShopifyConnector metadata ─────────────────────────────────────────────────

def test_shopify_metadata(shopify):
    assert shopify.name == "shopify"
    assert shopify.display_name == "Shopify"
    assert SyncEntity.PRODUCTS in shopify.supported_entities
    assert SyncEntity.ORDERS in shopify.supported_entities
    assert SyncEntity.CONTACTS in shopify.supported_entities
    assert shopify.direction == SyncDirection.BOTH
    assert shopify.category == ConnectorCategory.WEBSITE


# ── Shopify webhook support ──────────────────────────────────────────────────

def test_shopify_webhook_topics_both(shopify):
    topics = shopify.webhook_topics_for_direction(SyncDirection.BOTH)
    assert "products/create" in topics
    assert "orders/create" in topics
    assert len(topics) == 8


def test_shopify_webhook_topics_outbound_empty(shopify):
    topics = shopify.webhook_topics_for_direction(SyncDirection.OUTBOUND)
    assert topics == []


def test_shopify_webhook_topics_inbound(shopify):
    topics = shopify.webhook_topics_for_direction(SyncDirection.INBOUND)
    assert len(topics) == 8


def test_shopify_validate_webhook(shopify):
    import hmac as _hmac
    import hashlib
    import base64
    secret = "test_secret_123"
    payload = b'{"id": 123}'
    computed = _hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    signature = base64.b64encode(computed).decode()
    assert shopify.validate_webhook(payload, signature, secret)


def test_shopify_validate_webhook_bad_signature(shopify):
    assert not shopify.validate_webhook(b'{"id": 123}', "bad_sig", "secret")


@pytest.mark.asyncio
@respx.mock
async def test_shopify_register_webhooks(shopify, ctx):
    url = "https://test-store.myshopify.com/admin/api/2024-01/webhooks.json"
    respx.post(url).mock(
        return_value=httpx.Response(201, json={"webhook": {"id": 12345}})
    )
    ids = await shopify.register_webhooks(ctx, "https://relay.example.com/webhooks/shopify/inst1")
    assert len(ids) == 8
    assert "12345" in ids


@pytest.mark.asyncio
@respx.mock
async def test_shopify_deregister_webhooks(shopify, ctx):
    respx.delete("https://test-store.myshopify.com/admin/api/2024-01/webhooks/111.json").mock(
        return_value=httpx.Response(200)
    )
    respx.delete("https://test-store.myshopify.com/admin/api/2024-01/webhooks/222.json").mock(
        return_value=httpx.Response(204)
    )
    await shopify.deregister_webhooks(ctx, ["111", "222"])  # no exception = success


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
                {"id": 1001, "sku": "BSR-001", "title": "Default Title", "price": "1200.00", "inventory_quantity": 5},
                {"id": 1002, "sku": "BSR-001-L", "title": "Large", "price": "1250.00", "inventory_quantity": 2},
            ],
        },
        {
            "id": 222,
            "title": "No SKU Product",
            "variants": [{"id": 2001, "sku": "", "title": "Default Title", "price": "99.00", "inventory_quantity": 0}],
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
    assert result.created == 2
    assert result.skipped == 1
    assert result.entity == SyncEntity.PRODUCTS


@pytest.mark.asyncio
@respx.mock
async def test_sync_products_skips_duplicate(shopify, ctx):
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
        return_value=httpx.Response(200, json=SHOPIFY_PRODUCTS)
    )
    with patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=False)):
        result = await shopify.sync_products(ctx)
    assert result.created == 0
    assert result.skipped == 3


@pytest.mark.asyncio
@respx.mock
async def test_sync_products_api_error(shopify, ctx):
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
        return_value=httpx.Response(401, json={"errors": "Invalid API key"})
    )
    result = await shopify.sync_products(ctx)
    assert not result.ok
    assert "401" in result.errors[0]


@pytest.mark.asyncio
@respx.mock
async def test_sync_products_variant_name_includes_variant_title(shopify, ctx):
    products = {
        "products": [{
            "id": 1, "title": "Ring",
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
    ctx_no_handle = ConnectorContext(company_id="c", access_token="tok", store_handle=None)
    result = await shopify.sync_products(ctx_no_handle)
    assert not result.ok
    assert result.errors


# ── sync_runner with direction filtering ──────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_runner_blocks_outbound_when_inbound():
    from celerp.connectors.sync_runner import run_sync
    shopify = ShopifyConnector()
    ctx = ConnectorContext(company_id="test", access_token="tok", store_handle="test.myshopify.com")
    result = await run_sync(shopify, ctx, "products_out", direction=SyncDirection.INBOUND)
    assert not result.ok
    assert "blocked" in result.errors[0]


@pytest.mark.asyncio
async def test_sync_runner_blocks_inbound_when_outbound():
    from celerp.connectors.sync_runner import run_sync
    shopify = ShopifyConnector()
    ctx = ConnectorContext(company_id="test", access_token="tok", store_handle="test.myshopify.com")
    result = await run_sync(shopify, ctx, "products", direction=SyncDirection.OUTBOUND)
    assert not result.ok
    assert "blocked" in result.errors[0]


@pytest.mark.asyncio
@respx.mock
async def test_sync_runner_allows_inbound_when_both():
    from celerp.connectors.sync_runner import run_sync
    shopify = ShopifyConnector()
    ctx = ConnectorContext(company_id="test", access_token="tok", store_handle="test-store.myshopify.com")
    respx.get("https://test-store.myshopify.com/admin/api/2024-01/products.json").mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    with patch("celerp.db.get_session_ctx") as mock_db:
        mock_session = AsyncMock()
        mock_db.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_db.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await run_sync(shopify, ctx, "products", direction=SyncDirection.BOTH)
    assert result.ok


@pytest.mark.asyncio
async def test_sync_runner_no_direction_runs_all():
    """When direction=None, sync_runner does not filter."""
    from celerp.connectors.sync_runner import run_sync
    shopify = ShopifyConnector()
    ctx = ConnectorContext(company_id="test", access_token="tok", store_handle="test-store.myshopify.com")
    # products_out will raise NotImplementedError for missing items, but it won't be direction-blocked
    with patch("celerp.db.get_session_ctx") as mock_db:
        mock_session = AsyncMock()
        mock_db.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_db.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await run_sync(shopify, ctx, "products_out", direction=None)
    # Should not be blocked by direction - it will fail for other reasons (no items)
    assert "blocked" not in str(result.errors or [])


# ── Webhook: topic_to_entity ─────────────────────────────────────────────────

def test_topic_to_entity_shopify_products():
    assert topic_to_entity("products/create") == "products"
    assert topic_to_entity("products/update") == "products"
    assert topic_to_entity("products/delete") == "products"


def test_topic_to_entity_shopify_orders():
    assert topic_to_entity("orders/create") == "orders"
    assert topic_to_entity("orders/updated") == "orders"


def test_topic_to_entity_shopify_customers():
    assert topic_to_entity("customers/create") == "contacts"
    assert topic_to_entity("customers/update") == "contacts"


def test_topic_to_entity_shopify_inventory():
    assert topic_to_entity("inventory_levels/update") == "inventory"


def test_topic_to_entity_wc_product():
    assert topic_to_entity("product.created") == "products"
    assert topic_to_entity("product.updated") == "products"


def test_topic_to_entity_wc_order():
    assert topic_to_entity("order.created") == "orders"


def test_topic_to_entity_wc_customer():
    assert topic_to_entity("customer.created") == "contacts"


def test_topic_to_entity_unknown():
    assert topic_to_entity("refund/created") is None


# ── Webhook: handle_webhook ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_webhook_respects_direction():
    """Webhook for products should be blocked when direction is outbound."""
    event = WebhookEvent(platform="shopify", topic="products/update", entity_id="123")
    ctx = ConnectorContext(company_id="test", access_token="tok", store_handle="test.myshopify.com")
    with patch("celerp.connectors.webhooks.run_sync", new=AsyncMock()) as mock_sync:
        await handle_webhook(event, ctx, direction=SyncDirection.OUTBOUND)
        mock_sync.assert_not_called()


@pytest.mark.asyncio
async def test_handle_webhook_runs_sync_for_inbound():
    event = WebhookEvent(platform="shopify", topic="products/update", entity_id="123")
    ctx = ConnectorContext(company_id="test", access_token="tok", store_handle="test.myshopify.com")
    with patch("celerp.connectors.webhooks.run_sync", new=AsyncMock()) as mock_sync:
        await handle_webhook(event, ctx, direction=SyncDirection.BOTH)
        mock_sync.assert_called_once()


@pytest.mark.asyncio
async def test_handle_webhook_unknown_topic():
    event = WebhookEvent(platform="shopify", topic="refund/created")
    ctx = ConnectorContext(company_id="test", access_token="tok")
    with patch("celerp.connectors.webhooks.run_sync", new=AsyncMock()) as mock_sync:
        await handle_webhook(event, ctx)
        mock_sync.assert_not_called()


@pytest.mark.asyncio
async def test_handle_webhook_unknown_platform():
    event = WebhookEvent(platform="unknown_platform", topic="products/update")
    ctx = ConnectorContext(company_id="test", access_token="tok")
    with patch("celerp.connectors.webhooks.run_sync", new=AsyncMock()) as mock_sync:
        await handle_webhook(event, ctx)
        mock_sync.assert_not_called()


# ── OutboundQueue model ──────────────────────────────────────────────────────

def test_outbound_queue_backoff_minutes():
    from celerp.connectors.outbound_queue import MAX_RETRIES, BACKOFF_MINUTES
    assert MAX_RETRIES == 5
    assert len(BACKOFF_MINUTES) == 5
    assert BACKOFF_MINUTES[0] == 1
    assert BACKOFF_MINUTES[-1] == 240


# ── ConnectorConfig model ────────────────────────────────────────────────────

def test_connector_config_webhook_ids():
    from celerp.models.connector_config import ConnectorConfig
    config = ConnectorConfig(
        company_id="test",
        connector="shopify",
        direction="both",
        sync_frequency="realtime",
        daily_sync_hour=2,
    )
    assert config.webhook_ids == []
    config.webhook_ids = ["111", "222"]
    assert config.webhook_ids_json == '["111", "222"]'
    assert config.webhook_ids == ["111", "222"]
    config.webhook_ids = []
    assert config.webhook_ids_json is None


# ── /connectors router (HTTP) ─────────────────────────────────────────────────

_FAKE_SESSION_TOKEN = "test-session-token-abc123"


@pytest.fixture(autouse=False)
def patch_session_token():
    import celerp.gateway.state as gw_state
    gw_state.set_session_token(_FAKE_SESSION_TOKEN)
    yield
    gw_state.set_session_token("")


@pytest.mark.asyncio
async def test_connectors_require_session_token(client):
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
        "entity": "invoices",
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
        store_handle="1234567890",
    )


def test_quickbooks_metadata(qb):
    assert qb.name == "quickbooks"
    assert qb.display_name == "QuickBooks"
    assert SyncEntity.PRODUCTS in qb.supported_entities
    assert SyncEntity.INVOICES in qb.supported_entities
    assert qb.direction == SyncDirection.BOTH
    assert qb.category == ConnectorCategory.ACCOUNTING


def test_quickbooks_no_webhook_support(qb):
    """Accounting connectors should not have webhook topics."""
    topics = qb.webhook_topics_for_direction(SyncDirection.BOTH)
    assert topics == []


def test_registry_get_quickbooks():
    c = connector_registry.get("quickbooks")
    assert c.name == "quickbooks"


@pytest.mark.asyncio
@respx.mock
async def test_quickbooks_sync_products_success(qb, qb_ctx):
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
    assert result.created == 2
    assert result.ok


@pytest.mark.asyncio
@respx.mock
async def test_quickbooks_sync_products_api_error(qb, qb_ctx):
    respx.get("https://quickbooks.api.intuit.com/v3/company/1234567890/query").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized"})
    )
    result = await qb.sync_products(qb_ctx)
    assert not result.ok


@pytest.mark.asyncio
@respx.mock
async def test_quickbooks_sync_orders(qb, qb_ctx):
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
        store_handle="tenant-uuid-1234",
    )


def test_xero_metadata(xero):
    assert xero.name == "xero"
    assert xero.display_name == "Xero"
    assert SyncEntity.INVOICES in xero.supported_entities
    assert xero.direction == SyncDirection.BOTH
    assert xero.category == ConnectorCategory.ACCOUNTING


def test_xero_no_webhook_support(xero):
    topics = xero.webhook_topics_for_direction(SyncDirection.BOTH)
    assert topics == []


def test_registry_get_xero():
    c = connector_registry.get("xero")
    assert c.name == "xero"


def test_all_connectors_includes_all_four():
    names = [c.name for c in connector_registry.all_connectors()]
    assert "quickbooks" in names
    assert "xero" in names
    assert "shopify" in names
    assert "woocommerce" in names


@pytest.mark.asyncio
@respx.mock
async def test_xero_sync_products_success(xero, xero_ctx):
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
async def test_xero_sync_orders_filters_non_accrec(xero, xero_ctx):
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
async def test_xero_has_sync_invoices_out(xero):
    from celerp.connectors.base import ConnectorBase
    assert type(xero).sync_invoices_out is not ConnectorBase.sync_invoices_out


# ── WooCommerce connector tests ──────────────────────────────────────────────

@pytest.fixture
def wc():
    from celerp.connectors.woocommerce import WooCommerceConnector
    return WooCommerceConnector()


@pytest.fixture
def wc_ctx():
    return ConnectorContext(
        company_id="test-company",
        access_token="ck_test123:cs_test456",
        store_handle="https://mystore.example.com",
    )


def test_woocommerce_metadata(wc):
    assert wc.name == "woocommerce"
    assert wc.display_name == "WooCommerce"
    assert wc.direction == SyncDirection.BOTH
    assert wc.category == ConnectorCategory.WEBSITE
    assert SyncEntity.PRODUCTS in wc.supported_entities
    assert SyncEntity.INVENTORY in wc.supported_entities


def test_woocommerce_webhook_topics(wc):
    topics = wc.webhook_topics_for_direction(SyncDirection.BOTH)
    assert "product.created" in topics
    assert "order.created" in topics
    assert len(topics) == 7


def test_woocommerce_webhook_topics_outbound_empty(wc):
    assert wc.webhook_topics_for_direction(SyncDirection.OUTBOUND) == []


def test_woocommerce_validate_webhook(wc):
    import hmac as _hmac
    import hashlib
    import base64
    secret = "wc_webhook_secret"
    payload = b'{"id": 456}'
    computed = _hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    signature = base64.b64encode(computed).decode()
    assert wc.validate_webhook(payload, signature, secret)


def test_woocommerce_requires_store_handle():
    from celerp.connectors.woocommerce import _base_url
    ctx = ConnectorContext(company_id="c", access_token="k:s", store_handle=None)
    with pytest.raises(ValueError, match="store_handle"):
        _base_url(ctx)


def test_woocommerce_requires_colon_in_token():
    from celerp.connectors.woocommerce import _auth
    ctx = ConnectorContext(company_id="c", access_token="no_colon", store_handle="https://x.com")
    with pytest.raises(ValueError, match="consumer_key:consumer_secret"):
        _auth(ctx)


@pytest.mark.asyncio
@respx.mock
async def test_woocommerce_sync_products(wc, wc_ctx):
    respx.get("https://mystore.example.com/wp-json/wc/v3/products").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "sku": "WC-PROD-1", "name": "T-Shirt", "regular_price": "29.99"},
            {"id": 2, "sku": "", "name": "No SKU Item", "regular_price": "10.00"},
        ], headers={"X-WP-TotalPages": "1"})
    )
    with patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=True)):
        result = await wc.sync_products(wc_ctx)
    assert result.ok
    assert result.created == 2  # both get SKUs (second gets WC-2 fallback)


@pytest.mark.asyncio
@respx.mock
async def test_woocommerce_sync_orders(wc, wc_ctx):
    respx.get("https://mystore.example.com/wp-json/wc/v3/orders").mock(
        return_value=httpx.Response(200, json=[
            {"id": 100, "status": "processing", "line_items": []},
        ], headers={"X-WP-TotalPages": "1"})
    )
    with patch("celerp.connectors.upsert.upsert_order_from_woocommerce", new=AsyncMock(return_value=True)):
        result = await wc.sync_orders(wc_ctx)
    assert result.ok
    assert result.created == 1


@pytest.mark.asyncio
@respx.mock
async def test_woocommerce_sync_contacts(wc, wc_ctx):
    respx.get("https://mystore.example.com/wp-json/wc/v3/customers").mock(
        return_value=httpx.Response(200, json=[
            {"id": 50, "email": "customer@example.com", "first_name": "Bob", "last_name": "Jones"},
        ], headers={"X-WP-TotalPages": "1"})
    )
    with patch("celerp.connectors.upsert.upsert_contact_from_woocommerce", new=AsyncMock(return_value=True)):
        result = await wc.sync_contacts(wc_ctx)
    assert result.ok
    assert result.created == 1


@pytest.mark.asyncio
@respx.mock
async def test_woocommerce_sync_products_api_error(wc, wc_ctx):
    respx.get("https://mystore.example.com/wp-json/wc/v3/products").mock(
        return_value=httpx.Response(403, json={"code": "woocommerce_rest_cannot_view"})
    )
    result = await wc.sync_products(wc_ctx)
    assert not result.ok


@pytest.mark.asyncio
@respx.mock
async def test_woocommerce_register_webhooks(wc, wc_ctx):
    respx.post("https://mystore.example.com/wp-json/wc/v3/webhooks").mock(
        return_value=httpx.Response(201, json={"id": 789})
    )
    ids = await wc.register_webhooks(wc_ctx, "https://relay.example.com/webhooks/woocommerce/inst1")
    assert len(ids) == 7
    assert "789" in ids


@pytest.mark.asyncio
@respx.mock
async def test_woocommerce_deregister_webhooks(wc, wc_ctx):
    respx.delete("https://mystore.example.com/wp-json/wc/v3/webhooks/789").mock(
        return_value=httpx.Response(200)
    )
    await wc.deregister_webhooks(wc_ctx, ["789"])


# ── RateLimitedClient tests ──────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_client_retries_429():
    from celerp.connectors.http import RateLimitedClient
    route = respx.get("https://api.example.com/test")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0.01"}),
        httpx.Response(200, json={"ok": True}),
    ]
    async with RateLimitedClient(max_retries=3, backoff_base=0.01) as client:
        resp = await client.get("https://api.example.com/test")
    assert resp.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_client_gives_up_after_max_retries():
    from celerp.connectors.http import RateLimitedClient
    respx.get("https://api.example.com/test").mock(
        return_value=httpx.Response(429)
    )
    async with RateLimitedClient(max_retries=1, backoff_base=0.01) as client:
        resp = await client.get("https://api.example.com/test")
    assert resp.status_code == 429


# ── Daily scheduler unit tests ───────────────────────────────────────────────

def test_daily_scheduler_min_hours():
    from celerp.connectors.daily_scheduler import _MIN_HOURS_BETWEEN_SYNCS
    assert _MIN_HOURS_BETWEEN_SYNCS == 23


def test_daily_scheduler_check_interval():
    from celerp.connectors.daily_scheduler import _CHECK_INTERVAL_SECONDS
    assert _CHECK_INTERVAL_SECONDS == 3600

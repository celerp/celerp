# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Integration guard for the shared connector upsert layer (celerp/connectors/upsert.py).

These tests drive the REAL upsert shims against a REAL database — they do NOT
mock `celerp.connectors.upsert.*` the way the connector unit tests do. That mock
is exactly what hid a class of bugs where the shim imported a non-existent module
(`celerp.services.docs`) or delegated to a function that was never written: every
connector test was green while order/contact/invoice import raised at runtime.

The service functions open their own `celerp.db.SessionLocal`, so we point that at
the test's savepoint-isolated session (commits become savepoints, rolled back at
teardown) and seed a real company so the ledger FK is satisfied.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import text

from celerp.models.company import Company
import celerp.connectors.upsert as u


async def _seed_company(session, name: str) -> uuid.UUID:
    cid = uuid.uuid4()
    session.add(Company(id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}", settings={}))
    await session.flush()
    return cid


@pytest.fixture
def use_test_session(session, monkeypatch):
    """Make the connector services' own SessionLocal resolve to the test session."""
    @contextlib.asynccontextmanager
    async def _fake_sessionlocal():
        yield session
    monkeypatch.setattr("celerp.db.SessionLocal", _fake_sessionlocal)
    return session


async def _ledger_rows(session, cid, idem_key) -> int:
    return await session.scalar(text(
        "SELECT count(*) FROM ledger WHERE company_id = :c AND idempotency_key = :k"
    ), {"c": cid, "k": idem_key})


async def _state(session, cid, idem_key) -> dict:
    """The projection state for the entity created under this idempotency key."""
    row = (await session.execute(text(
        "SELECT entity_id FROM ledger WHERE company_id = :c AND idempotency_key = :k"
    ), {"c": cid, "k": idem_key})).first()
    st = await session.scalar(text(
        "SELECT state FROM projections WHERE company_id = :c AND entity_id = :e"
    ), {"c": cid, "e": row[0]})
    return st or {}


# ── Orders / invoices ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_woocommerce_order_creates_doc(use_test_session):
    session = use_test_session
    cid = await _seed_company(session, "WooOrd")
    order = {
        "id": 55, "number": "1001", "status": "completed",
        "line_items": [{"name": "Widget", "quantity": 2, "price": "5.00", "total": "10.00"}],
        "total": "10.00",
    }
    assert await u.upsert_order_from_woocommerce(str(cid), order) is True
    assert await u.upsert_order_from_woocommerce(str(cid), order) is False  # dedup
    assert await _ledger_rows(session, cid, "woocommerce:order:55") == 1
    st = await _state(session, cid, "woocommerce:order:55")
    assert st["doc_type"] == "invoice"
    assert st["status"] == "closed"           # completed -> closed
    assert st["total"] == 10.0
    assert st["line_items"][0]["unit_price"] == 5.0
    assert st["woocommerce_order_id"] == "55"


@pytest.mark.asyncio
async def test_quickbooks_invoice_creates_doc(use_test_session):
    session = use_test_session
    cid = await _seed_company(session, "QbInv")
    inv = {
        "Id": "77", "DocNumber": "INV-77", "Balance": 0,
        "TotalAmt": 30.0,
        "Line": [
            {"DetailType": "SalesItemLineDetail", "Amount": 30.0, "Description": "Service",
             "SalesItemLineDetail": {"Qty": 3, "UnitPrice": 10.0}},
            {"DetailType": "SubTotalLineDetail", "Amount": 30.0},  # must be skipped
        ],
    }
    assert await u.upsert_invoice_from_quickbooks(str(cid), inv) is True
    st = await _state(session, cid, "quickbooks:invoice:77")
    assert st["status"] == "closed"           # Balance 0 -> closed
    assert len(st["line_items"]) == 1         # subtotal row skipped
    assert st["line_items"][0]["quantity"] == 3.0
    assert st["quickbooks_invoice_id"] == "77"


@pytest.mark.asyncio
async def test_xero_invoice_creates_doc(use_test_session):
    session = use_test_session
    cid = await _seed_company(session, "XeroInv")
    inv = {
        "InvoiceID": "abc-123", "InvoiceNumber": "X-1", "Status": "AUTHORISED",
        "Total": 42.0, "AmountDue": 42.0,
        "LineItems": [{"Description": "Item", "Quantity": 1, "UnitAmount": 42.0, "LineAmount": 42.0}],
    }
    assert await u.upsert_invoice_from_xero(str(cid), inv) is True
    st = await _state(session, cid, "xero:invoice:abc-123")
    assert st["status"] == "open"             # AUTHORISED (not PAID) -> open
    assert st["amount_outstanding"] == 42.0
    assert st["xero_invoice_id"] == "abc-123"


@pytest.mark.asyncio
async def test_shopify_order_still_works_after_import_fix(use_test_session):
    """Regression: the shim now points at celerp_docs.doc_service, not the
    non-existent celerp.services.docs."""
    session = use_test_session
    cid = await _seed_company(session, "ShopOrd")
    order = {"id": 9001, "name": "#1001", "financial_status": "paid",
             "line_items": [{"title": "T", "quantity": 1, "price": "3.00"}], "total_price": "3.00"}
    assert await u.upsert_order_from_shopify(str(cid), order) is True
    assert await _ledger_rows(session, cid, "shopify:order:9001") == 1


# ── Contacts ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_contacts_create_for_all_platforms(use_test_session):
    session = use_test_session
    cid = await _seed_company(session, "Contacts")

    assert await u.upsert_contact_from_woocommerce(str(cid), {
        "id": 1, "first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.test",
        "billing": {"phone": "123", "city": "London", "country": "GB"}}) is True
    assert await u.upsert_contact_from_quickbooks(str(cid), {
        "Id": "2", "DisplayName": "Bob Co", "PrimaryEmailAddr": {"Address": "bob@x.test"},
        "PrimaryPhone": {"FreeFormNumber": "456"}, "BillAddr": {"City": "NYC", "Country": "US"}}) is True
    assert await u.upsert_contact_from_xero(str(cid), {
        "ContactID": "x-3", "Name": "Carol", "EmailAddress": "carol@x.test",
        "Phones": [{"PhoneType": "DEFAULT", "PhoneNumber": "789"}],
        "Addresses": [{"City": "Sydney", "Country": "AU"}]}) is True
    assert await u.upsert_contact_from_shopify(str(cid), {
        "id": 4, "first_name": "Dan", "email": "dan@x.test", "addresses": [{"city": "LA"}]}) is True

    for key in ("woocommerce:customer:1", "quickbooks:customer:2", "xero:contact:x-3", "shopify:customer:4"):
        assert await _ledger_rows(session, cid, key) == 1

    st = await _state(session, cid, "xero:contact:x-3")
    assert st["name"] == "Carol"
    assert st["phone"] == "789"
    assert st["attributes"]["country"] == "AU"


@pytest.mark.asyncio
async def test_contact_dedup_is_per_company(use_test_session):
    """Two companies importing the same WooCommerce customer id both succeed."""
    session = use_test_session
    a = await _seed_company(session, "CoA")
    b = await _seed_company(session, "CoB")
    cust = {"id": 99, "first_name": "Same", "email": "same@x.test", "billing": {}}
    assert await u.upsert_contact_from_woocommerce(str(a), cust) is True
    assert await u.upsert_contact_from_woocommerce(str(b), cust) is True   # NOT blocked by A
    assert await u.upsert_contact_from_woocommerce(str(a), cust) is False  # A's own dedup
    assert await _ledger_rows(session, a, "woocommerce:customer:99") == 1
    assert await _ledger_rows(session, b, "woocommerce:customer:99") == 1


# ── Outbound list helpers ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_items_with_external_id_parses_ids(use_test_session):
    session = use_test_session
    cid = await _seed_company(session, "OutItems")
    from celerp_inventory.routes import ItemCreate
    await u.upsert_item(str(cid), ItemCreate(
        sku="S1", name="Shop Item", sell_by="piece", sale_price=12.0,
        idempotency_key="shopify:111:222"))
    await u.upsert_item(str(cid), ItemCreate(
        sku="W1", name="Woo Item", sell_by="piece", sale_price=8.0,
        idempotency_key="woocommerce:333"))

    # Shopify outbound is opt-in per item: enable sync on the linked item so it is
    # eligible for outbound push (inbound import alone does not enable outbound).
    import sqlalchemy as sa
    from celerp.models.projections import Projection
    await session.execute(
        sa.update(Projection).where(
            Projection.company_id == cid,
            Projection.state["idempotency_key"].as_string().like("shopify:%"),
        ).values(is_sync_to_shopify=True)
    )
    await session.flush()

    shop = await u.list_items_with_external_id(str(cid), platform="shopify")
    assert len(shop) == 1
    assert shop[0]["shopify_product_id"] == "111"
    assert shop[0]["shopify_variant_id"] == "222"

    woo = await u.list_items_with_external_id(str(cid), platform="woocommerce")
    assert len(woo) == 1
    assert woo[0]["woocommerce_product_id"] == "333"
    assert woo[0]["sale_price"] == 8.0


@pytest.mark.asyncio
async def test_list_unsynced_invoices_excludes_imported(use_test_session):
    """Returns native invoices to push out; excludes platform-imported ones."""
    session = use_test_session
    cid = await _seed_company(session, "Unsynced")
    # A native invoice (no platform marker) and an imported one (has woo marker).
    await u.upsert_order_from_woocommerce(str(cid), {
        "id": 1, "number": "WC-1", "status": "completed",
        "line_items": [{"name": "x", "quantity": 1, "price": "1", "total": "1"}], "total": "1"})
    from celerp.events.engine import emit_event
    await emit_event(
        session, company_id=cid, entity_id="doc:NATIVE-1", entity_type="doc",
        event_type="doc.created",
        data={"doc_type": "invoice", "ref_id": "NATIVE-1", "line_items": [], "total": 5.0},
        actor_id=None, location_id=None, source="api",
        idempotency_key="native-invoice-1", metadata_={})
    await session.commit()

    out = await u.list_unsynced_invoices(str(cid), platform="quickbooks")
    refs = {r["ref_id"] for r in out}
    assert "NATIVE-1" in refs       # native -> candidate to push
    assert "WC-1" not in refs       # imported from WooCommerce -> excluded


@pytest.mark.asyncio
async def test_watermark_only_advances_on_full_success(session, monkeypatch):
    """A partial run must NOT advance the incremental cursor (its errored records
    would be skipped forever); only a fully successful run does."""
    import contextlib
    from datetime import datetime, timezone

    from celerp.connectors import sync_runner
    from celerp.models.sync_run import SyncRun

    @contextlib.asynccontextmanager
    async def _ctx():
        yield session
    monkeypatch.setattr("celerp.db.get_session_ctx", _ctx)

    co = "wm-co-1"
    t_partial = datetime(2026, 6, 2, tzinfo=timezone.utc)
    session.add(SyncRun(company_id=co, connector="shopify", entity="orders", direction="inbound",
                        started_at=t_partial, finished_at=t_partial, status="partial"))
    await session.flush()
    # partial alone -> no watermark (next run does a full pull and retries failures)
    assert await sync_runner._last_success_watermark(co, "shopify", "orders") is None

    t_ok = datetime(2026, 6, 1, tzinfo=timezone.utc)
    session.add(SyncRun(company_id=co, connector="shopify", entity="orders", direction="inbound",
                        started_at=t_ok, finished_at=t_ok, status="success"))
    await session.flush()
    # now the cursor is the successful run's start, not the later partial's
    assert await sync_runner._last_success_watermark(co, "shopify", "orders") == t_ok


@pytest.mark.asyncio
async def test_woocommerce_pull_product_files(use_test_session, monkeypatch):
    """_pull_product_files resolves the item by SKU and emits each image + cert."""
    import contextlib
    import json
    from unittest.mock import AsyncMock

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp_inventory.routes import ItemCreate

    session = use_test_session
    cid = await _seed_company(session, "WooFiles")
    await u.upsert_item(str(cid), ItemCreate(
        sku="WID-1", name="Widget", sell_by="piece", sale_price=1.0, idempotency_key="woocommerce:900"))

    @contextlib.asynccontextmanager
    async def _ctx():
        yield session
    monkeypatch.setattr("celerp.db.get_session_ctx", _ctx)
    emit = AsyncMock(return_value=True)
    monkeypatch.setattr("celerp.connectors.images.download_and_emit_file", emit)

    product = {
        "images": [{"src": "https://i.test/h.jpg", "name": "h.jpg"}],
        "meta_data": [{"key": "certificates",
                       "value": json.dumps([{"url": "https://c.test/c.pdf", "name": "c.pdf"}])}],
    }
    ctx = ConnectorContext(company_id=str(cid), access_token="k:s", store_handle="https://shop.test")
    await WooCommerceConnector()._pull_product_files(ctx, product, "WID-1")
    assert emit.await_count == 2  # the hero image + the certificate

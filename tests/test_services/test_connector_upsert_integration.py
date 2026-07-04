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


# Connector events key the ledger on "{idem_key}:{content_hash}" so a changed re-import
# updates the same entity rather than dedup'ing; match all events for one external item.
async def _ledger_rows(session, cid, idem_key) -> int:
    return await session.scalar(text(
        "SELECT count(*) FROM ledger WHERE company_id = :c AND idempotency_key LIKE :k"
    ), {"c": cid, "k": idem_key + ":%"})


async def _state(session, cid, idem_key) -> dict:
    """The projection state for the entity imported under this idempotency key (latest event)."""
    row = (await session.execute(text(
        "SELECT entity_id FROM ledger WHERE company_id = :c AND idempotency_key LIKE :k "
        "ORDER BY id DESC LIMIT 1"
    ), {"c": cid, "k": idem_key + ":%"})).first()
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
        "id": 55, "number": "1001", "status": "completed", "date_paid": "2024-06-01T10:00:00",
        "line_items": [{"name": "Widget", "quantity": 2, "price": "5.00", "total": "10.00"}],
        "total": "10.00",
    }
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "noop"  # dedup
    assert await _ledger_rows(session, cid, "woocommerce:order:55") == 1
    st = await _state(session, cid, "woocommerce:order:55")
    assert st["doc_type"] == "invoice"
    assert st["status"] == "closed"           # paid -> closed
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
    assert await u.upsert_invoice_from_quickbooks(str(cid), inv) == "created"
    st = await _state(session, cid, "quickbooks:invoice:77")
    assert st["status"] == "closed"           # Balance 0 -> closed
    assert len(st["line_items"]) == 1         # subtotal row skipped
    assert st["line_items"][0]["quantity"] == 3.0
    assert st["quickbooks_invoice_id"] == "77"


@pytest.mark.asyncio
async def test_duplicate_docnumber_does_not_collapse(use_test_session):
    """Two DIFFERENT invoices that share a DocNumber (QB allows it) must stay two
    distinct docs. Keyed on the human DocNumber they collapsed into one; keyed on the
    platform Id (via the idempotency key) they don't."""
    session = use_test_session
    cid = await _seed_company(session, "DupDoc")
    inv1 = {"Id": "101", "DocNumber": "1001", "Balance": 0, "TotalAmt": 10.0,
            "Line": [{"DetailType": "SalesItemLineDetail", "Amount": 10.0, "Description": "A",
                      "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 10.0}}]}
    inv2 = {"Id": "102", "DocNumber": "1001", "Balance": 20.0, "TotalAmt": 20.0,
            "Line": [{"DetailType": "SalesItemLineDetail", "Amount": 20.0, "Description": "B",
                      "SalesItemLineDetail": {"Qty": 2, "UnitPrice": 10.0}}]}
    assert await u.upsert_invoice_from_quickbooks(str(cid), inv1) == "created"
    assert await u.upsert_invoice_from_quickbooks(str(cid), inv2) == "created"

    st1 = await _state(session, cid, "quickbooks:invoice:101")
    st2 = await _state(session, cid, "quickbooks:invoice:102")
    assert st1["total"] == 10.0 and st1["line_items"][0]["name"] == "A"   # not overwritten
    assert st2["total"] == 20.0 and st2["line_items"][0]["name"] == "B"


@pytest.mark.asyncio
async def test_reimport_with_changed_data_updates_the_doc(use_test_session):
    """Update propagation (blocker D): an unchanged re-import is a no-op; a changed one
    updates the same doc rather than creating a duplicate or silently skipping."""
    session = use_test_session
    cid = await _seed_company(session, "UpdProp")
    inv = {"Id": "300", "DocNumber": "INV-300", "Balance": 10.0, "TotalAmt": 10.0,
           "Line": [{"DetailType": "SalesItemLineDetail", "Amount": 10.0, "Description": "X",
                     "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 10.0}}]}
    assert await u.upsert_invoice_from_quickbooks(str(cid), inv) == "created"    # created
    assert await u.upsert_invoice_from_quickbooks(str(cid), inv) == "noop"   # unchanged → no-op

    inv["TotalAmt"], inv["Balance"] = 25.0, 0
    assert await u.upsert_invoice_from_quickbooks(str(cid), inv) == "updated"  # changed → updated

    st = await _state(session, cid, "quickbooks:invoice:300")
    assert st["total"] == 25.0          # new value propagated
    assert st["status"] == "closed"     # Balance 0 → closed
    assert await _ledger_rows(session, cid, "quickbooks:invoice:300") == 2  # create + one update


@pytest.mark.asyncio
async def test_line_totals_are_decimal_quantized(use_test_session):
    """P11: money amounts go through Decimal quantization — no 0.30000000000000004."""
    session = use_test_session
    cid = await _seed_company(session, "Money")
    inv = {"Id": "400", "DocNumber": "M-400", "Balance": 0, "TotalAmt": 0.30,
           "Line": [{"DetailType": "SalesItemLineDetail", "Description": "x",
                     "SalesItemLineDetail": {"Qty": 3, "UnitPrice": 0.1}}]}  # Amount absent → computed
    await u.upsert_invoice_from_quickbooks(str(cid), inv)
    st = await _state(session, cid, "quickbooks:invoice:400")
    assert st["line_items"][0]["line_total"] == 0.30   # raw float would be 0.30000000000000004
    assert st["total"] == 0.30


@pytest.mark.asyncio
async def test_xero_invoice_creates_doc(use_test_session):
    session = use_test_session
    cid = await _seed_company(session, "XeroInv")
    inv = {
        "InvoiceID": "abc-123", "InvoiceNumber": "X-1", "Status": "AUTHORISED",
        "Total": 42.0, "AmountDue": 42.0,
        "LineItems": [{"Description": "Item", "Quantity": 1, "UnitAmount": 42.0, "LineAmount": 42.0}],
    }
    assert await u.upsert_invoice_from_xero(str(cid), inv) == "created"
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
    assert await u.upsert_order_from_shopify(str(cid), order) == "created"
    assert await _ledger_rows(session, cid, "shopify:order:9001") == 1


# ── Contacts ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_contacts_create_for_all_platforms(use_test_session):
    session = use_test_session
    cid = await _seed_company(session, "Contacts")

    assert await u.upsert_contact_from_woocommerce(str(cid), {
        "id": 1, "first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.test",
        "billing": {"phone": "123", "city": "London", "country": "GB"}}) == "created"
    assert await u.upsert_contact_from_quickbooks(str(cid), {
        "Id": "2", "DisplayName": "Bob Co", "PrimaryEmailAddr": {"Address": "bob@x.test"},
        "PrimaryPhone": {"FreeFormNumber": "456"}, "BillAddr": {"City": "NYC", "Country": "US"}}) == "created"
    assert await u.upsert_contact_from_xero(str(cid), {
        "ContactID": "x-3", "Name": "Carol", "EmailAddress": "carol@x.test",
        "Phones": [{"PhoneType": "DEFAULT", "PhoneNumber": "789"}],
        "Addresses": [{"City": "Sydney", "Country": "AU"}]}) == "created"
    assert await u.upsert_contact_from_shopify(str(cid), {
        "id": 4, "first_name": "Dan", "email": "dan@x.test", "addresses": [{"city": "LA"}]}) == "created"

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
    assert await u.upsert_contact_from_woocommerce(str(a), cust) == "created"
    assert await u.upsert_contact_from_woocommerce(str(b), cust) == "created"   # NOT blocked by A
    assert await u.upsert_contact_from_woocommerce(str(a), cust) == "noop"  # A's own dedup
    assert await _ledger_rows(session, a, "woocommerce:customer:99") == 1
    assert await _ledger_rows(session, b, "woocommerce:customer:99") == 1


# ── Outbound list helpers ─────────────────────────────────────────────────────

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
    session.add(SyncRun(company_id=co, connector="shopify", entity="orders", started_at=t_partial, finished_at=t_partial, status="partial"))
    await session.flush()
    # partial alone -> no watermark (next run does a full pull and retries failures)
    assert await sync_runner._last_success_watermark(co, "shopify", "orders") is None

    t_ok = datetime(2026, 6, 1, tzinfo=timezone.utc)
    session.add(SyncRun(company_id=co, connector="shopify", entity="orders", started_at=t_ok, finished_at=t_ok, status="success"))
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


@pytest.mark.asyncio
async def test_reimport_updates_legacy_uuid_projection_not_duplicate(use_test_session):
    """Finding: a record imported under the pre-deterministic-id scheme (random-uuid
    entity_id, but idempotency_key present in state — as the backfill migration stamps it)
    must UPDATE in place on re-import, not spawn a duplicate under the new deterministic
    entity_id."""
    from celerp.events.engine import emit_event

    session = use_test_session
    cid = await _seed_company(session, "Legacy")
    # A legacy import: a doc projection under a random entity_id whose state carries the
    # stable idempotency_key (what the e4f5a6b7c8d9 backfill stamps onto old rows).
    await emit_event(
        session, company_id=cid, entity_id="doc:legacy-uuid-xyz", entity_type="doc",
        event_type="doc.created",
        data={"doc_type": "invoice", "ref_id": "#1001", "status": "open", "total": 10.0,
              "shopify_order_id": "555", "idempotency_key": "shopify:order:555"},
        actor_id=None, location_id=None, source="connector",
        idempotency_key="shopify:order:555", metadata_={},
    )
    await session.flush()  # the legacy row exists before the re-import queries for it
    # A changed re-import of the same Shopify order after upgrade.
    order = {"id": 555, "name": "#1001", "financial_status": "paid",
             "line_items": [{"title": "X", "quantity": 1, "price": "20.00"}], "total_price": "20.00"}
    assert await u.upsert_order_from_shopify(str(cid), order) == "updated"  # not "created"

    n = await session.scalar(text(
        "SELECT count(*) FROM projections WHERE company_id = :c AND entity_type='doc' "
        "AND state->>'idempotency_key' = 'shopify:order:555'"), {"c": cid})
    assert n == 1  # exactly one doc — no duplicate
    eid = await session.scalar(text(
        "SELECT entity_id FROM projections WHERE company_id = :c AND entity_type='doc' "
        "AND state->>'idempotency_key' = 'shopify:order:555'"), {"c": cid})
    assert eid == "doc:legacy-uuid-xyz"  # updated in place under the legacy id


@pytest.mark.asyncio
async def test_run_sync_feeds_watermark_as_since(session, monkeypatch):
    """run_sync must pass the last-successful watermark into the connector as `since=`
    — the incremental-pull wiring. Without it every scheduled/webhook sync silently
    degrades to a full pull on each tick."""
    import contextlib
    from datetime import datetime, timezone

    from celerp.connectors import sync_runner
    from celerp.connectors.base import ConnectorBase, ConnectorContext, SyncEntity, SyncResult
    from celerp.models.sync_run import SyncRun

    @contextlib.asynccontextmanager
    async def _ctx():
        yield session
    monkeypatch.setattr("celerp.db.get_session_ctx", _ctx)

    co = "wm-since-1"
    watermark = datetime(2026, 5, 1, tzinfo=timezone.utc)
    session.add(SyncRun(company_id=co, connector="shopify", entity="products",
                        started_at=watermark, finished_at=watermark, status="success"))
    await session.flush()

    seen: dict = {}

    class _Stub(ConnectorBase):
        name = "shopify"
        display_name = "Stub"
        category = None
        supported_entities = [SyncEntity.PRODUCTS]
        conflict_strategy: dict = {}

        async def sync_products(self, ctx, since=None):
            seen["since"] = since
            return SyncResult(entity=SyncEntity.PRODUCTS, created=1)

        async def sync_orders(self, ctx, since=None):
            return SyncResult(entity=SyncEntity.ORDERS)

    ctx = ConnectorContext(company_id=co, access_token="t", store_handle="s.myshopify.com")
    await sync_runner.run_sync(_Stub(), ctx, "products")  # since defaults to the watermark
    assert seen["since"] == watermark

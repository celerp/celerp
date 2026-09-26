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
from sqlalchemy import select, text

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
import celerp.connectors.upsert as u


async def _seed_company(session, name: str) -> uuid.UUID:
    cid = uuid.uuid4()
    uid = uuid.uuid4()
    session.add(Company(
        id=cid, name=name, slug=f"{name.lower()}-{cid.hex[:8]}",
        settings={"currency": "USD"},
    ))
    session.add(User(
        id=uid, email=f"{name.lower()}-{uid.hex[:8]}@example.test",
        name=f"{name} Owner", auth_hash=None,
    ))
    await session.flush()
    session.add(UserCompany(
        user_id=uid, company_id=cid, role="owner", is_active=True,
    ))
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
    assert await _ledger_rows(session, cid, "woocommerce:order:55") >= 1
    st = await _state(session, cid, "woocommerce:order:55")
    assert st["doc_type"] == "invoice"
    assert st["status"] == "paid"
    assert st["finalized"] is True
    assert st["total"] == 10.0
    assert st["line_items"][0]["unit_price"] == 5.0
    assert st["woocommerce_order_id"] == "55"


@pytest.mark.asyncio
async def test_quickbooks_invoice_creates_doc(use_test_session):
    session = use_test_session
    cid = await _seed_company(session, "QbInv")
    inv = {
        "Id": "77", "DocNumber": "INV-77", "Balance": 0,
        "TotalAmt": 30.0, "CurrencyRef": {"value": "USD"}, "ExchangeRate": 35.5,
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
    assert st["conversion_rate"] == 35.5


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
        "Total": 42.0, "AmountDue": 42.0, "CurrencyCode": "USD", "CurrencyRate": 0.025,
        "LineItems": [{"Description": "Item", "Quantity": 1, "UnitAmount": 42.0, "LineAmount": 42.0}],
    }
    assert await u.upsert_invoice_from_xero(str(cid), inv) == "created"
    st = await _state(session, cid, "xero:invoice:abc-123")
    assert st["status"] == "open"             # AUTHORISED (not PAID) -> open
    assert st["amount_outstanding"] == 42.0
    assert st["xero_invoice_id"] == "abc-123"
    assert st["conversion_rate"] == 40.0


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
    session.add(SyncRun(company_id=co, connector="shopify", entity="orders", direction="inbound", started_at=t_partial, finished_at=t_partial, status="partial"))
    await session.flush()
    # partial alone -> no watermark (next run does a full pull and retries failures)
    assert await sync_runner._last_success_watermark(co, "shopify", "orders") is None

    t_ok = datetime(2026, 6, 1, tzinfo=timezone.utc)
    session.add(SyncRun(company_id=co, connector="shopify", entity="orders", direction="inbound", started_at=t_ok, finished_at=t_ok, status="success"))
    await session.flush()
    # now the cursor is the successful run's start, not the later partial's
    assert await sync_runner._last_success_watermark(co, "shopify", "orders") == t_ok


    t_reset = datetime(2026, 6, 3, tzinfo=timezone.utc)
    session.add(SyncRun(
        company_id=co, connector="shopify",
        entity=sync_runner.CONNECTOR_RESET_ENTITY,
        direction="inbound", started_at=t_reset, finished_at=t_reset,
        status="reset",
    ))
    await session.flush()
    assert await sync_runner._last_success_watermark(co, "shopify", "orders") is None

    t_new = datetime(2026, 6, 4, tzinfo=timezone.utc)
    session.add(SyncRun(
        company_id=co, connector="shopify", entity="orders", direction="inbound",
        started_at=t_new, finished_at=t_new, status="success",
    ))
    await session.flush()
    assert await sync_runner._last_success_watermark(co, "shopify", "orders") == t_new


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
async def test_connector_item_upsert_rejects_comma_sku(use_test_session):
    """A connector pushing a comma-bearing SKU is rejected at the event/schema boundary, not just at
    the interactive route: the invariant lives on the item event itself, so every emitter (import,
    connector, direct) is covered. A comma is Celerp's OR operator; a SKU with one is unscannable."""
    from celerp_inventory.routes import ItemCreate

    session = use_test_session
    cid = await _seed_company(session, "CommaConn")
    with pytest.raises(ValueError, match="comma"):
        await u.upsert_item(str(cid), ItemCreate(
            sku="BAD,SKU", name="Bad", sell_by="piece", sale_price=1.0,
            idempotency_key="woocommerce:comma-1"))


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
async def test_invoice_push_writeback_prevents_duplicate(use_test_session, monkeypatch):
    """Outbound invoice push stamps the returned external id (doc.pushed), so the doc
    drops off list_unsynced_invoices and is never created on the platform twice — the
    fix for the create-without-write-back duplication."""
    from unittest.mock import AsyncMock, MagicMock

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.quickbooks import QuickBooksConnector
    from celerp.events.engine import emit_event
    from celerp_docs.doc_service import list_unsynced_invoices

    session = use_test_session
    cid = await _seed_company(session, "QbPush")
    # A native Celerp invoice (no quickbooks_invoice_id marker) — an outbound candidate.
    await emit_event(
        session, company_id=cid, entity_id="doc:native-1", entity_type="doc",
        event_type="doc.created",
        data={"doc_type": "invoice", "ref_id": "INV-100", "status": "open", "total": 50.0,
              "line_items": [{"description": "X", "quantity": 1, "unit_price": 50.0, "total": 50.0}]},
        actor_id=None, location_id=None, source="api", idempotency_key="native-1", metadata_={},
    )
    await session.flush()

    before = await list_unsynced_invoices(str(cid), "quickbooks")
    assert any(i["ref_id"] == "INV-100" for i in before)  # a push candidate before the push

    # Mock the QuickBooks POST to return a created invoice Id.
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"Invoice": {"Id": "QB-555"}})
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("celerp.connectors.quickbooks.RateLimitedClient", lambda *a, **k: client)

    ctx = ConnectorContext(company_id=str(cid), access_token="t", store_handle="123456789")
    connector = QuickBooksConnector()
    first = await connector.sync_invoices_out(ctx)
    assert first.created == 1 and not first.errors

    # After push: stamped, and no longer a candidate.
    after = await list_unsynced_invoices(str(cid), "quickbooks")
    assert not any(i["ref_id"] == "INV-100" for i in after)
    st = await session.scalar(text(
        "SELECT state FROM projections WHERE company_id = :c AND entity_id = 'doc:native-1'"), {"c": cid})
    assert st["quickbooks_invoice_id"] == "QB-555"

    # The regression that matters: a SECOND run must NOT create the invoice again.
    # Without the doc.pushed write-back this POSTs twice → a duplicate invoice in QB.
    second = await connector.sync_invoices_out(ctx)
    assert second.created == 0                    # nothing left to push
    assert client.post.await_count == 1           # exactly one create on the platform, ever


@pytest.mark.asyncio
async def test_run_sync_feeds_watermark_as_since(session, monkeypatch):
    """run_sync must pass the last-successful watermark into the connector as `since=`
    — the incremental-pull wiring. Without it every scheduled/webhook sync silently
    degrades to a full pull on each tick."""
    import contextlib
    from datetime import datetime, timezone

    from celerp.connectors import sync_runner
    from celerp.connectors.base import (
        ConnectorBase, ConnectorContext, SyncDirection, SyncEntity, SyncResult,
    )
    from celerp.models.sync_run import SyncRun

    @contextlib.asynccontextmanager
    async def _ctx():
        yield session
    monkeypatch.setattr("celerp.db.get_session_ctx", _ctx)

    co = str(await _seed_company(session, "WatermarkSync"))
    watermark = datetime(2026, 5, 1, tzinfo=timezone.utc)
    session.add(SyncRun(company_id=co, connector="shopify", entity="products",
                        direction="inbound", started_at=watermark, finished_at=watermark, status="success"))
    await session.flush()

    seen: dict = {}

    class _Stub(ConnectorBase):
        name = "shopify"
        display_name = "Stub"
        category = None
        direction = SyncDirection.BOTH
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


@pytest.mark.asyncio
async def test_run_sync_dispatches_and_gates_outbound_entity(session, monkeypatch):
    """run_sync must route an outbound entity to its *_out method, and the direction
    gate must BLOCK it when the connector is inbound-only. Regression for both the
    'outbound never wired' bug and the direction filter."""
    import contextlib

    from celerp.connectors import sync_runner
    from celerp.connectors.base import (
        ConnectorBase, ConnectorContext, SyncDirection, SyncEntity, SyncResult,
    )

    @contextlib.asynccontextmanager
    async def _ctx():
        yield session
    monkeypatch.setattr("celerp.db.get_session_ctx", _ctx)

    calls: list[str] = []

    class _Stub(ConnectorBase):
        name = "stub_out"
        display_name = "Stub"
        category = None
        direction = SyncDirection.BOTH
        supported_entities = [SyncEntity.PRODUCTS]
        conflict_strategy: dict = {}

        async def sync_products(self, ctx, since=None):
            return SyncResult(entity=SyncEntity.PRODUCTS)

        async def sync_orders(self, ctx, since=None):
            return SyncResult(entity=SyncEntity.ORDERS)

        async def sync_products_out(self, ctx):
            calls.append("products_out")
            return SyncResult(entity=SyncEntity.PRODUCTS, direction=SyncDirection.OUTBOUND, created=1)

    co = str(await _seed_company(session, "OutboundSync"))
    ctx = ConnectorContext(company_id=co, access_token="t", store_handle="s")

    # direction=both -> the outbound method IS dispatched.
    r1 = await sync_runner.run_sync(_Stub(), ctx, "products_out", direction=SyncDirection.BOTH)
    assert calls == ["products_out"] and r1.created == 1

    # direction=inbound -> the outbound entity is BLOCKED (method not called again).
    r2 = await sync_runner.run_sync(_Stub(), ctx, "products_out", direction=SyncDirection.INBOUND)
    assert calls == ["products_out"]  # unchanged — the push did not run
    assert r2.errors and "blocked by direction" in r2.errors[0]


@pytest.mark.asyncio
async def test_run_sync_uses_current_connector_context_and_direction(
    use_test_session, monkeypatch
):
    from unittest.mock import AsyncMock

    from celerp.connectors import sync_runner
    from celerp.connectors.base import (
        ConnectorBase,
        ConnectorContext,
        SyncDirection,
        SyncEntity,
        SyncResult,
    )
    from celerp.models.connector_config import ConnectorConfig

    session = use_test_session
    cid = await _seed_company(session, "FreshSync")
    connector_name = "fresh_ctx_stub"
    config = ConnectorConfig(
        company_id=str(cid),
        connector=connector_name,
        direction="inbound",
    )
    session.add(config)
    await session.commit()

    seen: list[str] = []

    class _Stub(ConnectorBase):
        name = connector_name
        display_name = "Fresh Context Stub"
        category = None
        direction = SyncDirection.BOTH
        supported_entities = [SyncEntity.PRODUCTS]
        conflict_strategy: dict = {}

        async def sync_products(self, ctx, since=None):
            seen.append(ctx.access_token)
            return SyncResult(entity=SyncEntity.PRODUCTS, created=1)

        async def sync_orders(self, ctx, since=None):
            return SyncResult(entity=SyncEntity.ORDERS)

        async def sync_products_out(self, ctx):
            seen.append("outbound")
            return SyncResult(
                entity=SyncEntity.PRODUCTS,
                direction=SyncDirection.OUTBOUND,
                created=1,
            )

    fresh = ConnectorContext(
        company_id=str(cid),
        access_token="new-token",
        store_handle="new-store",
    )
    fetch = AsyncMock(return_value=fresh)
    monkeypatch.setattr("celerp.connectors.relay_token.fetch_context", fetch)

    stale = ConnectorContext(
        company_id=str(cid),
        access_token="old-token",
        store_handle="old-store",
    )
    inbound = await sync_runner.run_sync(
        _Stub(), stale, "products", direction=SyncDirection.BOTH
    )
    assert inbound.created == 1
    assert seen == ["new-token"]
    fetch.assert_awaited_once_with(
        str(cid), connector_name, ownership_session=session
    )

    changed = await sync_runner.run_sync(
        _Stub(),
        stale,
        "products",
        direction=SyncDirection.BOTH,
        expected_config_id=config.id + 1000,
    )
    assert changed.errors and "connection changed" in changed.errors[0]
    assert seen == ["new-token"]
    assert fetch.await_count == 1

    blocked = await sync_runner.run_sync(
        _Stub(), stale, "products_out", direction=SyncDirection.BOTH
    )
    assert blocked.errors and "blocked by direction=inbound" in blocked.errors[0]
    assert seen == ["new-token"]

    config.direction = "outbound"
    await session.commit()

    allowed = await sync_runner.run_sync(
        _Stub(), stale, "products_out", direction=SyncDirection.INBOUND
    )
    assert allowed.created == 1
    assert seen == ["new-token", "outbound"]

    replaced = await sync_runner.run_sync(
        _Stub(),
        stale,
        "products_out",
        direction=SyncDirection.BOTH,
        expected_store_handle="old-store",
    )
    assert replaced.errors and "connection changed" in replaced.errors[0]
    assert seen == ["new-token", "outbound"]


@pytest.mark.asyncio
async def test_woocommerce_processing_order_reserves_across_lots(use_test_session):
    from datetime import datetime, timezone

    from celerp.models.projections import Projection

    session = use_test_session
    cid = await _seed_company(session, "WooLots")
    now = datetime.now(timezone.utc)
    from celerp_inventory.services import upsert_external_product

    outcome, root_id = await upsert_external_product(
        str(cid),
        platform="woocommerce",
        product_id="501",
        variation_id=None,
        sku="LOT-SKU",
        name="Lot Product",
        seed_quantity=False,
        link_fields={"manage_stock": True},
    )
    assert outcome == "created"
    for suffix, qty in (("a", 2), ("b", 3)):
        session.add(Projection(
            company_id=cid, entity_id=f"item:lot-{suffix}", entity_type="item",
            version=1, created_at=now, updated_at=now,
            state={
                "sku": "LOT-SKU", "name": "Lot Product", "quantity": qty,
                "status": "available", "sell_by": "piece", "lot": True,
                "parent_item_id": root_id, "allow_splitting": True,
            },
        ))
    await session.commit()

    order = {
        "id": 991,
        "number": "991",
        "status": "processing",
        "currency": "USD",
        "total": "40.00",
        "total_tax": "0",
        "line_items": [{
            "product_id": 501,
            "variation_id": 0,
            "sku": "LOT-SKU",
            "name": "Lot Product",
            "quantity": 4,
            "total": "40.00",
            "total_tax": "0",
        }],
        "shipping_lines": [],
        "fee_lines": [],
    }

    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == cid,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    reserved = [
        row for row in rows
        if (row.state or {}).get("status") == "reserved"
        and (row.state or {}).get("status_doc_id") == "doc:woocommerce:order:991"
    ]
    assert sum(float((row.state or {}).get("quantity") or 0) for row in reserved) == 4
    assert len(reserved) == 2


@pytest.mark.asyncio
async def test_woocommerce_same_sku_keeps_the_live_external_link(use_test_session):
    cid = await _seed_company(use_test_session, "WooIdentity")
    from celerp_inventory.services import upsert_external_product
    await upsert_external_product(
        str(cid), platform="woocommerce", product_id="701", variation_id=None,
        sku="IDENTITY-SKU", name="Identity Product", link_fields={"manage_stock": True},
    )
    with pytest.raises(ValueError, match="already linked to a different"):
        await upsert_external_product(
            str(cid), platform="woocommerce", product_id="702", variation_id=None,
            sku="IDENTITY-SKU", name="Other Remote Product", link_fields={"manage_stock": True},
        )


@pytest.mark.asyncio
async def test_disabled_remote_relink_repairs_identity_without_overwriting_product(
    use_test_session,
):
    from celerp.models.projections import Projection
    from celerp_inventory.services import (
        external_link_for_state,
        set_external_link_state,
        upsert_external_product,
    )

    session = use_test_session
    cid = await _seed_company(session, "WooDisabledRelink")
    _, entity_id = await upsert_external_product(
        str(cid),
        platform="woocommerce",
        product_id="701",
        variation_id=None,
        sku="KEEP-SKU",
        name="Local Name",
        description="Local Description",
        sale_price=10.0,
        link_fields={"manage_stock": True},
    )
    await set_external_link_state(
        session,
        cid,
        entity_id,
        "woocommerce",
        sync_enabled=False,
        remote_deleted=True,
    )
    await session.commit()

    outcome, relinked_id = await upsert_external_product(
        str(cid),
        platform="woocommerce",
        product_id="702",
        variation_id=None,
        sku="KEEP-SKU",
        name="Remote Name",
        description="Remote Description",
        sale_price=99.0,
        link_fields={"manage_stock": False},
    )

    assert outcome == "disabled"
    assert relinked_id == entity_id
    row = await session.get(
        Projection,
        {"company_id": cid, "entity_id": entity_id},
        populate_existing=True,
    )
    state = row.state or {}
    assert state["name"] == "Local Name"
    assert state["description"] == "Local Description"
    assert state["sale_price"] == 10.0
    link = external_link_for_state(state, "woocommerce")
    assert link["product_id"] == "702"
    assert link["sync_enabled"] is False
    assert link["remote_deleted"] is False
    assert link["manage_stock"] is False


@pytest.mark.asyncio
async def test_historical_barcoded_parcel_is_not_a_catalog_anchor(use_test_session):
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    from celerp_inventory.services import resolve_catalog_anchor_for_item, upsert_external_product
    session = use_test_session
    cid = await _seed_company(session, "WooAnchor")
    _, root_id = await upsert_external_product(
        str(cid), platform="woocommerce", product_id="711", variation_id=None,
        sku="ANCHOR-SKU", name="Anchor Product", link_fields={"manage_stock": True},
    )
    now = datetime.now(timezone.utc)
    parcel_id = "item:historical-receipt"
    session.add(Projection(
        company_id=cid, entity_id=parcel_id, entity_type="item", version=1,
        created_at=now, updated_at=now,
        state={"sku": "ANCHOR-SKU", "name": "Received Parcel", "quantity": 1,
               "status": "available", "sell_by": "piece", "barcode": "900001"},
    ))
    await session.commit()
    anchor = await resolve_catalog_anchor_for_item(session, cid, parcel_id)
    assert anchor.entity_id == root_id


@pytest.mark.parametrize("release_status", ["pending", "cancelled", "failed"])
@pytest.mark.asyncio
async def test_woocommerce_on_hold_reservation_releases_on_woo_restore_status(
    use_test_session, release_status
):
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    from celerp_inventory.services import upsert_external_product
    session = use_test_session
    cid = await _seed_company(session, f"WooHold-{release_status}")
    _, root_id = await upsert_external_product(
        str(cid), platform="woocommerce", product_id="721", variation_id=None,
        sku=f"HOLD-{release_status}", name="Hold Product",
        link_fields={"manage_stock": True},
    )
    now = datetime.now(timezone.utc)
    session.add(Projection(
        company_id=cid, entity_id=f"item:hold-lot-{release_status}", entity_type="item",
        version=1, created_at=now, updated_at=now,
        state={"sku": f"HOLD-{release_status}", "name": "Hold Product", "quantity": 2,
               "status": "available", "sell_by": "piece", "lot": True,
               "parent_item_id": root_id, "allow_splitting": True},
    ))
    await session.commit()
    order = {
        "id": {"pending": 722, "cancelled": 723, "failed": 724}[release_status],
        "number": release_status,
        "status": "on-hold", "currency": "USD",
        "total": "10.00", "total_tax": "0",
        "line_items": [{
            "product_id": 721, "variation_id": 0, "sku": f"HOLD-{release_status}",
            "name": "Hold Product", "quantity": 1, "total": "10.00", "total_tax": "0",
        }],
        "shipping_lines": [], "fee_lines": [],
    }
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    doc_id = f"woocommerce:order:{order['id']}"
    doc = await _state(session, cid, doc_id)
    assert doc.get("finalized") is not True

    assert await u.upsert_order_from_woocommerce(
        str(cid), {**order, "status": release_status}
    ) == "updated"
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "item"
    ))).scalars().all()
    assert not [r for r in rows if (r.state or {}).get("status") == "reserved"]
    assert sum(float((r.state or {}).get("quantity") or 0) for r in rows
               if (r.state or {}).get("status") == "available"
               and (r.state or {}).get("sku") == f"HOLD-{release_status}") == 2


@pytest.mark.asyncio
async def test_woocommerce_pending_defers_stock_binding_until_processing(use_test_session):
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    from celerp_inventory.services import upsert_external_product

    session = use_test_session
    cid = await _seed_company(session, "WooPending")
    _, root_id = await upsert_external_product(
        str(cid), platform="woocommerce", product_id="751", variation_id=None,
        sku="PENDING-SKU", name="Pending Product", link_fields={"manage_stock": True},
    )
    now = datetime.now(timezone.utc)
    session.add(Projection(
        company_id=cid, entity_id="item:pending-lot", entity_type="item",
        version=1, created_at=now, updated_at=now,
        state={"sku": "PENDING-SKU", "name": "Pending Product", "quantity": 1,
               "status": "available", "sell_by": "piece", "lot": True,
               "parent_item_id": root_id, "allow_splitting": True},
    ))
    await session.commit()
    order = {
        "id": 752, "number": "752", "status": "pending", "currency": "USD",
        "total": "10.00", "total_tax": "0",
        "line_items": [{
            "product_id": 751, "variation_id": 0, "sku": "PENDING-SKU",
            "name": "Pending Product", "quantity": 1, "total": "10.00", "total_tax": "0",
        }],
        "shipping_lines": [], "fee_lines": [],
    }

    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    pending = await _state(session, cid, "woocommerce:order:752")
    assert pending["line_items"][0].get("item_id") is None

    assert await u.upsert_order_from_woocommerce(
        str(cid), {**order, "status": "processing"}
    ) == "updated"
    session.expire_all()
    processing = await _state(session, cid, "woocommerce:order:752")
    assert processing["finalized"] is True
    assert processing["line_items"][0].get("item_id")
    bound = await session.get(
        Projection,
        {"company_id": cid, "entity_id": processing["line_items"][0]["item_id"]},
        populate_existing=True,
    )
    assert (bound.state or {}).get("status") == "reserved"
    assert (bound.state or {}).get("status_doc_id") == "doc:woocommerce:order:752"


@pytest.mark.asyncio
async def test_woocommerce_refund_does_not_guess_restock_and_pauses_outbound_stock(use_test_session):
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    from celerp_inventory.services import external_link_for_state, upsert_external_product
    session = use_test_session
    cid = await _seed_company(session, "WooRefund")
    _, root_id = await upsert_external_product(
        str(cid), platform="woocommerce", product_id="741", variation_id=None,
        sku="REFUND-SKU", name="Refund Product", link_fields={"manage_stock": True},
    )
    now = datetime.now(timezone.utc)
    session.add(Projection(
        company_id=cid, entity_id="item:refund-lot", entity_type="item",
        version=1, created_at=now, updated_at=now,
        state={"sku": "REFUND-SKU", "name": "Refund Product", "quantity": 2,
               "status": "available", "sell_by": "piece", "lot": True,
               "parent_item_id": root_id, "allow_splitting": True},
    ))
    await session.commit()
    order = {
        "id": 742, "number": "742", "status": "on-hold", "currency": "USD",
        "total": "10.00", "total_tax": "0",
        "line_items": [{
            "product_id": 741, "variation_id": 0, "sku": "REFUND-SKU",
            "name": "Refund Product", "quantity": 1, "total": "10.00", "total_tax": "0",
        }],
        "shipping_lines": [], "fee_lines": [],
    }
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"

    with pytest.raises(ValueError, match="manual financial/inventory reconciliation") as refund:
        await u.upsert_order_from_woocommerce(str(cid), {**order, "status": "refunded"})

    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "item"
    ))).scalars().all()
    assert sum(float((r.state or {}).get("quantity") or 0) for r in rows
               if (r.state or {}).get("status") == "reserved") == 1
    root = await session.get(
        Projection, {"company_id": cid, "entity_id": root_id},
        populate_existing=True,
    )
    assert external_link_for_state(root.state or {}, "woocommerce").get(
        "inventory_sync_paused"
    ) is True

    # Once a person marks the refund reconciled, stock sync resumes.
    from celerp_connectors.routes import _set_order_reconciled

    await _attention_run(session, cid, [_entry("742", refund.value)])
    await _set_order_reconciled(session, cid, "742", refund.value.signature, None)
    session.expire_all()
    root = await session.get(
        Projection, {"company_id": cid, "entity_id": root_id},
        populate_existing=True,
    )
    assert external_link_for_state(root.state or {}, "woocommerce").get(
        "inventory_sync_paused"
    ) is False


@pytest.mark.asyncio
async def test_woocommerce_stock_stays_paused_while_any_refund_on_the_product_is_open(use_test_session):
    """Two refunded orders share one product. Reconciling one leaves stock
    paused for the other, and undoing a reconciliation pauses it again at once."""
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    from celerp_connectors.routes import _set_order_reconciled
    from celerp_inventory.services import external_link_for_state, upsert_external_product

    session = use_test_session
    cid = await _seed_company(session, "WooSharedRefund")
    _, root_id = await upsert_external_product(
        str(cid), platform="woocommerce", product_id="761", variation_id=None,
        sku="SHARED-SKU", name="Shared Product", link_fields={"manage_stock": True},
    )
    now = datetime.now(timezone.utc)
    session.add(Projection(
        company_id=cid, entity_id="item:shared-lot", entity_type="item",
        version=1, created_at=now, updated_at=now,
        state={"sku": "SHARED-SKU", "name": "Shared Product", "quantity": 4,
               "status": "available", "sell_by": "piece", "lot": True,
               "parent_item_id": root_id, "allow_splitting": True},
    ))
    await session.commit()

    def _order(order_id):
        return {
            "id": order_id, "number": str(order_id), "status": "on-hold", "currency": "USD",
            "total": "10.00", "total_tax": "0",
            "line_items": [{
                "product_id": 761, "variation_id": 0, "sku": "SHARED-SKU",
                "name": "Shared Product", "quantity": 1, "total": "10.00", "total_tax": "0",
            }],
            "shipping_lines": [], "fee_lines": [],
        }

    refunds = {}
    for order_id in (762, 763):
        assert await u.upsert_order_from_woocommerce(str(cid), _order(order_id)) == "created"
        with pytest.raises(ValueError, match="reconciliation") as refund:
            await u.upsert_order_from_woocommerce(
                str(cid), {**_order(order_id), "status": "refunded"}
            )
        refunds[str(order_id)] = refund.value
    await _attention_run(session, cid, [_entry(k, v) for k, v in refunds.items()])

    async def _paused():
        session.expire_all()
        root = await session.get(
            Projection, {"company_id": cid, "entity_id": root_id}, populate_existing=True,
        )
        return external_link_for_state(root.state or {}, "woocommerce").get(
            "inventory_sync_paused"
        )

    await _set_order_reconciled(session, cid, "762", refunds["762"].signature, None)
    assert await _paused() is True

    await _set_order_reconciled(session, cid, "763", refunds["763"].signature, None)
    assert await _paused() is False

    await _set_order_reconciled(session, cid, "763", None, None)
    assert await _paused() is True


@pytest.mark.asyncio
async def test_woocommerce_unmanaged_product_order_does_not_invent_stock(use_test_session):
    cid = await _seed_company(use_test_session, "WooUnmanaged")
    from celerp_inventory.services import upsert_external_product
    await upsert_external_product(
        str(cid), platform="woocommerce", product_id="731", variation_id=None,
        sku="UNMANAGED-SKU", name="Unmanaged Product", link_fields={"manage_stock": False},
    )
    order = {
        "id": 732, "number": "732", "status": "processing", "currency": "USD",
        "total": "15.00", "total_tax": "0",
        "line_items": [{"product_id": 731, "variation_id": 0, "sku": "UNMANAGED-SKU",
                        "name": "Unmanaged Product", "quantity": 1, "total": "15.00",
                        "total_tax": "0"}],
        "shipping_lines": [], "fee_lines": [],
    }
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    doc = await _state(use_test_session, cid, "woocommerce:order:732")
    assert doc["finalized"] is True
    assert doc["line_items"][0].get("item_id") is None


@pytest.mark.asyncio
async def test_explicit_catalog_relation_accepts_barcoded_catalog_template(use_test_session):
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    from celerp_inventory.services import resolve_catalog_anchor_for_item

    session = use_test_session
    cid = await _seed_company(session, "ExplicitCatalog")
    now = datetime.now(timezone.utc)
    root_id = "item:barcoded-catalog-root"
    child_id = "item:barcoded-catalog-child"
    session.add_all([
        Projection(
            company_id=cid, entity_id=root_id, entity_type="item",
            version=1, created_at=now, updated_at=now,
            state={
                "sku": "BARCODED-CATALOG", "name": "Catalog Product",
                "quantity": 0, "status": "available", "sell_by": "piece",
                "barcode": "CATALOG-REFERENCE-CODE",
            },
        ),
        Projection(
            company_id=cid, entity_id=child_id, entity_type="item",
            version=1, created_at=now, updated_at=now,
            state={
                "sku": "BARCODED-CATALOG", "name": "Physical Parcel",
                "quantity": 1, "status": "available", "sell_by": "piece",
                "barcode": "PHYSICAL-PARCEL-CODE", "catalog_item_id": root_id,
            },
        ),
    ])
    await session.commit()
    anchor = await resolve_catalog_anchor_for_item(session, cid, child_id)
    assert anchor.entity_id == root_id


@pytest.mark.asyncio
async def test_external_product_identity_has_one_catalog_owner(use_test_session):
    from celerp.events.engine import emit_event
    from celerp_inventory.services import (
        ExternalLinkConflictError,
        set_external_link,
    )

    session = use_test_session
    cid = await _seed_company(session, "ExternalIdentityOwner")
    for entity_id, sku in (("item:left", "LEFT-1"), ("item:right", "RIGHT-1")):
        await emit_event(
            session,
            company_id=cid,
            entity_id=entity_id,
            entity_type="item",
            event_type="item.created",
            data={"sku": sku, "name": sku, "sell_by": "piece"},
            actor_id=None,
            location_id=None,
            source="api",
            idempotency_key=f"create:{entity_id}",
            metadata_={},
        )
    await session.flush()

    link = {
        "product_id": "9901",
        "sync_enabled": True,
        "remote_deleted": False,
    }
    await set_external_link(
        session, cid, "item:left", "woocommerce", link, expected_sku="LEFT-1"
    )
    with pytest.raises(ExternalLinkConflictError, match="already linked"):
        await set_external_link(
            session, cid, "item:right", "woocommerce", link,
            expected_sku="RIGHT-1",
        )


@pytest.mark.asyncio
async def test_external_link_rejects_stale_sku_selection(use_test_session):
    from celerp.events.engine import emit_event
    from celerp_inventory.services import (
        ExternalLinkConflictError,
        set_external_link,
    )

    session = use_test_session
    cid = await _seed_company(session, "ExternalIdentityCas")
    await emit_event(
        session,
        company_id=cid,
        entity_id="item:cas",
        entity_type="item",
        event_type="item.created",
        data={"sku": "NEW-SKU", "name": "CAS", "sell_by": "piece"},
        actor_id=None,
        location_id=None,
        source="api",
        idempotency_key="create:cas",
        metadata_={},
    )
    await session.flush()

    with pytest.raises(ExternalLinkConflictError, match="SKU changed"):
        await set_external_link(
            session,
            cid,
            "item:cas",
            "woocommerce",
            {
                "product_id": "9902",
                "sync_enabled": True,
                "remote_deleted": False,
            },
            expected_sku="OLD-SKU",
        )


@pytest.mark.asyncio
async def test_external_link_compare_and_set_rejects_stale_writers(use_test_session):
    from celerp.events.engine import emit_event
    from celerp_inventory.services import (
        ExternalLinkConflictError,
        set_external_link,
    )

    session = use_test_session
    cid = await _seed_company(session, "LinkCas")
    entity_id = "item:link-cas"
    await emit_event(
        session,
        company_id=cid,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.created",
        data={"sku": "CAS-1", "name": "CAS", "sell_by": "piece"},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )

    await set_external_link(
        session, cid, entity_id, "woocommerce",
        {"product_id": "1", "sync_enabled": True},
    )
    await set_external_link(
        session, cid, entity_id, "woocommerce",
        {"product_id": "2", "sync_enabled": True},
    )

    with pytest.raises(ExternalLinkConflictError):
        await set_external_link(
            session, cid, entity_id, "woocommerce",
            {"product_id": "3", "sync_enabled": True},
            expected_identity=("1", None),
        )

    with pytest.raises(ExternalLinkConflictError):
        await set_external_link(
            session, cid, entity_id, "woocommerce",
            {"product_id": "3", "sync_enabled": True},
            require_unlinked=True,
        )


_WOO_PLAIN_ORDER = {
    "number": "2001", "status": "completed", "currency": "USD",
    "total": "10.00", "total_tax": "0",
    "line_items": [{"name": "Widget", "quantity": 2, "price": "5.00", "total": "10.00", "total_tax": "0"}],
    "shipping_lines": [], "fee_lines": [],
}


@pytest.mark.asyncio
async def test_woocommerce_payment_applies_the_outstanding_balance_not_the_total(use_test_session):
    """A payment recorded by hand before the store reports the order paid must not
    be booked twice: the connector settles what is still outstanding."""
    from celerp_docs.routes import apply_doc_payment

    session = use_test_session
    cid = await _seed_company(session, "WooOutstanding")
    order = {**_WOO_PLAIN_ORDER, "id": 2001}
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    doc_id = "doc:woocommerce:order:2001"
    await apply_doc_payment(
        session, cid, doc_id,
        {"amount": 4.0, "payment_date": "2024-06-01", "currency": "USD",
         "method": "cash", "reference": "hand-4", "bank_account": "1110"},
        source="api", actor_id=None, idempotency_key="manual:2001", commit=False,
    )
    await session.commit()

    assert await u.upsert_order_from_woocommerce(
        str(cid), {**order, "date_paid": "2024-06-02T10:00:00", "transaction_id": "txn-2001"}
    ) == "updated"
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2001")
    connector_payment = next(p for p in st["payments"] if p.get("reference") == "txn-2001")
    assert connector_payment["amount"] == 6.0
    assert st["amount_outstanding"] == 0.0
    assert st["status"] == "paid"


@pytest.mark.asyncio
async def test_woocommerce_paid_order_with_a_balance_again_goes_to_a_person(use_test_session):
    """Once the store's payment is booked, a balance that shows up again because a
    person changed the payments is not quietly left open or silently re-paid: the
    order waits for a person."""
    from types import SimpleNamespace

    from celerp_docs.doc_service import WooCommerceReconciliationRequired
    from celerp_docs.routes import VoidPaymentBody, apply_doc_payment, void_payment

    session = use_test_session
    cid = await _seed_company(session, "WooBalanceAgain")
    order = {**_WOO_PLAIN_ORDER, "id": 2002}
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    doc_id = "doc:woocommerce:order:2002"
    await apply_doc_payment(
        session, cid, doc_id,
        {"amount": 4.0, "payment_date": "2024-06-01", "currency": "USD",
         "method": "cash", "reference": "hand-4", "bank_account": "1110"},
        source="api", actor_id=None, idempotency_key="manual:2002", commit=False,
    )
    await session.commit()
    paid = {**order, "date_paid": "2024-06-02T10:00:00", "transaction_id": "txn-2002"}
    assert await u.upsert_order_from_woocommerce(str(cid), paid) == "updated"

    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2002")
    hand = next(p for p in st["payments"] if p.get("reference") == "hand-4")
    await void_payment(
        doc_id, VoidPaymentBody(payment_index=hand["index"], void_reason="bounced"),
        company_id=cid, _=None, user=SimpleNamespace(id=None), session=session,
    )
    session.expire_all()
    assert (await _state(session, cid, "woocommerce:order:2002"))["amount_outstanding"] == 4.0

    with pytest.raises(WooCommerceReconciliationRequired, match="balance again"):
        await u.upsert_order_from_woocommerce(str(cid), paid)
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2002")
    assert "balance again" in st["woocommerce_reconciliation_required"]
    assert st["amount_outstanding"] == 4.0


@pytest.mark.asyncio
async def test_woocommerce_balance_put_right_in_celerp_releases_the_order(use_test_session):
    """A paid order held because its balance reopened is released by the next
    import once the balance is settled again in Celerp: the note goes and
    stock sync resumes for its products."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from celerp.models.projections import Projection
    from celerp_docs.doc_service import WooCommerceReconciliationRequired
    from celerp_docs.routes import VoidPaymentBody, apply_doc_payment, void_payment
    from celerp_inventory.services import external_link_for_state, upsert_external_product

    session = use_test_session
    cid = await _seed_company(session, "WooBalanceFixed")
    _, root_id = await upsert_external_product(
        str(cid), platform="woocommerce", product_id="771", variation_id=None,
        sku="FIXED-SKU", name="Fixed Product", link_fields={"manage_stock": True},
    )
    now = datetime.now(timezone.utc)
    session.add(Projection(
        company_id=cid, entity_id="item:fixed-lot", entity_type="item",
        version=1, created_at=now, updated_at=now,
        state={"sku": "FIXED-SKU", "name": "Fixed Product", "quantity": 2,
               "status": "available", "sell_by": "piece", "lot": True,
               "parent_item_id": root_id, "allow_splitting": True},
    ))
    await session.commit()
    order = {
        "id": 772, "number": "772", "status": "processing", "currency": "USD",
        "total": "10.00", "total_tax": "0",
        "line_items": [{
            "product_id": 771, "variation_id": 0, "sku": "FIXED-SKU",
            "name": "Fixed Product", "quantity": 1, "total": "10.00", "total_tax": "0",
        }],
        "shipping_lines": [], "fee_lines": [],
    }
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    doc_id = "doc:woocommerce:order:772"
    await apply_doc_payment(
        session, cid, doc_id,
        {"amount": 4.0, "payment_date": "2024-06-01", "currency": "USD",
         "method": "cash", "reference": "hand-4", "bank_account": "1110"},
        source="api", actor_id=None, idempotency_key="manual:772", commit=False,
    )
    await session.commit()
    paid = {**order, "date_paid": "2024-06-02T10:00:00",
            "transaction_id": "txn-772"}
    assert await u.upsert_order_from_woocommerce(str(cid), paid) == "updated"

    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:772")
    hand = next(p for p in st["payments"] if p.get("reference") == "hand-4")
    await void_payment(
        doc_id, VoidPaymentBody(payment_index=hand["index"], void_reason="bounced"),
        company_id=cid, _=None, user=SimpleNamespace(id=None), session=session,
    )
    with pytest.raises(WooCommerceReconciliationRequired, match="balance again") as held:
        await u.upsert_order_from_woocommerce(str(cid), paid)

    async def _paused():
        session.expire_all()
        root = await session.get(
            Projection, {"company_id": cid, "entity_id": root_id}, populate_existing=True,
        )
        return external_link_for_state(root.state or {}, "woocommerce").get(
            "inventory_sync_paused"
        )

    assert await _paused() is True
    assert held.value.signature

    await apply_doc_payment(
        session, cid, doc_id,
        {"amount": 4.0, "payment_date": "2024-06-03", "currency": "USD",
         "method": "bank_transfer", "reference": "hand-4-again", "bank_account": "1110"},
        source="api", actor_id=None, idempotency_key="manual:772:again", commit=False,
    )
    await session.commit()
    assert await u.upsert_order_from_woocommerce(str(cid), paid) == "updated"

    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:772")
    assert st["amount_outstanding"] == 0.0
    assert st.get("woocommerce_reconciliation_required") is None
    assert st.get("woocommerce_reconciliation_signature") is None
    assert await _paused() is False

    # The same source state can be held again if the balance reopens once more.
    st = await _state(session, cid, "woocommerce:order:772")
    again = next(p for p in st["payments"] if p.get("reference") == "hand-4-again")
    await void_payment(
        doc_id, VoidPaymentBody(payment_index=again["index"], void_reason="bounced"),
        company_id=cid, _=None, user=SimpleNamespace(id=None), session=session,
    )
    with pytest.raises(WooCommerceReconciliationRequired, match="balance again"):
        await u.upsert_order_from_woocommerce(str(cid), paid)
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:772")
    assert "balance again" in st["woocommerce_reconciliation_required"]
    assert await _paused() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("settings, expected", [
    ({"woocommerce_deposit_account": "1200", "stripe_deposit_account": "1055"}, "1200"),
    ({"stripe_deposit_account": "1055"}, "1055"),
    ({}, "1110"),
])
async def test_woocommerce_payment_books_to_the_chosen_deposit_account(use_test_session, settings, expected):
    """Store payments land on the connector's own deposit account, else the
    company's online-payments default, else Cash."""
    from celerp.models.company import Company

    session = use_test_session
    cid = await _seed_company(session, "WooDeposit")
    company = await session.get(Company, cid)
    company.settings = {**(company.settings or {}), **settings}
    await session.flush()
    order = {**_WOO_PLAIN_ORDER, "id": 2002, "date_paid": "2024-06-02T10:00:00"}
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    st = await _state(session, cid, "woocommerce:order:2002")
    assert st["status"] == "paid"
    assert [p["bank_account"] for p in st["payments"]] == [expected]


async def _emit_item(session, cid, entity_id, state):
    from celerp.events.engine import emit_event
    await emit_event(
        session, company_id=cid, entity_id=entity_id, entity_type="item",
        event_type="item.created", data=state, actor_id=None, location_id=None,
        source="api", idempotency_key=f"create:{entity_id}", metadata_={},
    )


@pytest.mark.asyncio
async def test_set_external_link_unchanged_is_a_noop_even_when_identity_is_contested(use_test_session):
    """Re-sending the link an item already holds changes nothing, so it cannot
    fail on a conflict that a different row introduced in the meantime."""
    from celerp_inventory.services import set_external_link

    session = use_test_session
    cid = await _seed_company(session, "LinkNoop")
    link = {"product_id": "500", "sync_enabled": True, "remote_deleted": False}
    await _emit_item(session, cid, "item:first", {
        "sku": "NOOP-A", "name": "First", "sell_by": "piece", "external_links": {"woocommerce": link},
    })
    await _emit_item(session, cid, "item:second", {
        "sku": "NOOP-B", "name": "Second", "sell_by": "piece", "external_links": {"woocommerce": dict(link)},
    })
    await session.flush()
    assert await set_external_link(session, cid, "item:first", "woocommerce", link) == link


@pytest.mark.asyncio
async def test_resolve_external_product_point_lookups(use_test_session):
    """Identity resolves by external link first, then by one catalog SKU
    compared case- and whitespace-insensitively, and an ambiguous SKU is refused."""
    from celerp_inventory.services import resolve_external_product

    session = use_test_session
    cid = await _seed_company(session, "ResolveLookup")
    await _emit_item(session, cid, "item:linked", {
        "sku": "Linked-1", "name": "Linked", "sell_by": "piece",
        "external_links": {"woocommerce": {"product_id": "600", "sync_enabled": True, "remote_deleted": False}},
    })
    await _emit_item(session, cid, "item:plain", {"sku": "Plain-2", "name": "Plain", "sell_by": "piece"})
    await _emit_item(session, cid, "item:dup-a", {"sku": "Dup-3", "name": "Dup A", "sell_by": "piece"})
    await _emit_item(session, cid, "item:dup-b", {"sku": "dup-3", "name": "Dup B", "sell_by": "piece"})
    await session.flush()

    by_link = await resolve_external_product(session, cid, "woocommerce", "600", sku="something-else")
    assert by_link.entity_id == "item:linked"
    by_sku = await resolve_external_product(session, cid, "woocommerce", "601", sku="  PLAIN-2 ")
    assert by_sku.entity_id == "item:plain"
    assert await resolve_external_product(session, cid, "woocommerce", "602", sku="missing") is None
    with pytest.raises(ValueError, match="multiple catalog products"):
        await resolve_external_product(session, cid, "woocommerce", "603", sku="DUP-3")


@pytest.mark.asyncio
async def test_load_catalog_family_rows_matches_the_whole_catalog_inference(use_test_session):
    """Loading only the rows that can share the anchor's family yields the same
    family a scan of the whole catalog infers, and leaves unrelated items out."""
    from celerp.models.projections import Projection
    from celerp_inventory.services import catalog_family_rows, load_catalog_family_rows

    session = use_test_session
    cid = await _seed_company(session, "FamilyLoad")
    await _emit_item(session, cid, "item:fam-root", {
        "sku": "FAM-1", "name": "Family Root", "sell_by": "piece", "_catalog_sku_aliases": ["OLD-FAM"],
    })
    await _emit_item(session, cid, "item:fam-pinned", {
        "sku": "FAM-1", "name": "Pinned Lot", "sell_by": "piece", "lot": True, "catalog_item_id": "item:fam-root",
    })
    await _emit_item(session, cid, "item:fam-legacy", {
        "sku": "fam-1", "name": "Legacy Lot", "sell_by": "piece", "lot": True,
    })
    await _emit_item(session, cid, "item:fam-alias", {
        "sku": "OLD-FAM", "name": "Alias Lot", "sell_by": "piece", "lot": True,
    })
    for n in range(5):
        await _emit_item(session, cid, f"item:other-{n}", {"sku": f"OTHER-{n}", "name": f"Other {n}", "sell_by": "piece"})
    await session.flush()

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "item"
    ))).scalars().all()
    anchor = next(r for r in rows if r.entity_id == "item:fam-root")
    expected = {r.entity_id for r in catalog_family_rows(list(rows), anchor)}
    loaded = {r.entity_id for r in await load_catalog_family_rows(session, cid, anchor)}
    assert loaded == expected
    assert "item:fam-root" in loaded and "item:fam-pinned" in loaded
    assert not any(e.startswith("item:other-") for e in loaded)


@pytest.mark.asyncio
async def test_sku_lookups_stay_exact_when_sql_cannot_narrow(use_test_session):
    """A SKU whose case folding differs from SQL lower() (on either side of the
    comparison), or one padded with whitespace SQL trim() leaves alone, still
    resolves and still lands in its catalog family: narrowing never drops a row
    the exact check would keep."""
    from celerp.models.projections import Projection
    from celerp_inventory.services import (
        catalog_family_rows, load_catalog_family_rows, resolve_external_product,
    )

    session = use_test_session
    cid = await _seed_company(session, "SkuExact")
    await _emit_item(session, cid, "item:sharp-root", {
        "sku": "GRÖSSE-L", "name": "Sharp Root", "sell_by": "piece",
    })
    await _emit_item(session, cid, "item:sharp-lot", {
        "sku": "Größe-L", "name": "Sharp Lot", "sell_by": "piece", "lot": True,
    })
    await _emit_item(session, cid, "item:tabbed", {
        "sku": "Tab-9\t", "name": "Tabbed", "sell_by": "piece",
    })
    await _emit_item(session, cid, "item:gross-root", {
        "sku": "GROß-1", "name": "Gross Root", "sell_by": "piece",
    })
    await _emit_item(session, cid, "item:gross-lot", {
        "sku": "gross-1", "name": "Gross Lot", "sell_by": "piece", "lot": True,
    })
    await session.flush()

    by_sharp = await resolve_external_product(session, cid, "woocommerce", "700", sku="Größe-L")
    assert by_sharp is not None and by_sharp.entity_id == "item:sharp-root"
    by_tab = await resolve_external_product(session, cid, "woocommerce", "701", sku="tab-9")
    assert by_tab is not None and by_tab.entity_id == "item:tabbed"
    by_gross = await resolve_external_product(session, cid, "woocommerce", "702", sku="gross-1")
    assert by_gross is not None and by_gross.entity_id == "item:gross-root"

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid, Projection.entity_type == "item"
    ))).scalars().all()
    anchor = next(r for r in rows if r.entity_id == "item:sharp-root")
    expected = {r.entity_id for r in catalog_family_rows(list(rows), anchor)}
    assert "item:sharp-lot" in expected
    loaded = {r.entity_id for r in await load_catalog_family_rows(session, cid, anchor)}
    assert loaded == expected

    lot = next(r for r in rows if r.entity_id == "item:gross-lot")
    expected = {r.entity_id for r in catalog_family_rows(list(rows), lot)}
    assert "item:gross-root" in expected
    loaded = {r.entity_id for r in await load_catalog_family_rows(session, cid, lot)}
    assert loaded == expected


async def _attention_run(session, cid, entries):
    """The latest orders sync left these WooCommerce orders waiting on a person."""
    import json
    from datetime import datetime, timezone

    from celerp.models.sync_run import SyncRun

    now = datetime.now(timezone.utc)
    session.add(SyncRun(
        company_id=str(cid), connector="woocommerce", entity="orders",
        started_at=now, finished_at=now, created_count=0, updated_count=0,
        skipped_count=0, status="success", attention_json=json.dumps(entries),
    ))
    await session.flush()


def _entry(order_id, exc):
    return {"id": order_id, "label": f"Order {order_id}", "reason": str(exc),
            "signature": exc.signature}


@pytest.mark.asyncio
async def test_woocommerce_partial_refund_on_issued_order_needs_reconciliation(use_test_session):
    """A partial refund leaves status, lines and total unchanged; the refunds
    list is its only trace. It still stops for a person."""
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    session = use_test_session
    cid = await _seed_company(session, "WooPartialRefund")
    order = {**_WOO_PLAIN_ORDER, "id": 2101}
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"

    with pytest.raises(WooCommerceReconciliationRequired, match="has a refund") as exc:
        await u.upsert_order_from_woocommerce(
            str(cid), {**order, "refunds": [{"id": 55, "total": "-4.00"}]}
        )
    assert exc.value.signature
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2101")
    assert st["woocommerce_reconciliation_required"] == str(exc.value)
    assert st["woocommerce_reconciliation_signature"] == exc.value.signature


@pytest.mark.asyncio
async def test_woocommerce_change_after_issue_is_recorded_on_the_order(use_test_session):
    """A change to an issued order stops for a person and the order itself
    records which change is waiting."""
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    session = use_test_session
    cid = await _seed_company(session, "WooChangedAfterIssue")
    order = {**_WOO_PLAIN_ORDER, "id": 2105}
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"

    with pytest.raises(WooCommerceReconciliationRequired, match="after the Celerp invoice") as exc:
        await u.upsert_order_from_woocommerce(str(cid), {**order, "total": "99.00"})
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2105")
    assert st["woocommerce_reconciliation_required"] == str(exc.value)
    assert st["woocommerce_reconciliation_signature"] == exc.value.signature


@pytest.mark.asyncio
async def test_marking_a_change_that_was_replaced_is_refused(use_test_session):
    """The order changed again in WooCommerce after the list was built: marking
    the earlier change is refused and the newer change still waits."""
    from fastapi import HTTPException

    from celerp_connectors.routes import _set_order_reconciled
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    session = use_test_session
    cid = await _seed_company(session, "WooReplacedChange")
    order = {**_WOO_PLAIN_ORDER, "id": 2106}
    await u.upsert_order_from_woocommerce(str(cid), order)
    refunded = {**order, "refunds": [{"id": 59, "total": "-4.00"}]}
    with pytest.raises(WooCommerceReconciliationRequired) as first:
        await u.upsert_order_from_woocommerce(str(cid), refunded)
    await _attention_run(session, cid, [_entry("2106", first.value)])
    with pytest.raises(WooCommerceReconciliationRequired) as second:
        await u.upsert_order_from_woocommerce(str(cid), {
            **refunded, "refunds": [*refunded["refunds"], {"id": 60, "total": "-1.00"}],
        })

    with pytest.raises(HTTPException) as refused:
        await _set_order_reconciled(session, cid, "2106", first.value.signature, None)
    assert refused.value.status_code == 409
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2106")
    assert st.get("woocommerce_reconciled_signature") is None
    assert st["woocommerce_reconciliation_required"] == str(second.value)


@pytest.mark.asyncio
async def test_woocommerce_refund_on_new_order_is_not_issued_or_paid(use_test_session):
    """A paid order that already carries a refund when first seen is imported
    as a draft for a person to reconcile, never issued or paid in full."""
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    session = use_test_session
    cid = await _seed_company(session, "WooRefundFirstSeen")
    order = {
        **_WOO_PLAIN_ORDER, "id": 2102, "status": "processing",
        "date_paid": "2024-06-02T10:00:00", "refunds": [{"id": 56, "total": "-4.00"}],
    }
    with pytest.raises(WooCommerceReconciliationRequired, match="has a refund"):
        await u.upsert_order_from_woocommerce(str(cid), order)
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2102")
    assert not st.get("finalized")
    assert not st.get("payments")


@pytest.mark.asyncio
async def test_woocommerce_reconciled_order_is_left_alone_until_it_changes(use_test_session):
    """Mark reconciled clears the order's note and the import leaves exactly that
    source state alone; Undo puts the note back, and any later change in
    WooCommerce needs a person again."""
    from celerp_connectors.routes import _set_order_reconciled
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    session = use_test_session
    cid = await _seed_company(session, "WooReconciled")
    order = {**_WOO_PLAIN_ORDER, "id": 2103}
    await u.upsert_order_from_woocommerce(str(cid), order)
    refunded = {**order, "refunds": [{"id": 57, "total": "-4.00"}]}
    with pytest.raises(WooCommerceReconciliationRequired) as first:
        await u.upsert_order_from_woocommerce(str(cid), refunded)
    await _attention_run(session, cid, [_entry("2103", first.value)])

    marked = await _set_order_reconciled(session, cid, "2103", first.value.signature, None)
    assert marked["entry"]["reconciled"] is True
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2103")
    assert st["woocommerce_reconciliation_required"] is None
    assert await u.upsert_order_from_woocommerce(str(cid), refunded) == "noop"

    undone = await _set_order_reconciled(session, cid, "2103", None, None)
    assert undone["entry"]["reconciled"] is False
    session.expire_all()
    st = await _state(session, cid, "woocommerce:order:2103")
    assert st["woocommerce_reconciliation_required"] == str(first.value)
    with pytest.raises(WooCommerceReconciliationRequired):
        await u.upsert_order_from_woocommerce(str(cid), refunded)

    await _set_order_reconciled(session, cid, "2103", first.value.signature, None)
    with pytest.raises(WooCommerceReconciliationRequired) as second:
        await u.upsert_order_from_woocommerce(str(cid), {
            **refunded, "refunds": [*refunded["refunds"], {"id": 58, "total": "-1.00"}],
        })
    assert second.value.signature != first.value.signature


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id, entry, signature, status", [
    ("abc", None, "sig", 422),
    ("2104", None, "sig", 404),
    ("2104", {"id": "2104", "reason": "no stock"}, "sig", 409),
    ("2104", {"id": "2104", "reason": "refund", "signature": "current"}, "stale", 409),
])
async def test_mark_reconciled_refusals(use_test_session, order_id, entry, signature, status):
    """Only an order on the list, carrying a change a person reconciles, at the
    state the person reviewed, can be marked."""
    from fastapi import HTTPException

    from celerp_connectors.routes import _set_order_reconciled

    session = use_test_session
    cid = await _seed_company(session, "WooMarkRefused")
    if entry is not None:
        await _attention_run(session, cid, [entry])
    with pytest.raises(HTTPException) as exc:
        await _set_order_reconciled(session, cid, order_id, signature, None)
    assert exc.value.status_code == status


async def _woo_stocked_product(session, cid, product_id, sku):
    """A WooCommerce-linked catalog product with one available lot."""
    from datetime import datetime, timezone

    from celerp.models.projections import Projection
    from celerp_inventory.services import upsert_external_product

    _, root_id = await upsert_external_product(
        str(cid), platform="woocommerce", product_id=str(product_id), variation_id=None,
        sku=sku, name=f"Product {sku}", link_fields={"manage_stock": True},
    )
    now = datetime.now(timezone.utc)
    session.add(Projection(
        company_id=cid, entity_id=f"item:{sku.lower()}-lot", entity_type="item",
        version=1, created_at=now, updated_at=now,
        state={"sku": sku, "name": f"Product {sku}", "quantity": 4,
               "status": "available", "sell_by": "piece", "lot": True,
               "parent_item_id": root_id, "allow_splitting": True},
    ))
    await session.commit()
    return root_id


def _woo_stocked_order(order_id, product_id, sku, **fields):
    return {
        "id": order_id, "number": str(order_id), "status": "on-hold", "currency": "USD",
        "total": "10.00", "total_tax": "0",
        "line_items": [{
            "product_id": product_id, "variation_id": 0, "sku": sku,
            "name": f"Product {sku}", "quantity": 1, "total": "10.00", "total_tax": "0",
        }],
        "shipping_lines": [], "fee_lines": [],
        **fields,
    }


async def _woo_stock_paused(session, cid, root_id):
    from celerp.models.projections import Projection
    from celerp_inventory.services import external_link_for_state

    session.expire_all()
    root = await session.get(
        Projection, {"company_id": cid, "entity_id": root_id}, populate_existing=True,
    )
    return external_link_for_state(root.state or {}, "woocommerce").get("inventory_sync_paused")


@pytest.mark.asyncio
async def test_woocommerce_change_reverted_to_the_reconciled_state_releases_its_hold(use_test_session):
    """A person reconciles a refund, the store changes the order again, then
    the store puts it back exactly as reconciled. The later hold no longer
    applies: the order is reconciled again, stock sync resumes and Undo still
    works."""
    from celerp_connectors.routes import _set_order_reconciled
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    session = use_test_session
    cid = await _seed_company(session, "WooRevertedChange")
    root_id = await _woo_stocked_product(session, cid, 781, "REVERT-SKU")
    order = _woo_stocked_order(782, 781, "REVERT-SKU")
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    refunded = {**order, "refunds": [{"id": 70, "total": "-4.00"}]}
    with pytest.raises(WooCommerceReconciliationRequired) as first:
        await u.upsert_order_from_woocommerce(str(cid), refunded)
    await _attention_run(session, cid, [_entry("782", first.value)])
    await _set_order_reconciled(session, cid, "782", first.value.signature, None)
    assert await _woo_stock_paused(session, cid, root_id) is False

    with pytest.raises(WooCommerceReconciliationRequired):
        await u.upsert_order_from_woocommerce(str(cid), {
            **refunded, "refunds": [*refunded["refunds"], {"id": 71, "total": "-1.00"}],
        })
    assert await _woo_stock_paused(session, cid, root_id) is True

    assert await u.upsert_order_from_woocommerce(str(cid), refunded) == "noop"
    st = await _state(session, cid, "woocommerce:order:782")
    assert st["woocommerce_reconciliation_required"] is None
    assert st["woocommerce_reconciliation_signature"] == first.value.signature
    assert st["woocommerce_reconciled_signature"] == first.value.signature
    assert await _woo_stock_paused(session, cid, root_id) is False

    undone = await _set_order_reconciled(session, cid, "782", None, None)
    assert undone["entry"]["reconciled"] is False
    assert await _woo_stock_paused(session, cid, root_id) is True


@pytest.mark.asyncio
async def test_woocommerce_order_refunded_when_first_seen_pauses_its_products(use_test_session):
    """An order already refunded the first time Celerp sees it still pauses
    outbound stock for its products until a person reconciles it, and the
    order line records the catalog product without claiming a physical unit."""
    from celerp_docs.doc_service import WooCommerceReconciliationRequired

    session = use_test_session
    cid = await _seed_company(session, "WooRefundedFirstSeen")
    root_id = await _woo_stocked_product(session, cid, 791, "FIRST-SKU")
    order = _woo_stocked_order(
        792, 791, "FIRST-SKU", status="refunded",
        refunds=[{"id": 72, "total": "-10.00"}],
    )
    with pytest.raises(WooCommerceReconciliationRequired):
        await u.upsert_order_from_woocommerce(str(cid), order)
    st = await _state(session, cid, "woocommerce:order:792")
    line = st["line_items"][0]
    assert line.get("catalog_item_id") == root_id
    assert not line.get("item_id")
    assert await _woo_stock_paused(session, cid, root_id) is True


@pytest.mark.asyncio
async def test_woocommerce_order_missing_from_the_store_waits_on_a_person(use_test_session):
    """An imported order WooCommerce no longer returns is held for a person and
    never voided: its products pause, a mark on it stays while the order is
    still missing, and the order restored in the store clears the hold and the
    mark, so a later deletion needs a person again."""
    from celerp_connectors.routes import _set_order_reconciled

    session = use_test_session
    cid = await _seed_company(session, "WooMissingOrder")
    root_id = await _woo_stocked_product(session, cid, 801, "GONE-SKU")
    order = _woo_stocked_order(802, 801, "GONE-SKU")
    assert await u.upsert_order_from_woocommerce(str(cid), order) == "created"
    assert "802" in await u.list_imported_woocommerce_order_ids(str(cid))

    entry = await u.hold_missing_woocommerce_order(str(cid), "802")
    assert entry == {
        "id": "802", "label": "Order 802", "signature": "gone",
        "reason": "WooCommerce Order 802 no longer exists in the store; reconcile it by hand",
    }
    st = await _state(session, cid, "woocommerce:order:802")
    assert st["status"] != "void"
    assert st["woocommerce_reconciliation_required"] == entry["reason"]
    assert await _woo_stock_paused(session, cid, root_id) is True

    await _attention_run(session, cid, [entry])
    await _set_order_reconciled(session, cid, "802", "gone", None)
    assert await _woo_stock_paused(session, cid, root_id) is False
    assert await u.hold_missing_woocommerce_order(str(cid), "802") == {**entry, "reconciled": True}
    assert await _woo_stock_paused(session, cid, root_id) is False

    await u.upsert_order_from_woocommerce(str(cid), order)
    st = await _state(session, cid, "woocommerce:order:802")
    assert st["woocommerce_reconciliation_required"] is None
    assert st["woocommerce_reconciliation_signature"] is None
    assert st["woocommerce_reconciled_signature"] is None
    assert await _woo_stock_paused(session, cid, root_id) is False
    assert "reconciled" not in await u.hold_missing_woocommerce_order(str(cid), "802")


@pytest.mark.asyncio
async def test_shopify_import_relinks_a_product_after_disconnect(use_test_session):
    """Reconnecting a store and importing again gives the same item back its Shopify link."""
    import httpx
    import respx

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.shopify import ShopifyConnector
    from celerp.models.projections import Projection
    from celerp_inventory.services import (
        detach_external_links_for_platform,
        external_link_for_state,
        upsert_external_product,
    )

    session = use_test_session
    cid = await _seed_company(session, "ShopifyRelink")
    _, entity_id = await upsert_external_product(
        str(cid), platform="shopify", product_id="1", variation_id="10",
        sku="SHOP-RELINK", name="Widget", sale_price=9.99,
    )
    await detach_external_links_for_platform(session, cid, "shopify")
    await session.commit()

    ctx = ConnectorContext(
        company_id=str(cid), access_token="shpat_test", store_handle="relink-store.myshopify.com",
    )
    with respx.mock:
        respx.get("https://relink-store.myshopify.com/admin/api/2024-01/products.json").mock(
            return_value=httpx.Response(200, json={"products": [
                {"id": 1, "title": "Widget", "images": [], "variants": [
                    {"id": 10, "sku": "SHOP-RELINK", "title": "Default Title", "price": "9.99"},
                ]},
            ]})
        )
        result = await ShopifyConnector().sync_products(ctx)

    assert not result.errors
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == cid,
            Projection.entity_type == "item",
        ).execution_options(populate_existing=True)
    )).scalars().all()
    assert [row.entity_id for row in rows] == [entity_id]
    link = external_link_for_state(rows[0].state or {}, "shopify")
    assert link["product_id"] == "1"
    assert link["variant_id"] == "10"


@pytest.mark.asyncio
async def test_queued_woocommerce_stock_push_reads_only_that_product(
    use_test_session, monkeypatch
):
    """A queued stock push for one WooCommerce product reads that product's
    rows, not the whole catalog, and still sends the family's sellable stock."""
    import datetime as dt
    from unittest.mock import AsyncMock

    import httpx
    import respx
    from sqlalchemy import event

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.woocommerce import WooCommerceConnector
    from celerp.models.projections import Projection

    session = use_test_session
    cid = await _seed_company(session, "Pointstock")
    now = dt.datetime.now(dt.timezone.utc)

    def item(entity_id, state):
        session.add(Projection(
            company_id=cid, entity_id=entity_id, entity_type="item",
            state={"status": "available", **state}, version=1, updated_at=now,
        ))

    item("item:shirt", {
        "sku": "SHIRT", "quantity": 2,
        "external_links": {"woocommerce": {"product_id": "10", "manage_stock": True}},
    })
    item("item:shirt-lot", {"sku": "SHIRT", "quantity": 3, "catalog_item_id": "item:shirt"})
    item("item:hat", {
        "sku": "HAT", "quantity": 9,
        "external_links": {"woocommerce": {"product_id": "11", "manage_stock": True}},
    })
    item("item:sock", {"sku": "SOCK", "quantity": 4})
    await session.flush()
    session.expunge_all()

    loaded: list[str] = []

    def on_load(target, _context):
        loaded.append(target.entity_id)

    monkeypatch.setattr(
        "celerp.connectors.upsert.list_items_with_external_id",
        AsyncMock(side_effect=AssertionError("whole catalog loaded")),
    )
    monkeypatch.setattr(
        "celerp.services.outbound_url._resolve_public_addresses",
        AsyncMock(return_value=["93.184.216.34"]),
    )
    ctx = ConnectorContext(
        company_id=str(cid),
        access_token="ck_testkey:cs_testsecret",
        store_handle="https://store.example.com",
    )
    event.listen(Projection, "load", on_load)
    try:
        with respx.mock:
            shirt = respx.put(
                "https://store.example.com/wp-json/wc/v3/products/10"
            ).mock(return_value=httpx.Response(200, json={}))
            result = await WooCommerceConnector().sync_inventory_identity_out(ctx, "10")
    finally:
        event.remove(Projection, "load", on_load)

    assert result.errors is None
    assert result.updated == 1
    assert shirt.calls.last.request.content == b'{"stock_quantity":5}'
    assert set(loaded) == {"item:shirt", "item:shirt-lot"}

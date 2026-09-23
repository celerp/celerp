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

    co = "wm-since-1"
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

    ctx = ConnectorContext(company_id="co-out-1", access_token="t", store_handle="s")

    # direction=both -> the outbound method IS dispatched.
    r1 = await sync_runner.run_sync(_Stub(), ctx, "products_out", direction=SyncDirection.BOTH)
    assert calls == ["products_out"] and r1.created == 1

    # direction=inbound -> the outbound entity is BLOCKED (method not called again).
    r2 = await sync_runner.run_sync(_Stub(), ctx, "products_out", direction=SyncDirection.INBOUND)
    assert calls == ["products_out"]  # unchanged — the push did not run
    assert r2.errors and "blocked by direction" in r2.errors[0]


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
async def test_woocommerce_same_sku_cannot_steal_live_external_identity(use_test_session):
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

    with pytest.raises(ValueError, match="manual financial/inventory reconciliation"):
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

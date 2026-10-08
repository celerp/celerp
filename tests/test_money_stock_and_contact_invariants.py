# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Currency precision, stock audit and contact reference invariants."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import types
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.lot_origin import recognize_opening_lots
from stock_books import older_release_lot
from test_helpers import make_authed_token, perm_setup, provision_company_books


async def _auth_company(session, currency: str = "USD") -> dict:
    cid, uid = uuid.uuid4(), uuid.uuid4()
    session.add(Company(id=cid, name="InvariantCo", slug=f"inv-{cid.hex[:8]}", settings={"currency": currency}))
    session.add(User(id=uid, email=f"inv-{uid.hex[:8]}@example.test", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await provision_company_books(session, cid)
    await session.commit()
    token = await make_authed_token(session, str(uid), str(cid), "admin")
    return {"company_id": cid, "user_id": uid, "headers": {"Authorization": f"Bearer {token}"}}


async def _api_item(client, auth, sku: str, qty: float, cost_total: float = 0) -> str:
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": sku, "name": sku, "quantity": qty, "sell_by": "piece",
        "status": "available", "cost_total": cost_total,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _invoice(client, auth, item_id: str, sku: str, qty: float) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice",
        "line_items": [{"entity_id": item_id, "sku": sku, "name": sku,
                        "quantity": qty, "unit_price": 5.0, "sell_by": "piece"}],
        "total": qty * 5.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc_id


@pytest.mark.asyncio
async def test_kwd_fulfillment_true_up_keeps_fils(client, session):
    auth = await _auth_company(session, "KWD")
    sku = f"KWD-TU-{uuid.uuid4().hex[:6]}"
    lot_a = await _api_item(client, auth, sku, 1, 1.000)
    lot_b = await _api_item(client, auth, sku, 1, 1.000)
    await _api_item(client, auth, sku, 1, 1.004)

    doc1 = await _invoice(client, auth, lot_a, sku, 2)
    doc2 = await _invoice(client, auth, lot_b, sku, 1)
    r = await client.post(f"/docs/{doc2}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot_b]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc1}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot_a]})
    assert r.status_code == 200, r.text

    session.expire_all()
    adj = await session.get(
        Projection, {"company_id": auth["company_id"],
                     "entity_id": f"je:auto:{doc1}:cogs-adj:fulfill-0:l0"})
    assert adj is not None and adj.state.get("status") == "posted"
    by_account = {e["account"]: (e["debit"], e["credit"]) for e in adj.state["entries"]}
    assert by_account["5100"] == (0.004, 0.0)
    assert by_account["1130-OB"] == (0.0, 0.004)


@pytest.mark.asyncio
async def test_kwd_manual_overpayment_uses_fils_not_cent_tolerance(client, session):
    auth = await _auth_company(session, "KWD")
    sku = f"KWD-PAY-{uuid.uuid4().hex[:6]}"
    item_id = await _api_item(client, auth, sku, 1)
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "memo",
        "line_items": [{"entity_id": item_id, "sku": sku, "name": sku,
                        "quantity": 1, "unit_price": 10.0, "sell_by": "piece"}],
        "total": 10.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])).status_code == 200

    r = await client.post(f"/docs/{doc_id}/payment", headers=auth["headers"], json={
        "amount": 10.009, "payment_date": "2026-07-01",
        "method": "cash", "bank_account": "1111",
    })
    assert r.status_code == 409, r.text
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert not [p for p in (doc.get("payments") or []) if p.get("status") != "deleted"]
    assert doc["amount_outstanding"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_kwd_opening_inventory_posts_sub_cent_gap(client, session):
    auth = await _auth_company(session, "KWD")
    await older_release_lot(session, auth["company_id"], auth["user_id"], 0.005)
    await auto_je.book_opening_inventory(
        session, company_id=auth["company_id"], user_id=auth["user_id"], in_production=Decimal("0"))
    await session.commit()
    session.expire_all()
    row = await session.get(
        Projection, {"company_id": auth["company_id"],
                     "entity_id": f"je:auto:opening-inventory:{auth['company_id']}"})
    assert row is not None and row.state.get("status") == "posted"
    entries = [(e["account"], e["debit"], e["credit"]) for e in row.state["entries"]]
    assert entries == [("1130-OB", 0.005, 0.0), ("3200", 0.0, 0.005)]


async def _seed_company(factory) -> uuid.UUID:
    cid = uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=cid, name="AuditRaceCo", slug=f"audit-{cid.hex[:8]}", settings={"currency": "USD"}))
        await s.commit()
    return cid


async def _seed_user(factory) -> uuid.UUID:
    uid = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=uid, email=f"audit-{uid.hex[:8]}@example.test", name="Manager", is_active=True))
        await s.commit()
    return uid


async def _seed_chart(factory, company_id) -> None:
    from celerp_accounting.routes import seed_chart_of_accounts_hook
    async with factory() as s:
        await seed_chart_of_accounts_hook(session=s, company_id=company_id)
        await s.commit()


async def _seed_item(factory, company_id, item_id: str, *, qty: float, cost_total: float) -> None:
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=item_id, entity_type="item",
            event_type="item.created",
            data={"sku": item_id.split(":")[-1], "name": item_id, "quantity": qty,
                  "cost_total": cost_total, "sell_by": "piece", "status": "available",
                  "inventory_type": "stocked"},
            actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await recognize_opening_lots(s, company_id, [item_id], None, f"seed:{item_id}")
        await s.commit()


async def _seed_audit(factory, company_id, list_id: str, item_id: str, *,
                      status: str, counted_qty: float, prior_qty=None, adjusted=False,
                      adjustment_unit_cost=None) -> None:
    line = {"item_id": item_id, "sku": item_id.split(":")[-1],
            "quantity": 10, "counted_qty": counted_qty}
    if prior_qty is not None:
        line["prior_qty"] = prior_qty
    if adjusted:
        line["adjusted"] = True
    if adjustment_unit_cost is not None:
        line["adjustment_unit_cost"] = adjustment_unit_cost
    data = {"list_type": "audit", "status": status, "ref_id": list_id.split(":")[-1],
            "line_items": [line], "adjust_count": 1 if adjusted else 0}
    if adjusted:
        data["result"] = "stock_adjusted"
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=list_id, entity_type="list",
            event_type="list.created", data=data, actor_id=None, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()


async def _cleanup(factory, company_id, user_id) -> None:
    from celerp_accounting.models import Account, BankAccount
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(BankAccount).where(BankAccount.company_id == company_id))
        await s.execute(delete(Account).where(Account.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


@pytest.mark.asyncio
async def test_audit_adjust_uses_fresh_locked_item_state(_db_engine):
    from celerp_docs.routes import adjust_audit

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    await _seed_chart(factory, company_id)
    item_id, list_id = "item:AUD-CONC", "list:AUD-CONC"
    await _seed_item(factory, company_id, item_id, qty=10, cost_total=100)
    await _seed_audit(factory, company_id, list_id, item_id, status="finalized", counted_qty=5)
    user = types.SimpleNamespace(id=user_id)
    stock, audit = factory(), factory()
    try:
        await emit_event(
            stock, company_id=company_id, entity_id=item_id, entity_type="item",
            event_type="item.quantity.adjusted", data={"new_qty": 8, "reason": "concurrent"},
            actor_id=user_id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        task = asyncio.create_task(
            adjust_audit(list_id, company_id=company_id, _=None, user=user, session=audit))
        await asyncio.sleep(0.3)
        assert not task.done()
        await stock.commit()

        result = await asyncio.wait_for(task, timeout=10)
        assert result["shrinkage_value"] == 30.0
        async with factory() as s:
            item = await s.get(Projection, {"company_id": company_id, "entity_id": item_id})
            je = await s.get(
                Projection, {"company_id": company_id, "entity_id": f"je:auto:{list_id}:audit:0"})
            assert item.state["quantity"] == 5
            by_account = {e["account"]: (e["debit"], e["credit"]) for e in je.state["entries"]}
            assert by_account["6970"] == (30.0, 0.0)
            assert by_account["1130-OB"] == (0.0, 30.0)
    finally:
        await stock.close()
        await audit.close()
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_audit_undo_refuses_to_overwrite_later_stock_activity(_db_engine):
    from fastapi import HTTPException
    from celerp_docs.routes import undo_audit_adjust

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    item_id, list_id = "item:AUD-UNDO", "list:AUD-UNDO"
    await _seed_item(factory, company_id, item_id, qty=4, cost_total=40)
    await _seed_audit(
        factory, company_id, list_id, item_id, status="closed",
        counted_qty=5, prior_qty=10, adjusted=True)
    user = types.SimpleNamespace(id=user_id)
    try:
        async with factory() as s:
            with pytest.raises(HTTPException) as exc:
                await undo_audit_adjust(list_id, company_id=company_id, _=None, user=user, session=s)
            assert exc.value.status_code == 409
            await s.rollback()
        async with factory() as s:
            item = await s.get(Projection, {"company_id": company_id, "entity_id": item_id})
            audit = await s.get(Projection, {"company_id": company_id, "entity_id": list_id})
            assert item.state["quantity"] == 4
            assert audit.state["status"] == "closed"
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_audit_undo_refuses_after_later_cost_change(_db_engine):
    from fastapi import HTTPException
    from celerp_docs.routes import undo_audit_adjust

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    item_id, list_id = "item:AUD-COST", "list:AUD-COST"
    await _seed_item(factory, company_id, item_id, qty=5, cost_total=60)
    await _seed_audit(
        factory, company_id, list_id, item_id, status="closed",
        counted_qty=5, prior_qty=10, adjusted=True, adjustment_unit_cost=10)
    user = types.SimpleNamespace(id=user_id)
    try:
        async with factory() as db:
            with pytest.raises(HTTPException) as exc:
                await undo_audit_adjust(list_id, company_id=company_id, _=None, user=user, session=db)
            assert exc.value.status_code == 409
            assert "cost changed" in str(exc.value.detail)
            await db.rollback()
        async with factory() as db:
            item = await db.get(Projection, {"company_id": company_id, "entity_id": item_id})
            assert item.state["quantity"] == 5
            assert item.state["cost_total"] == 60
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_refinalize_waits_for_a_cost_correction_in_flight(_db_engine):
    """A re-finalized invoice keeps its number, so nothing else makes it wait."""
    from celerp_docs.routes import finalize_document
    from celerp_inventory.services import restate_item_cost

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    await _seed_chart(factory, company_id)
    item_id, doc_id = "item:FIN-RACE", "doc:FIN-RACE"
    await _seed_item(factory, company_id, item_id, qty=1, cost_total=100)
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=doc_id, entity_type="doc", event_type="doc.created",
            data={"doc_type": "invoice", "status": "draft", "ref_id": "INV-RACE", "revert_count": 1,
                  "currency": "USD",
                  "line_items": [{"sku": "FIN-RACE", "name": "FIN-RACE", "quantity": 1,
                                  "unit_price": 200.0, "entity_id": item_id, "item_id": item_id}],
                  "subtotal": 200.0, "total": 200.0},
            actor_id=user_id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()
    correction, finalize = factory(), factory()
    try:
        await restate_item_cost(
            correction, company_id, item_id, event_type="item.updated",
            data={"fields_changed": {"cost_total": {"old": 100, "new": 120}}},
            actor_id=user_id, source="test", idempotency_key=str(uuid.uuid4()),
        )
        task = asyncio.create_task(finalize_document(
            doc_id, company_id, types.SimpleNamespace(id=user_id), finalize, commit=True))
        await asyncio.sleep(0.3)
        assert not task.done()
        await correction.commit()
        await asyncio.wait_for(task, timeout=10)
        async with factory() as s:
            assert await _account_net(s, company_id, "5100") == 120.0
    finally:
        await correction.close()
        await finalize.close()
        await _cleanup(factory, company_id, user_id)


async def _contact(client, h, name: str) -> str:
    r = await client.post("/crm/contacts", headers=h, json={"name": name, "contact_type": "customer"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_delete_refuses_contact_named_on_a_deal(client, session):
    ctx = await perm_setup(client, session)
    h = ctx["admin_h"]
    source = await _contact(client, h, "Deal Old")
    company_id = uuid.UUID((await client.get("/companies/me", headers=h)).json()["id"])
    # A deal as the optional sales-funnel module stores it (that module is not loaded here).
    deal_id = f"deal:{uuid.uuid4()}"
    deal = {"name": "Open deal", "stage": "lead", "contact_id": source}
    now = datetime.now(timezone.utc)
    session.add(LedgerEntry(company_id=company_id, entity_id=deal_id, entity_type="deal",
                            event_type="crm.deal.created", data=deal, actor_id=None, location_id=None,
                            source="test", idempotency_key=str(uuid.uuid4()), metadata_={}))
    session.add(Projection(company_id=company_id, entity_id=deal_id, entity_type="deal", state=deal,
                           version=1, location_id=None, created_at=now, updated_at=now))
    await session.commit()

    blocked = await client.post("/crm/contacts/bulk/delete", headers=h, json={"contact_ids": [source]})
    assert blocked.status_code == 422, blocked.text
    assert "1 deal(s)" in blocked.json()["detail"]



def test_kwd_projection_keeps_four_fils_outstanding():
    from celerp_docs.doc_projections import apply_documents_event
    result = apply_documents_event(
        {"doc_type": "invoice", "status": "final", "currency": "KWD",
         "total": 1.000, "amount_paid": 0.0, "amount_outstanding": 1.000},
        "doc.payment.received",
        {"amount": 0.996, "payment_date": "2026-09-29", "index": 0},
    )
    assert result["status"] == "partial"
    assert result["amount_outstanding"] == 0.004


@pytest.mark.asyncio
async def test_bulk_payment_rejects_mixed_document_currencies(client, session):
    auth = await _auth_company(session, "USD")
    doc_ids = []
    for currency in ("USD", "EUR"):
        doc_id = f"doc:{uuid.uuid4()}"
        doc_ids.append(doc_id)
        await emit_event(
            session, company_id=auth["company_id"], entity_id=doc_id, entity_type="doc",
            event_type="doc.created",
            data={"doc_type": "invoice", "status": "final", "currency": currency,
                  "total": 10.0, "amount_outstanding": 10.0, "line_items": []},
            actor_id=auth["user_id"], location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
    await session.commit()
    r = await client.post("/docs/bulk-payment", headers=auth["headers"], json={
        "doc_ids": doc_ids, "amount": 10.0, "payment_date": "2026-09-29",
        "bank_account": "1111",
    })
    assert r.status_code == 422, r.text
    assert "same currency" in r.json()["detail"]


@pytest.mark.asyncio
async def test_payment_rejects_currency_that_differs_from_document(client, session):
    auth = await _auth_company(session, "KWD")
    sku = f"KWD-CUR-{uuid.uuid4().hex[:6]}"
    item_id = await _api_item(client, auth, sku, 1)
    doc_id = await _invoice(client, auth, item_id, sku, 1)
    r = await client.post(f"/docs/{doc_id}/payment", headers=auth["headers"], json={
        "amount": 1.0, "payment_date": "2026-09-29", "bank_account": "1111",
        "currency": "USD",
    })
    assert r.status_code == 422, r.text
    assert "does not match document currency" in r.json()["detail"]


def test_payment_form_uses_document_currency_precision_and_rate():
    from fasthtml.common import to_xml
    from ui.routes.documents import _payment_section
    html = to_xml(_payment_section({
        "entity_id": "doc:fx", "doc_type": "invoice", "status": "final",
        "currency": "KWD", "conversion_rate": 36.5,
        "total": 1.004, "amount_paid": 0, "amount_outstanding": 1.004,
        "payments": [],
    }, bank_accounts=[]))
    assert 'value="1.004"' in html
    assert 'step="0.001"' in html
    assert 'value="36.5"' in html


@pytest.mark.asyncio
async def test_legacy_audit_undo_without_cost_identity_fails_closed(_db_engine):
    from fastapi import HTTPException
    from celerp_docs.routes import undo_audit_adjust
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    item_id, list_id = "item:AUD-LEGACY", "list:AUD-LEGACY"
    await _seed_item(factory, company_id, item_id, qty=5, cost_total=50)
    await _seed_audit(
        factory, company_id, list_id, item_id, status="closed",
        counted_qty=5, prior_qty=10, adjusted=True)
    user = types.SimpleNamespace(id=user_id)
    try:
        async with factory() as db:
            with pytest.raises(HTTPException) as exc:
                await undo_audit_adjust(list_id, company_id=company_id, _=None, user=user, session=db)
            assert exc.value.status_code == 409
            assert "predates safe cost tracking" in str(exc.value.detail)
            await db.rollback()
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
async def test_credit_application_rejects_mixed_document_currencies(client, session):
    auth = await _auth_company(session, "USD")
    ids = {}
    for doc_type, currency in (("credit_note", "EUR"), ("invoice", "USD")):
        doc_id = f"doc:{uuid.uuid4()}"
        ids[doc_type] = doc_id
        await emit_event(
            session, company_id=auth["company_id"], entity_id=doc_id, entity_type="doc",
            event_type="doc.created",
            data={"doc_type": doc_type, "status": "final", "currency": currency,
                  "total": 10.0, "amount_outstanding": 10.0, "line_items": []},
            actor_id=auth["user_id"], location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
    await session.commit()
    r = await client.post(
        f"/docs/{ids['credit_note']}/apply-to-invoice",
        headers=auth["headers"],
        json={"target_doc_id": ids["invoice"], "amount": 1.0},
    )
    assert r.status_code == 422, r.text
    assert "same currency" in r.json()["detail"]


@pytest.mark.asyncio
async def test_kwd_credit_refund_rejects_sub_minor_unit_amount(client, session):
    auth = await _auth_company(session, "KWD")
    doc_id = f"doc:{uuid.uuid4()}"
    await emit_event(
        session, company_id=auth["company_id"], entity_id=doc_id, entity_type="doc",
        event_type="doc.created",
        data={"doc_type": "credit_note", "status": "final", "currency": "KWD",
              "total": 1.0, "amount_outstanding": 1.0, "line_items": []},
        actor_id=auth["user_id"], location_id=None, source="test",
        idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()
    r = await client.post(
        f"/docs/{doc_id}/cn-refund",
        headers=auth["headers"],
        json={"amount": 0.0004, "date": "2026-09-29", "bank_account": "1111"},
    )
    assert r.status_code == 422, r.text
    assert "must be positive" in r.json()["detail"]


async def _account_net(session, company_id, account: str) -> float:
    session.expire_all()
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == company_id,
                                 Projection.entity_type == "journal_entry")
    )).scalars().all()
    return round(sum(float(e.get("debit") or 0) - float(e.get("credit") or 0)
                     for p in rows if p.state.get("status") == "posted"
                     for e in p.state.get("entries", []) if e.get("account") == account), 3)


@pytest.mark.asyncio
async def test_true_ups_on_two_lines_round_once_for_the_invoice(client, session):
    auth = await _auth_company(session, "USD")
    lines, first_lots = [], []
    for n in range(2):
        sku = f"USD-TU{n}-{uuid.uuid4().hex[:6]}"
        lot_a = await _api_item(client, auth, sku, 1, 1.00)
        lot_b = await _api_item(client, auth, sku, 1, 1.00)
        await _api_item(client, auth, sku, 1, 1.004)
        other = await _invoice(client, auth, lot_b, sku, 1)
        lines.append({"entity_id": lot_a, "sku": sku, "name": sku,
                      "quantity": 2, "unit_price": 5.0, "sell_by": "piece"})
        first_lots.append((lot_b, other))
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": lines, "total": 20.0})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", headers=auth["headers"])).status_code == 200
    for lot_b, other in first_lots:
        r = await client.post(f"/docs/{other}/fulfill-lines", headers=auth["headers"],
                              json={"line_entity_ids": [lot_b]})
        assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc}/fulfill-lines", headers=auth["headers"],
                          json={"line_entity_ids": [ln["entity_id"] for ln in lines]})
    assert r.status_code == 200, r.text

    # Each line leaves at 2.004 against 2.00 recognized: 0.008 for the invoice rounds to 0.01.
    session.expire_all()
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == auth["company_id"],
                                 Projection.entity_id.like(f"je:auto:{doc}:cogs-adj:%"))
    )).scalars().all()
    amounts = [sum(float(e["debit"]) - float(e["credit"]) for e in p.state["entries"] if e["account"] == "5100")
               for p in rows if p.state.get("status") == "posted"]
    assert amounts == [0.01]


@pytest.mark.asyncio
async def test_kwd_landed_cost_capitalises_every_fils(client, session):
    auth = await _auth_company(session, "KWD")
    h = auth["headers"]
    loc = (await client.post("/companies/me/locations", headers=h, json={"name": "WH", "type": "warehouse"})).json()["id"]
    goods = []
    for n in range(3):
        sku = f"KWD-LC{n}-{uuid.uuid4().hex[:6]}"
        r = await client.post("/items", headers=h, json={"status": "available", "sku": sku, "name": sku,
                                                           "quantity": 0, "sell_by": "piece"})
        assert r.status_code == 200, r.text
        goods.append((r.json()["id"], sku))
    r = await client.post("/items", headers=h, json={"status": "available", "sku": "FRT", "name": "FRT", "quantity": 0,
                                                      "sell_by": "piece", "inventory_type": "freight",
                                                      "landed_cost_kind": "freight"})
    frt = r.json()["id"]
    line = lambda eid, sku, price: {"entity_id": eid, "sku": sku, "name": sku, "quantity": 1, "unit_price": price,
                                    "line_total": price, "sell_by": "piece", "receive_as": "stock"}
    r = await client.post("/docs", headers=h, json={"doc_type": "bill", "total": 31.234, "line_items": [
        *(line(eid, sku, 10.0) for eid, sku in goods), line(frt, "FRT", 1.234)]})
    assert r.status_code == 200, r.text
    bill = r.json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=h)).status_code == 200
    assert await _account_net(session, auth["company_id"], "1130-FRT") == 1.234

    r = await client.post(f"/docs/{bill}/receive", headers=h, json={"location_id": loc, "received_items": [
        {"item_id": eid, "sku": sku, "name": sku, "quantity_received": 1, "cost_price": 10.0, "receive_as": "stock"}
        for eid, sku in goods]})
    assert r.status_code == 200, r.text
    assert await _account_net(session, auth["company_id"], "1130-FRT") == 0.0
    assert await _account_net(session, auth["company_id"], "1130-P") == 31.234

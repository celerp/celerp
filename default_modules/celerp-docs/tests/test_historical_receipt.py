# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A bill's receipt state is held once, on the bill, and every receipt reads it.

Goods a bill received, or an invoice delivered, before the books came to Celerp are
recorded as Celerp's own receipt and fulfilment record them, naming the lots they
moved, without moving stock or posting anything. A live receipt takes at most what
each line still has to receive, and a retried receipt is the same receipt.
"""
from __future__ import annotations

import base64
import json
import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


async def _register(client) -> tuple[str, uuid.UUID]:
    r = await client.post("/auth/register", json={
        "company_name": "Receipt Co", "email": f"admin-{uuid.uuid4().hex[:8]}@receipt.test",
        "name": "A", "password": "validpass1"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return token, uuid.UUID(json.loads(base64.b64decode(token.split(".")[1] + "=="))["company_id"])


def _user(token: str) -> str:
    return json.loads(base64.b64decode(token.split(".")[1] + "=="))["sub"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _bill(client, token: str, qty: float = 10) -> tuple[str, str, str]:
    """A finalized bill for `qty` of one stocked item. Returns (bill, item, location)."""
    loc = (await client.post("/companies/me/locations", headers=_h(token),
                             json={"name": "WH", "type": "warehouse"})).json()["id"]
    item = (await client.post("/items", headers=_h(token), json={
        "status": "available", "sku": "WID", "name": "Widget", "quantity": 0, "sell_by": "piece"})).json()["id"]
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "bill", "line_items": [
        {"item_id": item, "sku": "WID", "name": "Widget", "quantity": qty, "unit_price": 4,
         "line_total": qty * 4}], "total": qty * 4})
    assert r.status_code == 200, r.text
    bill = r.json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(token))).status_code == 200
    return bill, item, loc


async def _receive(client, token: str, bill: str, loc: str, qty: float, key: str | None = None):
    body = {"location_id": loc, "received_items": [{"po_line_index": 0, "quantity_received": qty}]}
    if key:
        body["idempotency_key"] = key
    return await client.post(f"/docs/{bill}/receive", headers=_h(token), json=body)


async def _state(session, company_id, entity_id: str) -> dict:
    session.expire_all()
    row = await session.get(Projection, (company_id, entity_id))
    return dict(row.state) if row else {}


async def _stock_and_books(session, company_id) -> tuple:
    """Every item's quantity and cost, and every journal entry, as they stand."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type.in_(("item", "journal_entry"))))).scalars().all()
    items = sorted((r.entity_id, r.state.get("quantity"), r.state.get("cost_total"))
                   for r in rows if r.entity_type == "item")
    journals = sorted(r.entity_id for r in rows if r.entity_type == "journal_entry")
    return items, journals


async def _events(session, company_id, event_type: str, entity_id: str | None = None) -> int:
    q = select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == event_type)
    if entity_id is not None:
        q = q.where(LedgerEntry.entity_id == entity_id)
    return await session.scalar(q)



# ── Historical receipts and deliveries ────────────────────────────────────────

async def _invoice(client, token: str, item: str, qty: float = 5) -> str:
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "line_items": [
        {"item_id": item, "sku": "WID", "name": "Widget", "quantity": qty, "unit_price": 10,
         "line_total": qty * 10}], "total": qty * 10})
    assert r.status_code == 200, r.text
    invoice = r.json()["id"]
    r = await client.post(f"/docs/{invoice}/finalize", headers=_h(token))
    assert r.status_code == 200, r.text
    return invoice


def _receipt(item: str, qty: float = 6, cost: float = 24) -> list[dict]:
    return [{"line": 0, "item_id": item, "quantity": qty, "cost": cost}]


def _delivery(item: str, qty: float = 5, cost: float = 20) -> list[dict]:
    return [{"line": 0, "item_id": item, "quantity": qty, "cost": cost,
             "lot_id": f"item:{uuid.uuid4()}", "date": "2025-02-03"}]


@pytest.mark.asyncio
async def test_historical_receipt_records_what_it_added_to_the_lot_without_new_stock_or_entries(client, session):
    """RED before the change: a historical receipt marked the whole bill received and named
    no lot, so its goods could never be returned or the receipt undone."""
    from celerp_docs.routes import record_historical_receipt

    token, company_id = await _register(client)
    bill, item, loc = await _bill(client, token)
    before = await _stock_and_books(session, company_id)
    created = await _events(session, company_id, "item.created")

    entry = await record_historical_receipt(session, company_id, bill, lines=_receipt(item), received_on="2025-01-02",
                                            actor_id=_user(token), source="migration", idempotency_key=f"m:{bill}:received")
    await session.commit()

    assert entry.event_type == "doc.received"
    state = await _state(session, company_id, bill)
    assert [li["quantity_received"] for li in state["line_items"]] == [6]
    (received,) = state["received_items"]
    assert (received["item_id"], received["lot_quantity_added"], received["lot_cost_added"]) == (item, 6, 24)
    assert state.get("received_item_ids") == []
    assert await _stock_and_books(session, company_id) == before
    assert await _events(session, company_id, "item.created") == created

    # The live receipt takes only the four still to come.
    r = await _receive(client, token, bill, loc, 5)
    assert r.status_code == 422, r.text
    assert "at most 4 more can be received" in r.json()["detail"]
    assert (await _receive(client, token, bill, loc, 4)).status_code == 200


@pytest.mark.asyncio
async def test_historical_receipt_replay_is_idempotent(client, session):
    """RED before the change: the helper took no lines, so it could not say what was received."""
    from celerp_docs.routes import record_historical_receipt

    token, company_id = await _register(client)
    bill, item, _loc = await _bill(client, token)
    key = f"m:{bill}:received"

    first = await record_historical_receipt(session, company_id, bill, lines=_receipt(item), received_on="2025-01-02",
                                            actor_id=_user(token), source="migration", idempotency_key=key)
    await session.commit()
    again = await record_historical_receipt(session, company_id, bill, lines=_receipt(item), received_on="2025-01-02",
                                            actor_id=_user(token), source="migration", idempotency_key=key)
    await session.commit()

    assert again.id == first.id
    assert await _events(session, company_id, "doc.received", bill) == 1
    assert len((await _state(session, company_id, bill))["received_items"]) == 1


@pytest.mark.asyncio
async def test_historical_receipt_refuses_a_draft_bill_a_wrong_item_and_too_much(client, session):
    """RED before the change: the helper took no lines, so none of these could be checked."""
    from fastapi import HTTPException

    from celerp_docs.routes import record_historical_receipt

    token, company_id = await _register(client)
    bill, item, _loc = await _bill(client, token)
    draft = (await client.post("/docs", headers=_h(token), json={"doc_type": "bill", "line_items": [
        {"item_id": item, "sku": "WID", "name": "Widget", "quantity": 2, "unit_price": 4}]})).json()["id"]

    for doc, lines, status, detail in (
        (draft, _receipt(item, 2, 8), 409, "Only an issued bill"),
        (bill, _receipt("item:other"), 422, "does not hold item"),
        (bill, _receipt(item, 11, 44), 422, "at most 10 can be moved"),
    ):
        with pytest.raises(HTTPException) as exc:
            await record_historical_receipt(session, company_id, doc, lines=lines, received_on="2025-01-02",
                                            actor_id=_user(token), source="migration", idempotency_key=f"m:{uuid.uuid4()}")
        assert (exc.value.status_code, detail in exc.value.detail) == (status, True), exc.value.detail
    assert await _events(session, company_id, "doc.received") == 0


@pytest.mark.asyncio
async def test_historical_delivery_fulfils_the_line_from_a_sold_lot_without_new_entries(client, session):
    """RED before the change: the docs module had no way to record goods an invoice
    delivered before its books came to Celerp."""
    from celerp_docs.routes import record_historical_delivery

    token, company_id = await _register(client)
    _bill_id, item, _loc = await _bill(client, token)
    invoice = await _invoice(client, token, item)
    before_journals = (await _stock_and_books(session, company_id))[1]
    parent = await _state(session, company_id, item)
    lines = _delivery(item)

    entry = await record_historical_delivery(session, company_id, invoice, lines=lines, actor_id=_user(token),
                                             source="migration", idempotency_key=f"m:{invoice}:delivered")
    await session.commit()

    assert entry.event_type == "doc.fulfilled"
    state = await _state(session, company_id, invoice)
    assert state["fulfillment_status"] == "fulfilled"
    assert state["line_items"][0]["entity_id"] == state["line_items"][0]["item_id"] == lines[0]["lot_id"]
    lot = await _state(session, company_id, lines[0]["lot_id"])
    assert (lot["status"], lot["quantity"], lot["cost_total"], lot["sku"]) == ("sold", 5, 20, "WID")
    assert await _state(session, company_id, item) == parent
    assert (await _stock_and_books(session, company_id))[1] == before_journals

    # The same delivery again is the same delivery.
    again = await record_historical_delivery(session, company_id, invoice, lines=lines, actor_id=_user(token),
                                             source="migration", idempotency_key=f"m:{invoice}:delivered")
    assert again.id == entry.id
    assert await _events(session, company_id, "item.fulfilled") == 1


@pytest.mark.asyncio
async def test_historical_delivery_of_part_of_a_line_leaves_the_invoice_partly_fulfilled(client, session):
    """RED before the change: the helper did not exist."""
    from fastapi import HTTPException

    from celerp_docs.routes import record_historical_delivery

    token, company_id = await _register(client)
    _bill_id, item, _loc = await _bill(client, token)
    invoice = await _invoice(client, token, item)

    with pytest.raises(HTTPException) as exc:
        await record_historical_delivery(session, company_id, invoice, lines=_delivery(item, 4, 16) + _delivery(item, 2, 8),
                                         actor_id=_user(token), source="migration", idempotency_key=f"m:{uuid.uuid4()}")
    assert exc.value.status_code == 422 and "at most 1 can be moved" in exc.value.detail
    await session.rollback()

    entry = await record_historical_delivery(session, company_id, invoice, lines=_delivery(item, 2, 8), actor_id=_user(token),
                                             source="migration", idempotency_key=f"m:{invoice}:delivered")
    await session.commit()
    assert entry.event_type == "doc.partially_fulfilled"
    assert (await _state(session, company_id, invoice))["fulfillment_status"] == "partial"


@pytest.mark.asyncio
async def test_historical_deliveries_of_one_line_are_one_sold_lot_each(client, session):
    """RED before the change: a line delivered twice was refused, so the deliveries had to
    be merged into one lot and lost which goods left when, and at what cost.

    The line names the first delivery's lot; the second is its own lot on the same line,
    and together they fulfil it."""
    from celerp.services import auto_je
    from celerp_docs.routes import record_historical_delivery

    token, company_id = await _register(client)
    _bill_id, item, _loc = await _bill(client, token)
    invoice = await _invoice(client, token, item)
    first, second = _delivery(item, 2, 8), [{**_delivery(item, 3, 12.5)[0], "date": "2025-02-05"}]

    entry = await record_historical_delivery(session, company_id, invoice, lines=first + second, actor_id=_user(token),
                                             source="migration", idempotency_key=f"m:{invoice}:delivered")
    await session.commit()

    assert entry.event_type == "doc.fulfilled"
    state = await _state(session, company_id, invoice)
    assert state["line_items"][0]["entity_id"] == first[0]["lot_id"]
    for moved in first + second:
        lot = await _state(session, company_id, moved["lot_id"])
        assert (lot["status"], lot["quantity"], lot["cost_total"]) == ("sold", moved["quantity"], moved["cost"])
        assert await auto_je.doc_line_of_lot(session, company_id, invoice, state, moved["lot_id"], lot) == 0
    assert await _events(session, company_id, "item.fulfilled") == 2


@pytest.mark.asyncio
async def test_a_sold_lot_is_stock_of_the_product_its_line_names_and_never_of_a_guess(client, session):
    """RED before the change: the sold lot named no product, so Demand Planning took it for a
    product of its own. A line whose item names no product (units split off under their own
    SKU) leaves its lot unlinked and the company is told once."""
    from celerp.models.notification import Notification
    from celerp_docs.routes import record_historical_delivery

    token, company_id = await _register(client)
    _bill_id, item, _loc = await _bill(client, token)
    stock = (await client.post("/items", headers=_h(token), json={
        "status": "available", "sku": "BULK", "name": "Bulk", "quantity": 10, "sell_by": "piece"})).json()["id"]
    r = await client.post(f"/items/{stock}/split", headers=_h(token), json={"children": [{"sku": "PART-1", "quantity": 4}]})
    assert r.status_code == 200, r.text
    part = r.json()["children"][0]["id"]
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "line_items": [
        {"item_id": item, "sku": "WID", "name": "Widget", "quantity": 5, "unit_price": 10, "line_total": 50},
        {"item_id": part, "sku": "PART-1", "name": "Part", "quantity": 3, "unit_price": 10, "line_total": 30}],
        "total": 80})
    invoice = r.json()["id"]
    assert (await client.post(f"/docs/{invoice}/finalize", headers=_h(token))).status_code == 200
    lines = _delivery(item, 2, 8) + [{**_delivery(part, 1, 2)[0], "line": 1}]

    for _ in range(2):  # the same delivery again changes nothing
        await record_historical_delivery(session, company_id, invoice, lines=lines, actor_id=_user(token),
                                         source="migration", idempotency_key=f"m:{invoice}:delivered")
        await session.commit()

    assert (await _state(session, company_id, lines[0]["lot_id"]))["catalog_item_id"] == item
    unlinked = await _state(session, company_id, lines[1]["lot_id"])
    assert not unlinked.get("catalog_item_id") and not unlinked.get("parent_item_id"), unlinked
    assert await _events(session, company_id, "item.updated", lines[0]["lot_id"]) == 1
    notices = (await session.execute(select(Notification).where(
        Notification.company_id == company_id, Notification.title == "Delivered goods with no product"))).scalars().all()
    assert len(notices) == 1 and "PART-1" in notices[0].body and "WID" not in notices[0].body, notices


# ── Live receipts read the bill's receipt state ───────────────────────────────

@pytest.mark.asyncio
async def test_partial_receipts_up_to_the_line_then_nothing_more(client, session):
    """Regression guard (already correct before the change): partial receipts are taken up
    to each line's remaining quantity; more than remains, or anything once the bill is
    received in full, is refused before any stock is written."""
    token, company_id = await _register(client)
    bill, _item, loc = await _bill(client, token)

    assert (await _receive(client, token, bill, loc, 4)).status_code == 200
    assert (await _receive(client, token, bill, loc, 6)).status_code == 200
    state = await _state(session, company_id, bill)
    assert state["status"] == "received"
    assert len(state["received_item_ids"]) == 2
    full = await _stock_and_books(session, company_id)

    r = await _receive(client, token, bill, loc, 1)
    assert r.status_code == 422, r.text
    assert "at most 0 more can be received" in r.json()["detail"]
    assert await _stock_and_books(session, company_id) == full
    assert await _events(session, company_id, "doc.received", bill) == 2


@pytest.mark.asyncio
async def test_over_receipt_is_refused_before_any_stock_is_written(client, session):
    """Regression guard (already correct before the change)."""
    token, company_id = await _register(client)
    bill, _item, loc = await _bill(client, token)
    assert (await _receive(client, token, bill, loc, 4)).status_code == 200
    before = await _stock_and_books(session, company_id)
    parcels = await _events(session, company_id, "item.created")

    r = await _receive(client, token, bill, loc, 7)
    assert r.status_code == 422, r.text
    assert "at most 6 more can be received" in r.json()["detail"]
    assert await _stock_and_books(session, company_id) == before
    assert await _events(session, company_id, "item.created") == parcels


@pytest.mark.asyncio
async def test_a_retried_receipt_creates_its_parcels_once(client, session):
    """Regression guard (already correct before the change): the same receipt sent twice
    under one idempotency key is one receipt."""
    token, company_id = await _register(client)
    bill, _item, loc = await _bill(client, token)
    key = f"receive-{uuid.uuid4().hex}"

    first = await _receive(client, token, bill, loc, 4, key=key)
    again = await _receive(client, token, bill, loc, 4, key=key)
    assert first.status_code == again.status_code == 200, again.text
    assert first.json()["event_id"] == again.json()["event_id"]
    state = await _state(session, company_id, bill)
    assert len(state["received_item_ids"]) == 1
    assert [li["quantity_received"] for li in state["line_items"]] == [4]
    assert await _events(session, company_id, "doc.received", bill) == 1

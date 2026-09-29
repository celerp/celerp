# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A bill's receipt state is held once, on the bill, and every receipt reads it.

Goods already brought into stock before the books came to Celerp are recorded as
received without moving stock or posting anything. A live receipt takes at most
what each line still has to receive, and a retried receipt is the same receipt.
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


# ── Historical receipt state ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_historical_bill_gets_canonical_received_state_without_new_stock_or_entries(client, session):
    """RED before the change: the docs module has no way to record a receipt that moves no stock."""
    from celerp_docs.routes import record_historical_receipt

    token, company_id = await _register(client)
    bill, _item, loc = await _bill(client, token)
    before = await _stock_and_books(session, company_id)
    created = await _events(session, company_id, "item.created")

    entry = await record_historical_receipt(session, company_id, bill, actor_id=None, source="migration",
                                            idempotency_key=f"migration:test:{bill}:received")
    await session.commit()

    assert entry is not None and entry.event_type == "doc.received"
    state = await _state(session, company_id, bill)
    assert state["status"] == "received"
    assert [li["quantity_received"] for li in state["line_items"]] == [10]
    assert state.get("received_item_ids") == []
    assert await _stock_and_books(session, company_id) == before
    assert await _events(session, company_id, "item.created") == created

    # The bill now reads as fully received, so the live receipt refuses more goods.
    r = await _receive(client, token, bill, loc, 1)
    assert r.status_code == 422, r.text
    assert await _stock_and_books(session, company_id) == before


@pytest.mark.asyncio
async def test_historical_receipt_replay_is_idempotent(client, session):
    """RED before the change: the helper does not exist."""
    from celerp_docs.routes import record_historical_receipt

    token, company_id = await _register(client)
    bill, _item, _loc = await _bill(client, token)
    key = f"migration:test:{bill}:received"

    first = await record_historical_receipt(session, company_id, bill, actor_id=None, source="migration",
                                            idempotency_key=key)
    await session.commit()
    again = await record_historical_receipt(session, company_id, bill, actor_id=None, source="migration",
                                            idempotency_key=key)
    await session.commit()

    assert again.id == first.id
    assert await _events(session, company_id, "doc.received", bill) == 1
    state = await _state(session, company_id, bill)
    assert [li["quantity_received"] for li in state["line_items"]] == [10]
    assert len(state["received_items"]) == 1


@pytest.mark.asyncio
async def test_historical_receipt_records_only_what_is_still_to_receive(client, session):
    """RED before the change: the helper does not exist. A bill part received live
    is recorded as received for the rest; a bill with nothing left records nothing."""
    from celerp_docs.routes import record_historical_receipt

    token, company_id = await _register(client)
    bill, _item, loc = await _bill(client, token)
    assert (await _receive(client, token, bill, loc, 4)).status_code == 200

    await record_historical_receipt(session, company_id, bill, actor_id=None, source="migration",
                                    idempotency_key=f"migration:test:{bill}:received")
    await session.commit()
    state = await _state(session, company_id, bill)
    assert [li["quantity_received"] for li in state["line_items"]] == [10]
    assert state["received_items"][-1]["quantity_received"] == 6

    nothing = await record_historical_receipt(session, company_id, bill, actor_id=None, source="migration",
                                              idempotency_key=f"migration:test:{bill}:received-again")
    assert nothing is None
    assert await _events(session, company_id, "doc.received", bill) == 2


@pytest.mark.asyncio
async def test_historical_receipt_refuses_a_draft_bill(client, session):
    """RED before the change: the helper does not exist. A draft bill is not an issued
    purchase, so nothing about it was received."""
    from fastapi import HTTPException

    from celerp_docs.routes import record_historical_receipt

    token, company_id = await _register(client)
    item = (await client.post("/items", headers=_h(token), json={
        "status": "available", "sku": "WID", "name": "Widget", "quantity": 0, "sell_by": "piece"})).json()["id"]
    draft = (await client.post("/docs", headers=_h(token), json={"doc_type": "bill", "line_items": [
        {"item_id": item, "sku": "WID", "name": "Widget", "quantity": 2, "unit_price": 4}]})).json()["id"]

    with pytest.raises(HTTPException) as exc:
        await record_historical_receipt(session, company_id, draft, actor_id=None, source="migration",
                                        idempotency_key=f"migration:test:{draft}:received")
    assert exc.value.status_code == 409
    assert await _events(session, company_id, "doc.received", draft) == 0


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

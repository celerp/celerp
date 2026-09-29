# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Receiving goods books what arrived, once.

A purchase order receipt books the goods it brings in, priced the same way the
lot's cost is; the bill then books whatever its receipts have not. Receiving
before or after finalizing ends in the same books and the same lot costs.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from test_cost_restatement import _item, _state, auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_money_stock_and_contact_invariants import (
    _account_net,
    _cleanup,
    _seed_chart,
    _seed_company,
    _seed_item,
    _seed_user,
)


async def _doc(client, auth, doc_type: str, lines: list[dict], **extra) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": doc_type, "contact_id": "supplier:1", "line_items": lines, **extra,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _receive(client, auth, doc_id: str, *items: dict):
    return await client.post(f"/docs/{doc_id}/receive", headers=auth["headers"], json={
        "location_id": "loc:1",
        "received_items": [{"receive_as": "stock", **it} for it in items],
    })


async def _finalize(client, auth, doc_id: str) -> None:
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text


async def _books(session, auth, *accounts: str) -> dict[str, float]:
    return {a: await _account_net(session, auth["company_id"], a) for a in accounts}


async def _parcels(session, auth, doc_id: str) -> list[dict]:
    doc = await _state(session, auth, doc_id)
    return [await _state(session, auth, i) for i in doc.get("received_item_ids") or []]


@pytest.mark.asyncio
async def test_partial_po_receipt_books_only_the_goods_received(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": item_id, "name": "Lot", "quantity": 10, "unit_price": 14.0}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "item_id": item_id, "quantity_received": 4})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-P", "2110") == {"1130-P": 56.0, "2110": -56.0}
    assert (await _state(session, auth, item_id))["cost_base"] == 156.0

    r = await _receive(client, auth, po, {"po_line_index": 0, "item_id": item_id, "quantity_received": 6})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, "1130-P", "2110") == {"1130-P": 140.0, "2110": -140.0}
    state = await _state(session, auth, item_id)
    assert (state["quantity"], state["cost_base"]) == (20, 240.0)


@pytest.mark.parametrize("order", ["receive_first", "finalize_first", "split"])
@pytest.mark.asyncio
async def test_receiving_before_or_after_finalizing_books_the_same(client, session, auth, order):
    po = await _doc(client, auth, "purchase_order", [
        {"sku": f"NEW-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 14.0},
    ])
    sku = (await _state(session, auth, po))["line_items"][0]["sku"]

    async def receive(qty: float) -> None:
        r = await _receive(client, auth, po, {"po_line_index": 0, "sku": sku, "name": "Beads",
                                              "quantity_received": qty})
        assert r.status_code == 200, r.text

    if order == "receive_first":
        await receive(10)
        await _finalize(client, auth, po)
    elif order == "finalize_first":
        await _finalize(client, auth, po)
        await receive(10)
    else:
        await receive(4)
        await _finalize(client, auth, po)
        await receive(6)

    assert await _books(session, auth, "1130-P", "2110") == {"1130-P": 140.0, "2110": -140.0}
    parcels = await _parcels(session, auth, po)
    assert sum(p["quantity"] for p in parcels) == 10
    assert sum(p["cost_total"] for p in parcels) == 140.0


@pytest.mark.asyncio
async def test_new_parcel_takes_its_cost_from_the_order_line(client, session, auth):
    sku = f"TPL-{uuid.uuid4().hex[:6]}"
    await _item(client, auth, 99.0, qty=1, sku=sku)
    po = await _doc(client, auth, "purchase_order",
                    [{"sku": sku, "name": "Beads", "quantity": 4, "unit_price": 12.5}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 4})
    assert r.status_code == 200, r.text
    [parcel] = await _parcels(session, auth, po)
    assert (parcel["quantity"], parcel["cost_total"]) == (4, 50.0)
    assert await _books(session, auth, "1130-P", "2110") == {"1130-P": 50.0, "2110": -50.0}


@pytest.mark.parametrize("doc_type", ["purchase_order", "bill"])
@pytest.mark.asyncio
async def test_stock_nothing_prices_is_refused(client, session, auth, doc_type):
    doc = await _doc(client, auth, doc_type, [{"sku": "LISTED", "name": "Listed", "quantity": 1, "unit_price": 5.0}])
    if doc_type == "bill":
        await _finalize(client, auth, doc)
    before = await _books(session, auth, "1130-P", "2110")
    r = await _receive(client, auth, doc, {"sku": "UNLISTED", "name": "Unlisted", "quantity_received": 2})
    assert r.status_code == 422, r.text
    assert "no line on this" in r.json()["detail"]
    assert (await _state(session, auth, doc)).get("received_item_ids") in (None, [])
    assert await _books(session, auth, "1130-P", "2110") == before


@pytest.mark.asyncio
async def test_undoing_a_bill_receipt_keeps_the_bill_booked(client, session, auth):
    r = await client.post("/items", headers=auth["headers"], json={
        "status": "available", "sku": "FRT", "name": "Freight", "quantity": 0, "sell_by": "piece",
        "inventory_type": "freight", "landed_cost_kind": "freight"})
    assert r.status_code == 200, r.text
    bill = await _doc(client, auth, "bill", [
        {"sku": "GOODS", "name": "Goods", "quantity": 2, "unit_price": 15.0},
        {"entity_id": r.json()["id"], "sku": "FRT", "name": "Freight", "quantity": 1, "unit_price": 10.0},
    ])
    await _finalize(client, auth, bill)
    booked = {"1130-P": 30.0, "1130-FRT": 10.0, "2110": -40.0}
    assert await _books(session, auth, *booked) == booked

    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": "GOODS", "name": "Goods", "quantity_received": 2})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, *booked) == {"1130-P": 40.0, "1130-FRT": 0.0, "2110": -40.0}

    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _books(session, auth, *booked) == booked

    r = await _receive(client, auth, bill, {"po_line_index": 0, "sku": "GOODS", "name": "Goods", "quantity_received": 2})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, *booked) == {"1130-P": 40.0, "1130-FRT": 0.0, "2110": -40.0}


@pytest.mark.asyncio
async def test_undo_refuses_a_receipt_that_added_to_existing_stock(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order", [
        {"item_id": item_id, "name": "Lot", "quantity": 5, "unit_price": 14.0},
        {"sku": "FRESH", "name": "Fresh", "quantity": 2, "unit_price": 3.0},
    ])
    r = await _receive(client, auth, po,
                       {"po_line_index": 0, "item_id": item_id, "quantity_received": 5},
                       {"po_line_index": 1, "sku": "FRESH", "name": "Fresh", "quantity_received": 2})
    assert r.status_code == 200, r.text
    await _finalize(client, auth, po)

    r = await client.delete(f"/docs/{po}/receive", headers=auth["headers"])
    assert r.status_code == 409, r.text
    [parcel] = await _parcels(session, auth, po)
    assert parcel["status"] == "available"
    assert (await _state(session, auth, item_id))["quantity"] == 15


@pytest.mark.asyncio
async def test_doctor_finds_nothing_missing_on_a_received_order(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _doc(client, auth, "purchase_order",
                    [{"item_id": item_id, "name": "Lot", "quantity": 10, "unit_price": 14.0}])
    r = await _receive(client, auth, po, {"po_line_index": 0, "item_id": item_id, "quantity_received": 4})
    assert r.status_code == 200, r.text

    r = await client.post("/admin/doctor?checks=missing_jes&fix=true", headers=auth["headers"])
    assert r.status_code == 200, r.text
    missing = next(c for c in r.json()["results"] if c["check"] == "missing_jes")
    assert [d for d in missing["details"] if d.get("doc_id") == po] == []
    assert await _books(session, auth, "1130-P", "2110") == {"1130-P": 56.0, "2110": -56.0}


@pytest.mark.asyncio
async def test_concurrent_receipts_both_count(_db_engine):
    from celerp_docs.routes import ReceiveBody, receive_po

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id = await _seed_company(factory)
    user_id = await _seed_user(factory)
    await _seed_chart(factory, company_id)
    item_id, po = "item:RCV-RACE", "doc:PO-RCV-RACE"
    await _seed_item(factory, company_id, item_id, qty=10, cost_total=100)
    async with factory() as s:
        await emit_event(
            s, company_id=company_id, entity_id=po, entity_type="doc", event_type="doc.created",
            data={"doc_type": "purchase_order", "status": "final", "ref_id": "PO-RCV-RACE", "currency": "USD",
                  "line_items": [{"item_id": item_id, "sku": "RCV-RACE", "name": "RCV-RACE",
                                  "quantity": 10, "unit_price": 14.0}],
                  "subtotal": 140.0, "total": 140.0},
            actor_id=user_id, location_id=None, source="test",
            idempotency_key=str(uuid.uuid4()), metadata_={},
        )
        await s.commit()

    def body() -> ReceiveBody:
        return ReceiveBody(location_id="loc:1", received_items=[
            {"po_line_index": 0, "item_id": item_id, "quantity_received": 5}])

    first, second = factory(), factory()
    release = asyncio.Event()
    commit = first.commit

    async def held_commit() -> None:
        await release.wait()
        await commit()

    first.commit = held_commit
    user = types.SimpleNamespace(id=user_id)
    try:
        a = asyncio.create_task(receive_po(po, body(), company_id=company_id, _=None, user=user, session=first))
        await asyncio.sleep(0.3)
        b = asyncio.create_task(receive_po(po, body(), company_id=company_id, _=None, user=user, session=second))
        await asyncio.sleep(0.3)
        release.set()
        await asyncio.wait_for(asyncio.gather(a, b), timeout=10)
        async with factory() as s:
            row = await s.get(Projection, {"company_id": company_id, "entity_id": item_id})
            assert (row.state["quantity"], row.state["cost_base"]) == (20, 240.0)
            assert await _account_net(s, company_id, "1130-P") == 140.0
    finally:
        await first.close()
        await second.close()
        await _cleanup(factory, company_id, user_id)

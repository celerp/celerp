# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A lot's unit cost stays put when its quantity changes.

Units that leave (an audit shortfall, a manual count, a supplier return, a
consumption) take their share of the goods cost with them; units that come
back bring it; a receipt adds what the received goods cost. A zero-quantity
lot keeps its unit cost, and its landed cost, until stock arrives.
"""
from __future__ import annotations

import uuid

import pytest

from celerp_inventory.projections import apply_item_event
from test_cost_restatement import _cogs, _cogs_adjustments, _item, _merge, _sell, _set_cost, _state


def _apply(state: dict, *events: tuple[str, dict]) -> dict:
    for event_type, data in events:
        state = apply_item_event(state, event_type, data)
    return state


def _landed(bill: str, unit: float) -> tuple[str, dict]:
    return "item.landed_cost.applied", {"source_bill_id": bill, "kind": "freight", "unit_amount": unit}


async def _emit(session, auth, item_id: str, event_type: str, data: dict) -> None:
    from celerp.events.engine import emit_event
    await emit_event(
        session, company_id=auth["company_id"], entity_id=item_id, entity_type="item",
        event_type=event_type, data=data, actor_id=auth["user_id"], location_id=None,
        source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
    )
    await session.commit()


async def _adjust(client, auth, item_id: str, new_qty: float) -> None:
    r = await client.post(f"/items/{item_id}/adjust", headers=auth["headers"], json={"new_qty": new_qty})
    assert r.status_code == 200, r.text


# -- The projection primitive -------------------------------------------------

def test_quantity_change_scales_the_goods_basis():
    state = _apply({}, ("item.created", {"sku": "A", "quantity": 10, "cost_total": 100.0}),
                   ("item.quantity.adjusted", {"new_qty": 4}))
    assert (state["quantity"], state["cost_base"], state["cost_total"]) == (4, 40.0, 40.0)
    state = _apply(state, ("item.quantity.adjusted", {"new_qty": 10}))
    assert (state["cost_base"], state["cost_total"]) == (100.0, 100.0)


def test_zero_quantity_keeps_the_unit_cost_for_returning_stock():
    state = _apply({}, ("item.created", {"sku": "A", "quantity": 10, "cost_total": 100.0}),
                   ("item.quantity.adjusted", {"new_qty": 0}))
    assert state["cost_price"] == 10.0
    assert "cost_base" not in state and "cost_total" not in state
    state = _apply(state, ("item.quantity.adjusted", {"new_qty": 10}))
    assert (state["cost_base"], state["cost_total"]) == (100.0, 100.0)
    assert "cost_price" not in state


def test_explicit_cost_base_wins():
    state = _apply({}, ("item.created", {"sku": "A", "quantity": 10, "cost_total": 100.0}),
                   ("item.quantity.adjusted", {"new_qty": 15, "cost_base": 170.0}))
    assert (state["cost_base"], state["cost_total"]) == (170.0, 170.0)


def test_landed_cost_at_zero_quantity_keeps_the_unit_cost():
    state = _apply({}, ("item.created", {"sku": "A", "quantity": 0}),
                   ("item.pricing.set", {"price_type": "cost_price", "new_price": 12.5}),
                   _landed("bill:1", 2.0))
    assert state["cost_price"] == 12.5
    assert state.get("cost_total") is None
    state = _apply(state, ("item.quantity.adjusted", {"new_qty": 4}))
    assert (state["cost_base"], state["cost_landed"], state["cost_total"]) == (50.0, 8.0, 58.0)


def test_clearing_goods_cost_keeps_landed_cost():
    state = _apply({}, ("item.created", {"sku": "A", "quantity": 4, "cost_total": 40.0}),
                   _landed("bill:1", 2.0),
                   ("item.pricing.set", {"price_type": "cost_total", "new_price": None}))
    assert (state["cost_base"], state["cost_landed"], state["cost_total"]) == (0.0, 8.0, 8.0)


def test_consumption_relieves_cost_with_the_units():
    state = _apply({}, ("item.created", {"sku": "A", "quantity": 10, "cost_total": 100.0}),
                   _landed("bill:1", 1.0),
                   ("item.consumed", {"quantity_consumed": 4}))
    assert (state["cost_base"], state["cost_landed"], state["cost_total"]) == (60.0, 6.0, 66.0)
    state = _apply(state, ("item.consumed", {"quantity_consumed": 6}))
    assert state["cost_price"] == 10.0 and state.get("cost_total") is None


# -- Audit: shortfall and undo ------------------------------------------------

async def _audit_count(client, auth, item_id: str, loc: str, counted: float) -> tuple[str, dict]:
    h = auth["headers"]
    r = await client.post("/lists/audit", headers=h, json={"location_id": loc})
    assert r.status_code == 200, r.text
    audit = r.json()["id"]
    assert (await client.post(f"/lists/{audit}/finalize", headers=h)).status_code == 200
    r = await client.patch(f"/lists/{audit}/line/{item_id}", headers=h, json={"counted_qty": counted})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{audit}/adjust", headers=h)
    assert r.status_code == 200, r.text
    return audit, r.json()


async def _located_item(client, session, auth, qty: float, cost_total: float) -> tuple[str, str]:
    from celerp_accounting.routes import seed_chart_of_accounts
    await seed_chart_of_accounts(session, auth["company_id"])  # the shrinkage JE posts to it
    await session.commit()
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": f"W-{uuid.uuid4().hex[:4]}", "type": "warehouse"})
    assert r.status_code == 200, r.text
    loc = r.json()["id"]
    r = await client.post("/items", headers=auth["headers"], json={
        "sku": f"AU-{uuid.uuid4().hex[:6]}", "name": "Counted", "quantity": qty, "sell_by": "piece",
        "status": "available", "location_id": loc, "cost_total": cost_total,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"], loc


@pytest.mark.asyncio
@pytest.mark.parametrize("counted, left, shrinkage", [(5, 50.0, 50.0), (0, None, 100.0)])
async def test_audit_shortfall_moves_its_cost_to_shrinkage_and_undo_restores_it(
    client, session, auth, counted, left, shrinkage,
):
    item_id, loc = await _located_item(client, session, auth, 10, 100.0)
    audit, result = await _audit_count(client, auth, item_id, loc, counted)
    assert result["shrinkage_value"] == shrinkage
    state = await _state(session, auth, item_id)
    assert state["quantity"] == counted
    assert state.get("cost_total") == left
    assert shrinkage + (left or 0) == 100.0  # books and stock still reconcile

    r = await client.post(f"/lists/{audit}/undo-adjust", headers=auth["headers"])
    assert r.status_code == 200, r.text
    state = await _state(session, auth, item_id)
    assert (state["quantity"], state["cost_base"], state["cost_total"]) == (10, 100.0, 100.0)


@pytest.mark.asyncio
async def test_manual_count_scales_the_basis(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    await _adjust(client, auth, item_id, 4)
    state = await _state(session, auth, item_id)
    assert (state["cost_base"], state["cost_total"]) == (40.0, 40.0)


@pytest.mark.asyncio
async def test_correction_after_a_shortfall_is_refused(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    await _adjust(client, auth, item_id, 4)
    r = await _set_cost(client, auth, item_id, 60.0)
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_correction_after_an_undone_audit_carries_the_new_cost(client, session, auth):
    item_id, loc = await _located_item(client, session, auth, 10, 100.0)
    audit, _ = await _audit_count(client, auth, item_id, loc, 4)
    r = await client.post(f"/lists/{audit}/undo-adjust", headers=auth["headers"])
    assert r.status_code == 200, r.text

    r = await _set_cost(client, auth, item_id, 120.0)
    assert r.status_code == 200, r.text
    state = await _state(session, auth, item_id)
    assert (state["quantity"], state["cost_base"], state["cost_total"]) == (10, 120.0, 120.0)


@pytest.mark.asyncio
async def test_correction_after_an_audit_that_still_stands_is_refused(client, session, auth):
    item_id, loc = await _located_item(client, session, auth, 10, 100.0)
    audit, _ = await _audit_count(client, auth, item_id, loc, 4)
    r = await client.post(f"/lists/{audit}/undo-adjust", headers=auth["headers"])
    assert r.status_code == 200, r.text
    await _audit_count(client, auth, item_id, loc, 6)

    r = await _set_cost(client, auth, item_id, 120.0)
    assert r.status_code == 409, r.text
    assert (await _state(session, auth, item_id))["cost_total"] == 60.0


# -- Zero-quantity unit cost with landed cost, promoted by new stock ----------

async def _promoted_lot(client, session, auth) -> str:
    item_id = await _item(client, auth, None, qty=0)
    r = await client.patch(f"/items/{item_id}", headers=auth["headers"],
                           json={"fields_changed": {"cost_price": {"old": None, "new": 12.5}}})
    assert r.status_code == 200, r.text
    await _emit(session, auth, item_id, *_landed("bill:x", 2.0))
    state = await _state(session, auth, item_id)
    assert state["cost_price"] == 12.5 and state.get("cost_total") is None
    await _adjust(client, auth, item_id, 4)
    state = await _state(session, auth, item_id)
    assert (state["cost_base"], state["cost_landed"], state["cost_total"]) == (50.0, 8.0, 58.0)
    assert "cost_price" not in state
    return item_id


@pytest.mark.asyncio
async def test_promoted_lot_sells_and_restates(client, session, auth):
    item_id = await _promoted_lot(client, session, auth)
    doc = await _sell(client, session, auth, item_id)
    r = await _set_cost(client, auth, item_id, 60.0)
    assert r.status_code == 200, r.text
    assert [_cogs(s) for s in (await _cogs_adjustments(session, auth, doc)).values()] == [10.0]


@pytest.mark.asyncio
async def test_promoted_lot_merges_and_restates(client, session, auth):
    item_id = await _promoted_lot(client, session, auth)
    other = await _item(client, auth, 30.0, qty=2)
    merged = await _merge(client, auth, [item_id, other])
    assert (await _state(session, auth, merged))["cost_total"] == 88.0
    r = await _set_cost(client, auth, item_id, 60.0)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, merged))["cost_total"] == 98.0


# -- Receipts add what the received goods cost ---------------------------------

async def _po(client, auth, lines: list[dict]) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "purchase_order", "contact_id": "supplier:1", "line_items": lines,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _receive(client, auth, po: str, item_id: str, qty: float, **extra):
    return await client.post(f"/docs/{po}/receive", headers=auth["headers"], json={
        "location_id": "",
        "received_items": [{"po_line_index": 0, "item_id": item_id, "quantity_received": qty,
                            "receive_as": "stock", **extra}],
    })


@pytest.mark.asyncio
async def test_po_receipt_at_a_new_price_adds_the_purchase_cost(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _po(client, auth, [{"item_id": item_id, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    r = await _receive(client, auth, po, item_id, 5)
    assert r.status_code == 200, r.text
    state = await _state(session, auth, item_id)
    assert (state["quantity"], state["cost_base"], state["cost_total"]) == (15, 170.0, 170.0)


@pytest.mark.asyncio
async def test_po_receipt_of_free_goods_lowers_the_unit_cost(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _po(client, auth, [{"item_id": item_id, "name": "Lot", "quantity": 10, "unit_price": 0}])
    assert (await _receive(client, auth, po, item_id, 10)).status_code == 200
    state = await _state(session, auth, item_id)
    assert (state["quantity"], state["cost_base"], state["cost_total"]) == (20, 100.0, 100.0)


@pytest.mark.asyncio
async def test_po_receipt_no_line_prices_is_refused(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _po(client, auth, [{"sku": "OTHER", "name": "Other", "quantity": 5, "unit_price": 3.0}])
    r = await _receive(client, auth, po, item_id, 5, po_line_index=-1)
    assert r.status_code == 422, r.text
    assert "is not on this purchase order" in r.json()["detail"]
    state = await _state(session, auth, item_id)
    assert (state["quantity"], state["cost_total"]) == (10, 100.0)


@pytest.mark.asyncio
async def test_receipt_is_traceable_for_a_later_correction(client, session, auth):
    item_id = await _item(client, auth, 100.0, qty=10)
    po = await _po(client, auth, [{"item_id": item_id, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    assert (await _receive(client, auth, po, item_id, 5)).status_code == 200
    r = await _set_cost(client, auth, item_id, 180.0)
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, item_id))["cost_total"] == 180.0

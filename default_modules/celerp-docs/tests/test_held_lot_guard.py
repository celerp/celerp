# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A lot out on a memo is in the customer's hands: its quantity, measures and location are
what the memo shipped, and only the memo may change them. Every transport that writes an item
event meets one check; names, notes and prices stay editable, and the memo's own actions
(return, draw, Back to memo, Set as available) still move the lot."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from memo_draw_support import auth, back_to_memo, drawn, item, lot, memo_out, ok, owner, state
from test_helpers import company_auth

HELD = "inventory.err_held_by_memo"


def _refused(r) -> None:
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == HELD
    assert "MEMO-" in str(detail)


async def _held(client, h, units: float = 1) -> tuple[str, str, str]:
    memo, _, lots = await memo_out(client, h, [(units, 300.0)])
    return memo, lots[0][0], (await item(client, h, lots[0][0]))["sku"]


async def _location(client, h) -> str:
    return (await ok(await client.post("/companies/me/locations", headers=h, json={
        "name": f"Back-{uuid.uuid4().hex[:4]}", "type": "warehouse"})))["id"]


async def _patch(client, h, lot_id: str, field: str, value):
    return await client.patch(f"/items/{lot_id}", headers=h,
                              json={"fields_changed": {field: {"old": None, "new": value}}})


async def test_held_lot_transfer_refused(client, session):
    h = await auth(session)
    memo, held, _ = await _held(client, h)
    before = await item(client, h, held)
    _refused(await client.post(f"/items/{held}/transfer", headers=h, json={"to_location_id": await _location(client, h)}))
    after = await item(client, h, held)
    assert after.get("location_id") == before.get("location_id")
    assert await owner(client, h, held) == ("memo_out", memo)


async def test_held_lot_bulk_transfer_refused(client, session):
    h = await auth(session)
    _, held, sku = await _held(client, h)
    free = await lot(client, h, sku, 1)
    loc = await _location(client, h)
    r = await client.post("/items/bulk/transfer", headers=h, json={"entity_ids": [held, free], "to_location_id": loc})
    assert HELD in r.text or "is held by memo" in r.text, r.text
    assert (await item(client, h, free)).get("location_id") == loc
    assert (await item(client, h, held)).get("location_id") != loc


async def test_held_lot_adjust_down_refused(client, session):
    h = await auth(session)
    _, held, _ = await _held(client, h)
    _refused(await client.post(f"/items/{held}/adjust", headers=h, json={"new_qty": 0}))
    assert (await item(client, h, held))["quantity"] == 1


async def test_held_lot_adjust_up_refused(client, session):
    h = await auth(session)
    _, held, _ = await _held(client, h)
    _refused(await client.post(f"/items/{held}/adjust", headers=h, json={"new_qty": 3}))
    assert (await item(client, h, held))["quantity"] == 1


async def test_held_lot_patch_quantity_refused(client, session):
    h = await auth(session)
    _, held, _ = await _held(client, h)
    _refused(await _patch(client, h, held, "quantity", 5))
    assert (await item(client, h, held))["quantity"] == 1


async def test_held_lot_patch_weight_refused(client, session):
    h = await auth(session)
    _, held, _ = await _held(client, h)
    _refused(await _patch(client, h, held, "weight", 2.5))
    assert not (await item(client, h, held)).get("weight")


async def test_held_lot_patch_location_refused(client, session):
    h = await auth(session)
    _, held, _ = await _held(client, h)
    before = (await item(client, h, held)).get("location_id")
    _refused(await _patch(client, h, held, "location_id", await _location(client, h)))
    assert (await item(client, h, held)).get("location_id") == before


async def test_held_lot_import_quantity_refused(client, session):
    h = await auth(session)
    _, held, sku = await _held(client, h)
    free_sku = f"FR-{uuid.uuid4().hex[:6]}"
    free = await lot(client, h, free_sku, 1)
    rows = [{"entity_id": eid, "event_type": "item.patched", "source": "csv", "idempotency_key": uuid.uuid4().hex,
             "data": {"sku": s, "quantity": 7}} for eid, s in ((held, sku), (free, free_sku))]
    body = await ok(await client.post("/items/import/batch", headers=h, json={"records": rows}))
    assert len(body["errors"]) == 1 and HELD in str(body["errors"][0])
    assert (await item(client, h, held))["quantity"] == 1
    assert (await item(client, h, free))["quantity"] == 7


async def test_held_lot_raw_event_refused(client, session):
    from celerp.events.engine import emit_event

    a = await company_auth(session, uuid.uuid4(), uuid.uuid4())
    h = a["headers"]
    memo, held, _ = await _held(client, h, units=4)

    async def refused(**extra):
        with pytest.raises(HTTPException) as e:
            async with session.begin_nested():
                await emit_event(session, company_id=a["company_id"], entity_id=held, entity_type="item",
                                 event_type="item.quantity.adjusted", data={"new_qty": 2}, actor_id=None,
                                 location_id=None, idempotency_key=uuid.uuid4().hex, **extra)
        assert e.value.status_code == 409
        assert (await item(client, h, held))["quantity"] == 4

    await refused(source="test", metadata_={})
    await refused(source="fulfill_split", metadata_={"reason": "split_parent"})

    from celerp.events.engine import HeldLotCarve

    other = await lot(client, h, "OT-" + uuid.uuid4().hex[:6], 1)
    await refused(source="fulfill_split", metadata_={}, held_lot_carve=HeldLotCarve(other, 2, None, None))
    await refused(source="fulfill_split", metadata_={}, held_lot_carve=HeldLotCarve(held, 1, None, None))
    assert await owner(client, h, held) == ("memo_out", memo)


async def test_held_lot_metadata_edit_allowed(client, session):
    h = await auth(session)
    memo, held, _ = await _held(client, h)
    await ok(await _patch(client, h, held, "name", "Renamed ring"))
    await ok(await _patch(client, h, held, "description", "Platinum band"))
    got = await item(client, h, held)
    assert (got["name"], got["description"]) == ("Renamed ring", "Platinum band")
    assert await owner(client, h, held) == ("memo_out", memo)


async def test_held_lot_document_led_changes_pass(client, session):
    h = await auth(session)
    memo, lids, lots = await memo_out(client, h, [(10, 10.0)])
    mother = lots[0][0]
    await ok(await client.post(f"/docs/{memo}/revert-lines", headers=h,
                               json={"line_ids": lids, "quantities": {lids[0]: 3}}))
    assert (await item(client, h, mother))["quantity"] == 7
    inv = await drawn(client, h, memo, {lids[0]: 2})
    assert (await item(client, h, mother))["quantity"] == 5
    drawn_line = [li for li in (await state(client, h, inv))["line_items"] if li.get("drawn_from")][0]
    await ok(await back_to_memo(client, h, inv, [drawn_line["line_id"]]))
    assert await owner(client, h, drawn_line["item_id"]) == ("memo_out", memo)
    await ok(await client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_ids": lids}))
    assert (await item(client, h, mother))["status"] == "available"
    await ok(await client.post(f"/items/{mother}/transfer", headers=h, json={"to_location_id": await _location(client, h)}))

    memo2, _, lots2 = await memo_out(client, h, [(1, 10.0)])
    held = lots2[0][0]
    sibling = await lot(client, h, (await item(client, h, held))["sku"], 1)
    assert (await client.post(f"/items/{held}/split", headers=h, json={"children": [{"quantity": 0.5}]})).status_code == 404
    assert (await client.post(f"/items/{held}/transform", headers=h, json={
        "child_sku": "X-" + uuid.uuid4().hex[:6], "child_category": "Processed", "child_sell_by": "piece",
        "child_quantity": 1})).status_code == 404
    assert (await client.post("/items/merge", headers=h, json={
        "source_entity_ids": [held, sibling], "target_sku_from": held})).status_code == 409
    assert await owner(client, h, held) == ("memo_out", memo2)

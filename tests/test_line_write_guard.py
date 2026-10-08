# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A line that holds stock, shipped stock or received goods keeps its identity.

Every write of a document's or List's lines passes one rule: such a line may not be
removed, given another line id or bound to another item, and a shipped or received line
may not change position either (older shipments and receipts name their line by
position). Quantities, prices and descriptions stay editable. The rule lives at the
event boundary, so the tests also write the lines directly, as any writer could."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from line_actions_support import doc, h, item, line, line_ids, lot, quotation, set_list_lines, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _reserve(client, h, entity_id, ids, status="reserved"):
    path = "lists" if entity_id.startswith("list:") else "docs"
    return await client.post(f"/{path}/{entity_id}/reserve-lines", headers=h,
                             json={"line_ids": ids, "new_status": status})


async def _held_draft(client, h, sku):
    """A draft invoice of two lines whose first line holds its lot."""
    a = await lot(client, h, sku, 5)
    b = await lot(client, h, sku + "-B", 5)
    d = await doc(client, h, [line(a, 5, sku=sku), line(b, 1, sku=sku + "-B")])
    l0, _l1 = await line_ids(client, h, d)
    r = await _reserve(client, h, d, [l0])
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{d}/revert-to-draft", headers=h, json={})
    assert r.status_code == 200, r.text
    return d, a, b


async def _patch_lines(client, h, d, lines):
    return await client.patch(f"/docs/{d}", headers=h,
                              json={"fields_changed": {"line_items": {"new": lines}}})


def _key(r):
    return r.json()["detail"]["message_key"]


async def test_a_held_line_cannot_be_removed_until_released(client, h):
    d, a, _b = await _held_draft(client, h, "LW-1")
    lines = (await state(client, h, d))["line_items"]
    r = await _patch_lines(client, h, d, lines[1:])
    assert r.status_code == 409, r.text
    assert _key(r) == "line.protected_held"
    assert (await item(client, h, a))["status"] == "reserved"
    l0 = lines[0]["line_id"]
    assert (await _reserve(client, h, d, [l0], "available")).status_code == 200
    r = await _patch_lines(client, h, d, lines[1:])
    assert r.status_code == 200, r.text


async def test_a_held_line_cannot_be_bound_to_another_item_or_given_a_new_id(client, h):
    d, _a, _b = await _held_draft(client, h, "LW-2")
    other = await lot(client, h, "LW-2-C", 5)
    lines = (await state(client, h, d))["line_items"]
    rebound = [dict(lines[0], item_id=other, entity_id=other, sku="LW-2-C"), lines[1]]
    r = await _patch_lines(client, h, d, rebound)
    assert r.status_code == 409, r.text
    assert _key(r) == "line.protected_held"
    renamed = [dict(lines[0], line_id=str(uuid.uuid4())), lines[1]]
    r = await _patch_lines(client, h, d, renamed)
    assert r.status_code == 409, r.text
    assert _key(r) == "line.protected_held"


async def test_a_held_line_keeps_quantity_price_and_order_edits(client, h):
    d, _a, _b = await _held_draft(client, h, "LW-3")
    lines = (await state(client, h, d))["line_items"]
    edited = [lines[1], dict(lines[0], quantity=4, unit_price=12.5, description="Edited")]
    r = await _patch_lines(client, h, d, edited)
    assert r.status_code == 200, r.text
    after = (await state(client, h, d))["line_items"]
    assert [li["line_id"] for li in after] == [lines[1]["line_id"], lines[0]["line_id"]]
    assert after[1]["quantity"] == 4


async def test_a_list_line_that_holds_stock_cannot_be_removed(client, h):
    a = await lot(client, h, "LW-4", 3)
    q = await quotation(client, h, [line(a, 3, sku="LW-4")])
    (l0,) = await line_ids(client, h, q)
    assert (await _reserve(client, h, q, [l0])).status_code == 200
    r = await set_list_lines(client, h, q, [])
    assert r.status_code == 409, r.text
    assert _key(r) == "line.protected_held"
    assert await line_ids(client, h, q) == [l0]


async def _shipped_invoice(client, h, sku):
    a = await lot(client, h, sku, 2)
    b = await lot(client, h, sku + "-B", 2)
    d = await doc(client, h, [line(a, 2, sku=sku), line(b, 2, sku=sku + "-B")])
    l0, _l1 = await line_ids(client, h, d)
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": [l0]})
    assert r.status_code == 200, r.text
    return d


async def _write_lines(session, entity_id, entity_type, lines):
    """Write a record's lines straight through the event boundary, as any writer does."""
    from celerp.events.engine import emit_event
    from celerp.models.projections import Projection
    row = (await session.execute(select(Projection).where(Projection.entity_id == entity_id))).scalars().one()
    await emit_event(
        session, company_id=row.company_id, entity_id=entity_id, entity_type=entity_type,
        event_type=f"{entity_type}.updated",
        data={"fields_changed": {"line_items": {"old": row.state.get("line_items"), "new": lines}}},
        actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()), metadata_={},
    )


async def _refused(session, entity_id, lines, key, entity_type="doc"):
    with pytest.raises(HTTPException) as exc:
        await _write_lines(session, entity_id, entity_type, lines)
    await session.rollback()
    assert exc.value.status_code == 409
    assert exc.value.detail["message_key"] == key


async def test_a_shipped_line_cannot_be_removed_moved_or_rebound_by_any_writer(client, session, h):
    d = await _shipped_invoice(client, h, "LW-5")
    lines = (await state(client, h, d))["line_items"]
    await _refused(session, d, lines[1:], "line.protected_shipped")
    await _refused(session, d, [lines[1], lines[0]], "line.protected_shipped")
    other = await lot(client, h, "LW-5-C", 2)
    await _refused(session, d, [dict(lines[0], item_id=other, entity_id=other), lines[1]], "line.protected_shipped")
    # The patch route refuses it too.
    r = await _patch_lines(client, h, d, lines[1:])
    assert r.status_code == 409, r.text
    # A description edit is not an identity change.
    await _write_lines(session, d, "doc", [dict(lines[0], description="Renamed"), lines[1]])
    await session.commit()
    assert (await state(client, h, d))["line_items"][0]["description"] == "Renamed"


async def _received_bill(client, h, sku):
    r = await client.post("/docs", headers=h, json={
        "doc_type": "bill", "ref_id": f"BILL-{uuid.uuid4().hex[:6]}",
        "line_items": [{"name": "Widget", "sku": sku, "quantity": 2, "unit_price": 15.0, "sell_by": "piece"},
                       {"name": "Freight", "description": "Freight", "quantity": 1, "unit_price": 5.0}],
        "subtotal": 35, "tax": 0, "total": 35,
    })
    assert r.status_code == 200, r.text
    bill = r.json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=h, json={
        "location_id": "", "received_items": [{"sku": sku, "name": "Widget", "quantity_received": 2.0}]})
    assert r.status_code == 200, r.text
    return bill


async def test_a_received_line_cannot_be_removed_moved_or_rekinded_by_any_writer(client, session, h):
    bill = await _received_bill(client, h, f"LW-6-{uuid.uuid4().hex[:4]}")
    lines = (await state(client, h, bill))["line_items"]
    await _refused(session, bill, lines[1:], "line.protected_received")
    await _refused(session, bill, [lines[1], lines[0]], "line.protected_received")
    await _refused(session, bill, [dict(lines[0], receive_as="expense"), lines[1]], "line.protected_received")
    await _refused(session, bill, [dict(lines[0], line_id=str(uuid.uuid4())), lines[1]], "line.protected_received")
    # The line nothing was received on may go; the received one stays where it is.
    await _write_lines(session, bill, "doc", lines[:1])
    await session.commit()
    assert [li["line_id"] for li in (await state(client, h, bill))["line_items"]] == [lines[0]["line_id"]]

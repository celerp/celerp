# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reserving and releasing chosen lines of a document or List.

A line is chosen by its line id. A reservation holds the line's full quantity or
nothing; it holds the line's own reserved lots first, its bound lot next, then other
lots of the same product. Running it again only adds what the line now lacks or
gives back what it no longer needs, and a line never touches another line's hold."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from line_actions_support import doc, h, item, line, line_ids, lot, quotation, set_list_lines, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _item_events(session) -> int:
    from celerp.models.ledger import LedgerEntry
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.entity_type == "item"))).scalar_one()


async def _reserve(client, h, entity_id, ids, status="reserved", **extra):
    path = "lists" if entity_id.startswith("list:") else "docs"
    return await client.post(f"/{path}/{entity_id}/reserve-lines", headers=h,
                             json={"line_ids": ids, "new_status": status, **extra})


async def _held_by(session, owner):
    """Every lot ``owner`` holds, as {lot id: (quantity, line id)}."""
    from celerp.models.projections import Projection
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.entity_type == "item",
        Projection.state["status_doc_id"].as_string() == owner))).scalars().all()
    return {r.entity_id: (float(r.state["quantity"]), r.state.get("status_line_entity_id"))
            for r in rows if r.state.get("status") == "reserved"}


async def test_same_bound_lot_on_two_lines_reserves_each_line_alone(client, session, h):
    a = await lot(client, h, "RS-1", 11)
    d = await doc(client, h, [line(a, 4, sku="RS-1"), line(a, 7, sku="RS-1")])
    l0, l1 = await line_ids(client, h, d)
    r = await _reserve(client, h, d, [l0])
    assert r.status_code == 200, r.text
    held = await _held_by(session, d)
    assert sorted(held.values()) == [(4.0, l0)]
    lines = (await state(client, h, d))["line_items"]
    assert lines[1]["item_id"] == a and lines[0]["item_id"] != a
    assert (await item(client, h, a))["status"] == "available"

    r = await _reserve(client, h, d, [l1])
    assert r.status_code == 200, r.text
    held = await _held_by(session, d)
    assert sorted(held.values()) == [(4.0, l0), (7.0, l1)]

    r = await _reserve(client, h, d, [l0], "available")
    assert r.status_code == 200, r.text
    held = await _held_by(session, d)
    assert list(held.values()) == [(7.0, l1)]


async def test_selection_refusals(client, session, h):
    a = await lot(client, h, "RS-2", 11)
    b = await lot(client, h, "RS-2B", 5)
    d = await doc(client, h, [line(a, 4, sku="RS-2"), line(a, 7, sku="RS-2"), line(b, 5, sku="RS-2B")])
    l0, _l1, _l2 = await line_ids(client, h, d)
    both = await client.post(f"/docs/{d}/reserve-lines", headers=h, json={
        "line_ids": [l0], "line_entity_ids": [a], "new_status": "reserved"})
    assert both.status_code == 422
    neither = await client.post(f"/docs/{d}/reserve-lines", headers=h, json={"new_status": "reserved"})
    assert neither.status_code == 422
    unknown = await _reserve(client, h, d, ["99999999-9999-4999-8999-999999999999"])
    assert unknown.status_code == 422
    ambiguous = await client.post(f"/docs/{d}/reserve-lines", headers=h, json={
        "line_entity_ids": [a], "new_status": "reserved"})
    assert ambiguous.status_code == 409
    assert (await item(client, h, a))["status"] == "available"
    unique = await client.post(f"/docs/{d}/reserve-lines", headers=h, json={
        "line_entity_ids": [b], "new_status": "reserved"})
    assert unique.status_code == 200, unique.text
    assert (await item(client, h, b))["status_line_entity_id"] == _l2


async def test_reserve_spans_lots_and_leaves_the_rest(client, session, h):
    a = await lot(client, h, "RS-3", 10)
    b = await lot(client, h, "RS-3", 15)
    d = await doc(client, h, [line(a, 20, sku="RS-3")])
    (l0,) = await line_ids(client, h, d)
    r = await _reserve(client, h, d, [l0])
    assert r.status_code == 200, r.text
    held = await _held_by(session, d)
    assert sorted(q for q, _ in held.values()) == [10.0, 10.0]
    assert all(lid == l0 for _q, lid in held.values())
    left = await item(client, h, b)
    assert left["status"] == "available" and float(left["quantity"]) == 5.0


async def test_short_reserve_changes_nothing(client, session, h):
    a = await lot(client, h, "RS-4", 10)
    b = await lot(client, h, "RS-4", 5)
    d = await doc(client, h, [line(a, 20, sku="RS-4")])
    (l0,) = await line_ids(client, h, d)
    before = await _item_events(session)
    r = await _reserve(client, h, d, [l0])
    assert r.status_code == 409, r.text
    assert await _item_events(session) == before
    for eid, qty in ((a, 10.0), (b, 5.0)):
        st = await item(client, h, eid)
        assert st["status"] == "available" and float(st["quantity"]) == qty


async def test_reserve_again_takes_the_change_in_quantity(client, session, h):
    a = await lot(client, h, "RS-5", 10)
    q = await quotation(client, h, [line(a, 4, sku="RS-5")])
    (l0,) = await line_ids(client, h, q)
    assert (await _reserve(client, h, q, [l0])).status_code == 200
    lines = (await state(client, h, q))["line_items"]
    lines[0]["quantity"] = 6
    assert (await set_list_lines(client, h, q, lines)).status_code == 200
    r = await _reserve(client, h, q, [l0])
    assert r.status_code == 200, r.text
    held = await _held_by(session, q)
    assert sorted(held.values()) == [(2.0, l0), (4.0, l0)]

    lines = (await state(client, h, q))["line_items"]
    lines[0]["quantity"] = 3
    assert (await set_list_lines(client, h, q, lines)).status_code == 200
    r = await _reserve(client, h, q, [l0])
    assert r.status_code == 200, r.text
    held = await _held_by(session, q)
    bound = (await state(client, h, q))["line_items"][0]["item_id"]
    assert held == {bound: (3.0, l0)}
    # The parent kept 4; 3 are held and the 3 given back sit in their own lots.
    assert float((await item(client, h, a))["quantity"]) == 4.0


async def test_release_takes_back_the_whole_hold(client, session, h):
    a = await lot(client, h, "RS-6", 10)
    b = await lot(client, h, "RS-6", 15)
    d = await doc(client, h, [line(a, 20, sku="RS-6")])
    (l0,) = await line_ids(client, h, d)
    assert (await _reserve(client, h, d, [l0])).status_code == 200
    r = await _reserve(client, h, d, [l0], "available")
    assert r.status_code == 200, r.text
    assert await _held_by(session, d) == {}
    st = await item(client, h, a)
    assert st["status"] == "available" and "status_line_entity_id" not in st
    nothing = await _reserve(client, h, d, [l0], "available")
    assert nothing.status_code == 422
    assert b


async def test_same_key_replays_and_a_no_op_is_recorded(client, session, h):
    from celerp.models.ledger import LedgerEntry
    a = await lot(client, h, "RS-7", 10)
    d = await doc(client, h, [line(a, 10, sku="RS-7")])
    (l0,) = await line_ids(client, h, d)
    first = await _reserve(client, h, d, [l0], idempotency_key="k-reserve-1")
    assert first.status_code == 200, first.text
    after_first = await _item_events(session)
    again = await _reserve(client, h, d, [l0], idempotency_key="k-reserve-1")
    assert again.status_code == 200 and again.json() == first.json()
    assert await _item_events(session) == after_first

    noop = await _reserve(client, h, d, [l0], idempotency_key="k-reserve-2")
    assert noop.status_code == 200, noop.text
    assert await _item_events(session) == after_first
    recorded = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.event_type == "line_action.recorded",
        LedgerEntry.idempotency_key == "k-reserve-2"))).scalars().all()
    assert len(recorded) == 1
    replay = await _reserve(client, h, d, [l0], idempotency_key="k-reserve-2")
    assert replay.status_code == 200 and replay.json() == noop.json()

    other = await _reserve(client, h, d, [l0], "available", idempotency_key="k-reserve-2")
    assert other.status_code == 409

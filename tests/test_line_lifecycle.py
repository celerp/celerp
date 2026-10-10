# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What happens to a record's reserved stock when the record itself moves on.

Voiding, deleting, writing off or closing a record gives back everything it holds,
including lots of the same product it holds beyond its bound lot. Converting a
quotation hands every hold to the new document with the line it is held for. Reverting
to draft keeps the holds. A hold nothing can release any more (its record is gone or
void, or its line was removed) can be freed with a status edit; a live hold cannot."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from line_actions_support import doc, h, item, line, line_ids, lot, quotation, set_list_lines, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _reserve(client, h, entity_id, ids, status="reserved"):
    path = "lists" if entity_id.startswith("list:") else "docs"
    return await client.post(f"/{path}/{entity_id}/reserve-lines", headers=h,
                             json={"line_ids": ids, "new_status": status})


async def _held_by(session, owner):
    from celerp.models.projections import Projection
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.entity_type == "item",
        Projection.state["status_doc_id"].as_string() == owner))).scalars().all()
    return {r.entity_id: (float(r.state["quantity"]), r.state.get("status_line_entity_id"))
            for r in rows if r.state.get("status") == "reserved"}


async def _spanning_hold(client, session, h, sku, *, doc_type="invoice"):
    """A finalized document whose one line holds its bound lot and part of a sibling lot."""
    a = await lot(client, h, sku, 10)
    b = await lot(client, h, sku, 10)
    d = await doc(client, h, [line(a, 15, sku=sku)], doc_type=doc_type)
    (l0,) = await line_ids(client, h, d)
    r = await _reserve(client, h, d, [l0])
    assert r.status_code == 200, r.text
    held = await _held_by(session, d)
    assert sorted(q for q, _ in held.values()) == [5.0, 10.0]
    return d, l0, list(held)


async def _all_free(client, h, ids):
    for eid in ids:
        st = await item(client, h, eid)
        assert st["status"] == "available", st
        assert not st.get("status_doc_id") and not st.get("status_line_entity_id"), st


async def _revert(client, h, d):
    r = await client.post(f"/docs/{d}/revert-to-draft", headers=h, json={})
    assert r.status_code == 200, r.text


async def test_void_gives_back_every_hold(client, session, h):
    d, _l0, held = await _spanning_hold(client, session, h, "LC-1")
    r = await client.post(f"/docs/{d}/void", headers=h, json={})
    assert r.status_code == 200, r.text
    assert await _held_by(session, d) == {}
    await _all_free(client, h, held)


async def test_revert_to_draft_keeps_holds_and_a_draft_can_release_but_not_reserve(client, session, h):
    d, l0, held = await _spanning_hold(client, session, h, "LC-2")
    await _revert(client, h, d)
    assert set(await _held_by(session, d)) == set(held)
    again = await _reserve(client, h, d, [l0])
    assert again.status_code == 200, again.text  # holding exactly its quantity takes nothing new
    r = await _reserve(client, h, d, [l0], "available")
    assert r.status_code == 200, r.text
    await _all_free(client, h, held)
    refused = await _reserve(client, h, d, [l0])
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["message_key"] == "lines.reserve_status"


async def test_delete_of_a_draft_gives_back_its_holds(client, session, h):
    d, _l0, held = await _spanning_hold(client, session, h, "LC-3", doc_type="memo")
    await _revert(client, h, d)
    r = await client.delete(f"/docs/{d}", headers=h)
    assert r.status_code == 200, r.text
    await _all_free(client, h, held)


async def test_bulk_delete_of_drafts_gives_back_their_holds(client, session, h):
    d, _l0, held = await _spanning_hold(client, session, h, "LC-4", doc_type="memo")
    await _revert(client, h, d)
    r = await client.delete("/docs/bulk-draft", headers=h, params={"doc_ids": d})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == [d]
    await _all_free(client, h, held)


async def _list_hold(client, session, h, sku):
    a = await lot(client, h, sku, 10)
    b = await lot(client, h, sku, 10)
    q = await quotation(client, h, [line(a, 15, sku=sku)])
    (l0,) = await line_ids(client, h, q)
    r = await _reserve(client, h, q, [l0])
    assert r.status_code == 200, r.text
    held = await _held_by(session, q)
    assert sorted(v for v, _ in held.values()) == [5.0, 10.0]
    return q, l0, list(held)


async def test_list_void_gives_back_every_hold(client, session, h):
    q, _l0, held = await _list_hold(client, session, h, "LC-5")
    r = await client.post(f"/lists/{q}/void", headers=h, json={})
    assert r.status_code == 200, r.text
    await _all_free(client, h, held)


async def test_list_delete_gives_back_every_hold(client, session, h):
    q, _l0, held = await _list_hold(client, session, h, "LC-6")
    r = await client.delete(f"/lists/{q}", headers=h)
    assert r.status_code == 200, r.text
    await _all_free(client, h, held)


async def test_convert_hands_every_hold_to_the_new_document_with_its_line(client, session, h):
    q, l0, held = await _list_hold(client, session, h, "LC-7")
    r = await client.post(f"/lists/{q}/finalize", headers=h, json={})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{q}/convert", headers=h, json={"target_type": "invoice"})
    assert r.status_code == 200, r.text
    new_doc = r.json()["target_doc_id"]
    assert await _held_by(session, q) == {}
    moved = await _held_by(session, new_doc)
    assert set(moved) == set(held)
    assert {lid for _q, lid in moved.values()} == {l0}
    assert await line_ids(client, h, new_doc) == [l0]


async def test_write_off_gives_back_the_holds_of_its_list(client, session, h):
    a = await lot(client, h, "LC-8", 4)
    r = await client.post("/lists/writeoff", headers=h, json={"entity_ids": [a]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    (l0,) = await line_ids(client, h, wo)
    r = await _reserve(client, h, wo, [l0])
    assert r.status_code == 200, r.text
    assert (await item(client, h, a))["status"] == "reserved"
    r = await client.post(f"/lists/{wo}/writeoff-line", headers=h,
                          json={"line_id": l0, "qty_out": 1, "account": "6970"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/lists/{wo}/write-off", headers=h)
    assert r.status_code == 200, r.text
    assert await _held_by(session, wo) == {}


async def _stray_hold(session, owner: str, line_id: str | None, sku: str, status="reserved", near=None):
    """A lot stamped as held by ``owner`` the way an earlier release left it behind."""
    from datetime import datetime, timezone
    from celerp.models.projections import Projection
    company_id = (await session.execute(select(Projection.company_id).where(
        Projection.entity_id == (owner if near is None else near)))).scalars().first()
    now = datetime.now(timezone.utc)
    eid = f"item:{uuid.uuid4()}"
    st = {"sku": sku, "name": sku, "quantity": 1, "status": status, "sell_by": "piece",
          "status_doc_id": owner}
    if line_id:
        st["status_line_entity_id"] = line_id
    session.add(Projection(company_id=company_id, entity_id=eid, entity_type="item",
                           version=1, created_at=now, updated_at=now, state=st))
    await session.commit()
    return eid


async def _set_status(client, h, eid, status="available"):
    return await client.post(f"/items/{eid}/status", headers=h, json={"new_status": status})


async def test_a_live_hold_cannot_be_freed_by_a_status_edit(client, session, h):
    d, _l0, held = await _spanning_hold(client, session, h, "LC-9")
    r = await _set_status(client, h, held[0])
    assert r.status_code == 409, r.text
    assert (await item(client, h, held[0]))["status"] == "reserved"


async def test_a_hold_whose_line_is_gone_can_be_freed(client, session, h):
    d, _l0, _held = await _spanning_hold(client, session, h, "LC-10")
    stray = await _stray_hold(session, d, str(uuid.uuid4()), "LC-10")
    r = await _set_status(client, h, stray)
    assert r.status_code == 200, r.text
    st = await item(client, h, stray)
    assert st["status"] == "available" and not st.get("status_doc_id")


async def test_a_hold_whose_record_is_void_or_gone_can_be_freed(client, session, h):
    d, _l0, _held = await _spanning_hold(client, session, h, "LC-11")
    r = await client.post(f"/docs/{d}/void", headers=h, json={})
    assert r.status_code == 200, r.text
    stray = await _stray_hold(session, d, None, "LC-11")
    assert (await _set_status(client, h, stray)).status_code == 200
    gone = await _stray_hold(session, "doc:NO-SUCH-DOC", None, "LC-11", near=d)
    # The owner row does not exist at all: proof enough that nothing holds it.
    gone_r = await _set_status(client, h, gone)
    assert gone_r.status_code == 200, gone_r.text


async def test_goods_out_on_memo_or_sold_are_never_freed_by_a_status_edit(client, session, h):
    d, _l0, _held = await _spanning_hold(client, session, h, "LC-12")
    r = await client.post(f"/docs/{d}/void", headers=h, json={})
    assert r.status_code == 200, r.text
    memo = await _stray_hold(session, d, None, "LC-12", status="memo_out")
    assert (await _set_status(client, h, memo)).status_code in (409, 422)
    assert (await item(client, h, memo))["status"] == "memo_out"


async def _patch_draft_qty(client, h, d, qty):
    st = await state(client, h, d)
    lines = [dict(li) for li in st["line_items"]]
    lines[0]["quantity"] = qty
    r = await client.patch(f"/docs/{d}", headers=h, json={"fields_changed": {"line_items": {"new": lines}}})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{d}/finalize", headers=h)
    assert r.status_code == 200, r.text


async def _fulfil(client, h, d, ids):
    return await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": ids})


async def test_holding_less_than_the_line_ships_the_hold_and_free_stock(client, session, h):
    a = await lot(client, h, "LC-13", 10)
    d = await doc(client, h, [line(a, 4, sku="LC-13")])
    (l0,) = await line_ids(client, h, d)
    assert (await _reserve(client, h, d, [l0])).status_code == 200
    await _revert(client, h, d)
    await _patch_draft_qty(client, h, d, 6)
    r = await _fulfil(client, h, d, [l0])
    assert r.status_code == 200, r.text
    assert await _held_by(session, d) == {}
    shipped = 0.0
    for eid in r.json()["fulfilled"]:
        shipped += float((await item(client, h, eid))["quantity"])
    assert shipped == 6.0


async def test_holding_more_than_the_line_refuses_to_ship_until_reserved_again(client, session, h):
    a = await lot(client, h, "LC-14", 10)
    d = await doc(client, h, [line(a, 6, sku="LC-14")])
    (l0,) = await line_ids(client, h, d)
    assert (await _reserve(client, h, d, [l0])).status_code == 200
    await _revert(client, h, d)
    await _patch_draft_qty(client, h, d, 2)
    r = await _fulfil(client, h, d, [l0])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "lines.cannot_fulfil"
    assert sorted(q for q, _ in (await _held_by(session, d)).values()) == [6.0]
    assert (await _reserve(client, h, d, [l0])).status_code == 200
    assert sorted(q for q, _ in (await _held_by(session, d)).values()) == [2.0]
    r = await _fulfil(client, h, d, [l0])
    assert r.status_code == 200, r.text

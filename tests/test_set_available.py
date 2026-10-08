# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Set as available is one request for every chosen line of a record.

A line that holds stock gives its hold back; a line this record shipped takes its goods
back, whole or the part the user names. Every line is checked first and the record
changes once, so a mix of held, sold and memo lines comes back together or not at all.
A List can only give back holds: goods sold or out on memo are taken back on the
document that shipped them."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from line_actions_support import doc, h, item, line, line_ids, lot, quotation, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _reserve(client, h, entity_id, ids):
    path = "lists" if entity_id.startswith("list:") else "docs"
    r = await client.post(f"/{path}/{entity_id}/reserve-lines", headers=h,
                          json={"line_ids": ids, "new_status": "reserved"})
    assert r.status_code == 200, r.text


async def _fulfil(client, h, d, ids):
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": ids})
    assert r.status_code == 200, r.text
    return r.json()["fulfilled"]


async def _set_available(client, h, entity_id, **body):
    path = "lists" if entity_id.startswith("list:") else "docs"
    return await client.post(f"/{path}/{entity_id}/set-available", headers=h, json=body)


def _key(r):
    return r.json()["detail"]["message_key"]


async def _held_and_sold(client, h, sku, doc_type="invoice"):
    a = await lot(client, h, sku + "-A", 2)
    b = await lot(client, h, sku + "-B", 2)
    c = await lot(client, h, sku + "-C", 2)
    d = await doc(client, h, [line(a, 2, sku=sku + "-A"), line(b, 2, sku=sku + "-B"),
                              line(c, 2, sku=sku + "-C")], doc_type=doc_type)
    l0, l1, l2 = await line_ids(client, h, d)
    await _reserve(client, h, d, [l0])
    await _fulfil(client, h, d, [l1])
    return d, (a, b, c), (l0, l1, l2)


async def test_held_and_shipped_lines_come_back_in_one_request(client, session, h):
    from celerp.models.ledger import LedgerEntry
    d, (a, b, _c), (l0, l1, _l2) = await _held_and_sold(client, h, "SA-1")
    r = await _set_available(client, h, d, line_ids=[l0, l1])
    assert r.status_code == 200, r.text
    assert r.json()["released"] == [a]
    assert r.json()["reverted"] == [b]
    for eid in (a, b):
        st = await item(client, h, eid)
        assert st["status"] == "available" and not st.get("status_doc_id"), st
    recorded = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.event_type == "line_action.recorded",
        LedgerEntry.data["owner_id"].as_string() == d))).scalars().all()
    assert [e.data["action"] for e in recorded][-1] == "set-available"


async def test_one_line_with_nothing_to_give_back_changes_nothing(client, h):
    d, (a, b, _c), (l0, l1, l2) = await _held_and_sold(client, h, "SA-2")
    r = await _set_available(client, h, d, line_ids=[l0, l1, l2])
    assert r.status_code == 422, r.text
    assert _key(r) == "lines.cannot_set_available"
    assert r.json()["detail"]["message"] == "Cannot set as available: SA-2-C: nothing is held or out on this line"
    assert (await item(client, h, a))["status"] == "reserved"
    assert (await item(client, h, b))["status"] == "sold"


async def test_a_stale_set_available_names_the_action_the_user_took(client, h):
    """A second tab sets a held line available after the first already did: the refusal is
    about setting it as available, not about taking goods back."""
    d, _lots, (l0, _l1, _l2) = await _held_and_sold(client, h, "SA-5")
    assert (await _set_available(client, h, d, line_ids=[l0])).status_code == 200
    r = await _set_available(client, h, d, line_ids=[l0])
    assert r.status_code == 422, r.text
    assert _key(r) == "lines.cannot_set_available"
    assert r.json()["detail"]["message"] == "Cannot set as available: SA-5-A: nothing is held or out on this line"
    assert "take back" not in r.json()["detail"]["message"].lower()


async def test_a_memo_line_comes_back_in_part_beside_a_released_hold(client, h):
    a = await lot(client, h, "SA-3A", 2)
    b = await lot(client, h, "SA-3B", 5)
    d = await doc(client, h, [line(a, 2, sku="SA-3A"), line(b, 5, sku="SA-3B")], doc_type="memo")
    l0, l1 = await line_ids(client, h, d)
    await _reserve(client, h, d, [l0])
    await _fulfil(client, h, d, [l1])
    r = await _set_available(client, h, d, line_ids=[l0, l1], quantities={l1: 2})
    assert r.status_code == 200, r.text
    assert r.json()["released"] == [a]
    (back,) = r.json()["partially_returned"]
    assert back["quantity"] == 2
    st = await item(client, h, b)
    assert st["status"] == "memo_out" and float(st["quantity"]) == 3


async def test_a_quantity_on_a_held_line_is_refused(client, h):
    d, (a, _b, _c), (l0, _l1, _l2) = await _held_and_sold(client, h, "SA-4", doc_type="memo")
    r = await _set_available(client, h, d, line_ids=[l0], quantities={l0: 1})
    assert r.status_code == 422, r.text
    assert (await item(client, h, a))["status"] == "reserved"


async def test_a_draft_gives_back_its_holds(client, h):
    a = await lot(client, h, "SA-5", 2)
    d = await doc(client, h, [line(a, 2, sku="SA-5")])
    (l0,) = await line_ids(client, h, d)
    await _reserve(client, h, d, [l0])
    r = await client.post(f"/docs/{d}/revert-to-draft", headers=h, json={})
    assert r.status_code == 200, r.text
    r = await _set_available(client, h, d, line_ids=[l0])
    assert r.status_code == 200, r.text
    assert (await item(client, h, a))["status"] == "available"


async def test_a_record_that_may_not_take_back_refuses_the_hold_too(client, session, h):
    from celerp.models.projections import Projection
    d, (a, b, _c), (l0, l1, _l2) = await _held_and_sold(client, h, "SA-6")
    row = (await session.execute(select(Projection).where(Projection.entity_id == d))).scalars().one()
    row.state = dict(row.state, status="void")
    await session.commit()
    r = await _set_available(client, h, d, line_ids=[l0, l1])
    assert r.status_code == 409, r.text
    assert (await item(client, h, a))["status"] == "reserved"
    assert (await item(client, h, b))["status"] == "sold"


async def test_a_sale_made_by_another_record_is_not_taken_back(client, h):
    a = await lot(client, h, "SA-7", 1)
    x = await doc(client, h, [line(a, 1, sku="SA-7")])
    y = await doc(client, h, [line(a, 1, sku="SA-7")])
    (lx,) = await line_ids(client, h, x)
    (ly,) = await line_ids(client, h, y)
    await _fulfil(client, h, y, [ly])
    r = await _set_available(client, h, x, line_ids=[lx])
    assert r.status_code == 422, r.text
    st = await item(client, h, a)
    assert st["status"] == "sold" and st["status_doc_id"] == y


async def test_one_key_gives_back_once_and_a_new_request_under_it_is_refused(client, session, h):
    from celerp.models.ledger import LedgerEntry
    d, (a, b, _c), (l0, l1, l2) = await _held_and_sold(client, h, "SA-8")
    key = str(uuid.uuid4())
    first = await _set_available(client, h, d, line_ids=[l0, l1], idempotency_key=key)
    assert first.status_code == 200, first.text
    again = await _set_available(client, h, d, line_ids=[l0, l1], idempotency_key=key)
    assert again.status_code == 200 and again.json() == first.json()
    other = await _set_available(client, h, d, line_ids=[l2], idempotency_key=key)
    assert other.status_code in (409, 422), other.text
    reversals = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == b, LedgerEntry.event_type == "item.fulfillment_reversed"))).scalars().all()
    assert len(reversals) == 1


async def test_a_list_gives_back_its_holds(client, h):
    a = await lot(client, h, "SA-9", 3)
    q = await quotation(client, h, [line(a, 3, sku="SA-9")])
    (l0,) = await line_ids(client, h, q)
    await _reserve(client, h, q, [l0])
    r = await _set_available(client, h, q, line_ids=[l0])
    assert r.status_code == 200, r.text
    assert r.json()["released"] == [a]
    assert (await item(client, h, a))["status"] == "available"


async def test_a_list_refuses_goods_a_document_shipped(client, h):
    a = await lot(client, h, "SA-10A", 1)
    b = await lot(client, h, "SA-10B", 3)
    q = await quotation(client, h, [line(a, 1, sku="SA-10A"), line(b, 3, sku="SA-10B")])
    lq0, lq1 = await line_ids(client, h, q)
    await _reserve(client, h, q, [lq1])
    d = await doc(client, h, [line(a, 1, sku="SA-10A")])
    (ld,) = await line_ids(client, h, d)
    await _fulfil(client, h, d, [ld])
    r = await _set_available(client, h, q, line_ids=[lq0, lq1])
    assert r.status_code == 422, r.text
    assert _key(r) == "lines.shipped_elsewhere"
    assert (await item(client, h, a))["status"] == "sold"
    assert (await item(client, h, b))["status"] == "reserved"


def _plain(detail, code: str) -> str:
    from ui import i18n
    try:
        i18n.set_lang(code)
        return i18n.refusal_text(detail)
    finally:
        i18n.set_lang("en")


@pytest.mark.parametrize("path", ["set-available", "revert-lines"])
async def test_taking_back_part_of_a_piece_is_refused(client, h, path):
    """A piece item comes back in whole pieces: half a piece is refused in plain words,
    and nothing is split off."""
    a = await lot(client, h, "SA-P", 3)
    d = await doc(client, h, [line(a, 3, sku="SA-P")], doc_type="memo")
    [lid] = await line_ids(client, h, d)
    await _fulfil(client, h, d, [lid])
    r = await client.post(f"/docs/{d}/{path}", headers=h, json={"line_ids": [lid], "quantities": {lid: 0.5}})
    assert r.status_code == 422, r.text
    assert _key(r) == "quantity.precision"
    text = _plain(r.json()["detail"], "es")
    assert "0.5" in text and "SA-P" in text and "precision" not in text, text
    st = await item(client, h, a)
    assert st["status"] == "memo_out" and float(st["quantity"]) == 3
    r = await client.post(f"/docs/{d}/{path}", headers=h, json={"line_ids": [lid], "quantities": {lid: 1}})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("finalize", [False, True])
async def test_a_line_holding_nothing_is_refused_plainly(client, h, finalize):
    """Set as available stays offered on a line that holds nothing; choosing it says the line
    holds nothing, on a draft as on a finalized record."""
    a = await lot(client, h, f"SA-6-{int(finalize)}", 1)
    d = await doc(client, h, [line(a, 1, sku=f"SA-6-{int(finalize)}")], finalize=finalize)
    (l0,) = await line_ids(client, h, d)
    r = await _set_available(client, h, d, line_ids=[l0])
    assert r.status_code == 422, r.text
    assert _key(r) == "lines.cannot_set_available"
    assert r.json()["detail"]["message"] == (
        f"Cannot set as available: SA-6-{int(finalize)}: nothing is held or out on this line")

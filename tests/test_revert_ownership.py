# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Set as available on shipped lines takes back only what this record shipped.

Before anything changes, every chosen lot must be out (sold or on memo) for this record
and for the chosen line, read from what its shipment recorded. A lot sold by another
record, a lot nothing records the line of, a line with nothing out, or a record whose
status does not allow it refuses the whole request, so nothing is half taken back."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from line_actions_support import doc, h, item, line, line_ids, lot, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _fulfil(client, h, d, ids):
    r = await client.post(f"/docs/{d}/fulfill-lines", headers=h, json={"line_ids": ids})
    assert r.status_code == 200, r.text
    return r.json()["fulfilled"]


async def _revert(client, h, d, **body):
    return await client.post(f"/docs/{d}/revert-lines", headers=h, json=body)


def _key(r):
    return r.json()["detail"]["message_key"]


async def test_reverting_a_line_by_id_takes_back_every_lot_it_shipped(client, h):
    a = await lot(client, h, "RV-1", 10)
    await lot(client, h, "RV-1", 10)
    d = await doc(client, h, [line(a, 15, sku="RV-1")])
    (l0,) = await line_ids(client, h, d)
    shipped = await _fulfil(client, h, d, [l0])
    assert len(shipped) == 2
    r = await _revert(client, h, d, line_ids=[l0])
    assert r.status_code == 200, r.text
    assert sorted(r.json()["reverted"]) == sorted(shipped)
    for eid in shipped:
        assert (await item(client, h, eid))["status"] == "available"
    assert r.json()["fulfillment_status"] == "unfulfilled"


async def test_two_lines_sharing_one_lot_revert_only_the_chosen_line(client, h):
    a = await lot(client, h, "RV-2", 10)
    d = await doc(client, h, [line(a, 3, sku="RV-2"), line(a, 4, sku="RV-2")])
    l0, l1 = await line_ids(client, h, d)
    first = await _fulfil(client, h, d, [l0])
    second = await _fulfil(client, h, d, [l1])
    r = await _revert(client, h, d, line_ids=[l1])
    assert r.status_code == 200, r.text
    assert sorted(r.json()["reverted"]) == sorted(second)
    for eid in first:
        assert (await item(client, h, eid))["status"] == "sold"
    for eid in second:
        assert (await item(client, h, eid))["status"] == "available"


async def test_one_invalid_line_takes_nothing_back(client, h):
    a = await lot(client, h, "RV-3A", 1)
    b = await lot(client, h, "RV-3B", 1)
    d = await doc(client, h, [line(a, 1, sku="RV-3A"), line(b, 1, sku="RV-3B")], doc_type="memo")
    l0, l1 = await line_ids(client, h, d)
    await _fulfil(client, h, d, [l0])
    r = await _revert(client, h, d, line_entity_ids=[a, b])
    assert r.status_code == 422, r.text
    assert (await item(client, h, a))["status"] == "memo_out"
    r = await _revert(client, h, d, line_ids=[l0, l1])
    assert r.status_code == 422, r.text
    assert _key(r) == "lines.cannot_revert"
    assert (await item(client, h, a))["status"] == "memo_out"


async def test_a_lot_sold_by_another_record_is_never_taken_back(client, h):
    a = await lot(client, h, "RV-4", 1)
    # A memo names the lot as well: only one invoice may book its cost.
    x = await doc(client, h, [line(a, 1, sku="RV-4")], doc_type="memo")
    y = await doc(client, h, [line(a, 1, sku="RV-4")])
    (ly,) = await line_ids(client, h, y)
    await _fulfil(client, h, y, [ly])
    r = await _revert(client, h, x, line_entity_ids=[a])
    assert r.status_code == 422, r.text
    assert _key(r) == "lines.cannot_revert"
    st = await item(client, h, a)
    assert st["status"] == "sold" and st["status_doc_id"] == y


async def _legacy_out_lot(session, owner: str, sku: str) -> str:
    """A lot out on memo for ``owner`` from before shipments recorded their line."""
    from celerp.models.projections import Projection
    company_id = (await session.execute(select(Projection.company_id).where(
        Projection.entity_id == owner))).scalars().first()
    now = datetime.now(timezone.utc)
    eid = f"item:{uuid.uuid4()}"
    session.add(Projection(company_id=company_id, entity_id=eid, entity_type="item", version=1,
                           created_at=now, updated_at=now,
                           state={"sku": sku, "name": sku, "quantity": 1, "status": "memo_out",
                                  "sell_by": "piece", "status_doc_id": owner}))
    await session.commit()
    return eid


async def test_a_legacy_shipment_whose_line_cannot_be_told_is_refused(client, session, h):
    a = await lot(client, h, "RV-5", 2)
    b = await lot(client, h, "RV-5", 2)
    d = await doc(client, h, [line(a, 2, sku="RV-5"), line(b, 2, sku="RV-5")], doc_type="memo")
    l0, _l1 = await line_ids(client, h, d)
    await _fulfil(client, h, d, [l0])
    legacy = await _legacy_out_lot(session, d, "RV-5")
    r = await _revert(client, h, d, line_entity_ids=[legacy])
    assert r.status_code == 422, r.text
    assert _key(r) == "lines.cannot_revert"
    r = await _revert(client, h, d, line_ids=[l0])
    assert r.status_code == 422, r.text
    assert (await item(client, h, legacy))["status"] == "memo_out"
    assert (await item(client, h, a))["status"] == "memo_out"


async def test_a_record_in_a_terminal_status_refuses(client, session, h):
    from celerp.models.projections import Projection
    a = await lot(client, h, "RV-6", 1)
    d = await doc(client, h, [line(a, 1, sku="RV-6")])
    (l0,) = await line_ids(client, h, d)
    await _fulfil(client, h, d, [l0])
    row = (await session.execute(select(Projection).where(Projection.entity_id == d))).scalars().one()
    row.state = dict(row.state, status="void")
    await session.commit()
    r = await _revert(client, h, d, line_ids=[l0])
    assert r.status_code == 409, r.text
    assert _key(r) == "lines.revert_status"
    assert (await item(client, h, a))["status"] == "sold"


async def test_revertible_statuses_exclude_terminal_and_draft():
    from celerp_docs.doc_constants import REVERTIBLE_STATUSES
    assert set(REVERTIBLE_STATUSES) == {"memo", "invoice"}
    for statuses in REVERTIBLE_STATUSES.values():
        assert not statuses & {"draft", "void", "closed", "converted"}


async def test_part_of_a_line_comes_back_by_line_id(client, h):
    a = await lot(client, h, "RV-7", 5)
    d = await doc(client, h, [line(a, 5, sku="RV-7")], doc_type="memo")
    (l0,) = await line_ids(client, h, d)
    await _fulfil(client, h, d, [l0])
    r = await _revert(client, h, d, line_ids=[l0], quantities={l0: 2})
    assert r.status_code == 200, r.text
    (back,) = r.json()["partially_returned"]
    assert back["quantity"] == 2
    assert (await item(client, h, back["item_id"]))["status"] == "available"
    st = await item(client, h, a)
    assert st["status"] == "memo_out" and float(st["quantity"]) == 3


async def test_a_memo_line_says_how_much_is_still_out(client, h):
    a = await lot(client, h, "RV-9", 5)
    await lot(client, h, "RV-9", 5)
    d = await doc(client, h, [line(a, 8, sku="RV-9")], doc_type="memo")
    (l0,) = await line_ids(client, h, d)
    await _fulfil(client, h, d, [l0])

    async def _out():
        r = await client.get(f"/docs/{d}", headers=h)
        assert r.status_code == 200, r.text
        return r.json()["line_items"][0]["out_quantity"]

    assert await _out() == 8
    r = await _revert(client, h, d, line_ids=[l0], quantities={l0: 4})
    assert r.status_code == 200, r.text
    assert await _out() == 4


async def test_a_repeated_revert_with_one_key_takes_back_once(client, session, h):
    from celerp.models.ledger import LedgerEntry
    a = await lot(client, h, "RV-8", 1)
    d = await doc(client, h, [line(a, 1, sku="RV-8")])
    (l0,) = await line_ids(client, h, d)
    await _fulfil(client, h, d, [l0])
    key = str(uuid.uuid4())
    first = await _revert(client, h, d, line_ids=[l0], idempotency_key=key)
    assert first.status_code == 200, first.text
    again = await _revert(client, h, d, line_ids=[l0], idempotency_key=key)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    reversals = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id == a, LedgerEntry.event_type == "item.fulfillment_reversed"))).scalars().all()
    assert len(reversals) == 1

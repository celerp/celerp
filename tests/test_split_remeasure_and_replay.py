# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A split moves value only between its parts, and a re-weigh of the mother is a stock count.

Splitting a lot carves its cost across the parts, so the books do not move. When the user
re-weighs the mother to a quantity other than what the split leaves, the difference is the
same stock count POST /items/{id}/adjust records: the same journal entry and the same
adjustment event. A split sent again under the same request key returns the first split and
changes nothing; the key covers every part the split created.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from stock_books import assert_settled
from test_cost_restatement import _item, _state
from test_money_stock_and_contact_invariants import _account_net

pytestmark = pytest.mark.asyncio

_ACCOUNTS = ("1130-OB", "4300", "6970")


async def _gl(session, auth) -> dict[str, float]:
    return {c: round(await _account_net(session, auth["company_id"], c), 2) for c in _ACCOUNTS}


def _delta(before: dict, after: dict) -> dict[str, float]:
    return {k: round(after[k] - before[k], 2) for k in after if round(after[k] - before[k], 2)}


async def _split(client, auth, lot: str, **body):
    return await client.post(f"/items/{lot}/split", headers=auth["headers"], json=body)


async def _ok(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


async def _events(session, auth, lot: str, event_type: str) -> list[LedgerEntry]:
    session.expire_all()
    return list((await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == lot,
        LedgerEntry.event_type == event_type).order_by(LedgerEntry.id))).scalars())


async def _children(session, auth, lot: str) -> list[str]:
    return [c for e in await _events(session, auth, lot, "item.split") for c in e.data["child_ids"]]


async def _twin_and_subject(client, session, auth, mother_qty: float):
    """Two lots of 10 at 100.00. The twin splits 3 off, then a stock count sets it to
    ``mother_qty``; the subject splits 3 off with the mother re-weighed to ``mother_qty``."""
    twin = await _item(client, auth, 100.0, qty=10)
    subject = await _item(client, auth, 100.0, qty=10)
    await _ok(await _split(client, auth, twin, children=[{"quantity": 3}]))
    g0 = await _gl(session, auth)
    await _ok(await client.post(f"/items/{twin}/adjust", headers=auth["headers"], json={"new_qty": mother_qty}))
    g1 = await _gl(session, auth)
    await _ok(await _split(client, auth, subject, children=[{"quantity": 3}], mother_qty=mother_qty))
    g2 = await _gl(session, auth)
    return twin, subject, _delta(g0, g1), _delta(g1, g2)


# -- L2-04: a re-weighed mother posts through the stock adjustment ----------------------------

async def test_a_mother_re_weighed_above_the_remainder_books_the_found_unit_as_a_count(client, session, auth):
    """Remainder 7, re-weighed 8: the found unit enters at the lot's average (10.00), Dr stock
    10.00 / Cr stock gain 10.00, exactly as a count from 7 to 8; the mother is 8 at 80.00 and
    carries the adjustment event the count records."""
    twin, subject, twin_d, subject_d = await _twin_and_subject(client, session, auth, 8)
    assert twin_d == {"1130-OB": 10.0, "4300": -10.0}, twin_d
    assert subject_d == twin_d, (subject_d, twin_d)
    t, s = await _state(session, auth, twin), await _state(session, auth, subject)
    assert (float(s["quantity"]), round(float(s["cost_total"]), 2)) == (8.0, 80.0)
    assert round(float(s["cost_total"]), 2) == round(float(t["cost_total"]), 2)
    counts = [e for e in await _events(session, auth, subject, "item.quantity.adjusted")
              if (e.metadata_ or {}).get("reason") != "split_parent"]
    assert [e.data["new_qty"] for e in counts] == [8], counts
    await assert_settled(client, session, auth)


async def test_a_mother_re_weighed_below_the_remainder_books_the_missing_unit_as_a_count(client, session, auth):
    """Neighbour: remainder 7, re-weighed 6: the missing unit leaves at 10.00, as a count from
    7 to 6 books it; the mother is 6 at 60.00."""
    twin, subject, twin_d, subject_d = await _twin_and_subject(client, session, auth, 6)
    assert twin_d and subject_d == twin_d, (subject_d, twin_d)
    s = await _state(session, auth, subject)
    assert (float(s["quantity"]), round(float(s["cost_total"]), 2)) == (6.0, 60.0)
    await assert_settled(client, session, auth)


@pytest.mark.parametrize("mother_qty", [None, 7.0], ids=["no_override", "override_equal_to_remainder"])
async def test_a_split_without_a_re_weigh_moves_no_books(client, session, auth, mother_qty):
    """Neighbour: a split that leaves the mother at the derived remainder posts nothing and
    records no stock count; the parts add back to the lot's 100.00."""
    lot = await _item(client, auth, 100.0, qty=10)
    g0 = await _gl(session, auth)
    body = {"children": [{"quantity": 3}]} | ({"mother_qty": mother_qty} if mother_qty is not None else {})
    await _ok(await _split(client, auth, lot, **body))
    assert _delta(g0, await _gl(session, auth)) == {}
    assert all((e.metadata_ or {}).get("reason") == "split_parent"
               for e in await _events(session, auth, lot, "item.quantity.adjusted"))
    [child] = await _children(session, auth, lot)
    m, c = await _state(session, auth, lot), await _state(session, auth, child)
    assert round(float(m["cost_total"]) + float(c["cost_total"]), 2) == 100.0
    await assert_settled(client, session, auth)


# -- EF-13: the request key covers the whole split --------------------------------------------

async def test_a_split_sent_again_under_its_key_splits_once(client, session, auth):
    """The same split delivered twice under one key: the second returns the first's result,
    the re-weighed mother stays at 8, there is one part, and the books do not move again."""
    lot = await _item(client, auth, 100.0, qty=10)
    body = {"children": [{"quantity": 3}], "mother_qty": 8, "idempotency_key": "split-once-1"}
    first = await _ok(await _split(client, auth, lot, **body))
    g1 = await _gl(session, auth)
    again = await _ok(await _split(client, auth, lot, **body))
    assert again == first
    assert float((await _state(session, auth, lot))["quantity"]) == 8.0
    assert len(await _children(session, auth, lot)) == 1
    assert _delta(g1, await _gl(session, auth)) == {}
    await assert_settled(client, session, auth)


async def test_a_key_already_used_for_another_split_is_refused(client, session, auth):
    """Neighbour: the key names one split. Sent with other parts, or for another lot, it is
    refused and nothing changes."""
    lot = await _item(client, auth, 100.0, qty=10)
    other = await _item(client, auth, 100.0, qty=10)
    await _ok(await _split(client, auth, lot, children=[{"quantity": 3}], idempotency_key="split-once-2"))
    for target, children in ((lot, [{"quantity": 2}]), (other, [{"quantity": 3}])):
        r = await _split(client, auth, target, children=children, idempotency_key="split-once-2")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["message_key"] == "items.split_key_reused", r.text
    assert float((await _state(session, auth, lot))["quantity"]) == 7.0
    assert float((await _state(session, auth, other))["quantity"]) == 10.0
    await assert_settled(client, session, auth)


async def test_splits_under_different_keys_are_separate_splits(client, session, auth):
    """Neighbour: two splits with their own keys (or none) each take their part."""
    lot = await _item(client, auth, 100.0, qty=10)
    await _ok(await _split(client, auth, lot, children=[{"quantity": 3}], idempotency_key="split-a"))
    await _ok(await _split(client, auth, lot, children=[{"quantity": 3}], idempotency_key="split-b"))
    await _ok(await _split(client, auth, lot, children=[{"quantity": 1}]))
    assert float((await _state(session, auth, lot))["quantity"]) == 3.0
    assert len(await _children(session, auth, lot)) == 3
    await assert_settled(client, session, auth)

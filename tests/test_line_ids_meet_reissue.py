"""Line ids meet a reissued invoice: an invoice imported from an older version, whose lines carry
only the ids the backfill derived, goes through ship, set available, revert, a removed line and a
second issue, and each line action still finds its lines by those ids within the current issue.
Another invoice taking a lot from the reissued one names it once, and a lot the reissue removed
is taken from nobody."""
from __future__ import annotations

import pytest

from line_actions_support import backfill_line_ids
from test_cost_follows_goods import COGS
from test_money_stock_and_contact_invariants import _account_net
from test_reissued_invoice_cogs import (
    _invoice, _line_ids, _remove_first_line_and_reissue, _ship, _ship_back_and_revert, _stones,
)
from test_set_aside_goods_follow_the_lot import _per_doc

pytestmark = pytest.mark.asyncio


async def _legacy_reissued(client, session, auth):
    stones = await _stones(client, auth)
    doc = await _invoice(client, auth, stones)
    await backfill_line_ids(session, doc)
    await _ship_back_and_revert(client, auth, doc, 3)
    await _remove_first_line_and_reissue(client, auth, doc)
    return doc, stones


async def test_legacy_invoice_reissued_ships_remaining_lines_by_derived_ids(client, session, auth):
    doc, stones = await _legacy_reissued(client, session, auth)
    ids = await _line_ids(client, auth, doc)
    assert len(ids) == len(stones) - 1 and all(ids)
    r = await _ship(client, auth, doc, ids)
    assert r.status_code == 200, r.text
    kept = round(sum(cost for _lot, _sku, cost in stones[1:]), 2)
    assert (await _per_doc(session, auth)).get(doc) == kept
    assert await _account_net(session, auth["company_id"], COGS) == kept

    again = await _ship(client, auth, doc, ids)
    assert again.status_code == 422, again.text
    assert again.json()["detail"]["message_key"] == "documents.shipped_lines_skipped_many"
    assert await _account_net(session, auth["company_id"], COGS) == kept
    assert (await _per_doc(session, auth)).get(doc) == kept


async def test_another_invoice_takes_a_kept_lot_of_a_reissued_invoice_from_it_once(client, session, auth):
    doc, stones = await _legacy_reissued(client, session, auth)
    other = await _invoice(client, auth, [stones[1]])
    r = await _ship(client, auth, other, await _line_ids(client, auth, other))
    assert r.status_code == 200, r.text
    [taken] = r.json()["taken_from"]
    assert (taken["doc_id"], taken["lot_id"], taken["takes"]) == (doc, stones[1][0], "all")


async def test_a_lot_the_reissue_removed_is_taken_from_nobody(client, session, auth):
    _doc, stones = await _legacy_reissued(client, session, auth)
    other = await _invoice(client, auth, [stones[0]])
    r = await _ship(client, auth, other, await _line_ids(client, auth, other))
    assert r.status_code == 200, r.text
    assert r.json()["taken_from"] == []

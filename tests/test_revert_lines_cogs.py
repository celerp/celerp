# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Setting a shipped invoice line available takes the goods back into stock, so the invoice
gives up that line's cost of sales in the same transaction. Selling the lot again costs it
once; reserving it back to the invoice keeps the cost where it was."""
from __future__ import annotations

import pytest

from stock_books import assert_settled
from test_cost_restatement import _doc_cogs, _item, _sell, _state, auth, ids  # noqa: F401  (auth and ids are fixtures)


async def _revert(client, auth, doc: str, *lots: str) -> None:
    r = await client.post(f"/docs/{doc}/revert-lines", headers=auth["headers"], json={"line_entity_ids": list(lots)})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_lot_set_available_and_sold_again_is_costed_once(client, session, auth):
    lot = await _item(client, auth, 40.0)
    first = await _sell(client, session, auth, lot)
    assert await _doc_cogs(session, auth, first) == 40.0
    await _revert(client, auth, first, lot)
    assert (await _state(session, auth, lot))["status"] == "available"
    assert await _doc_cogs(session, auth, first) == 0.0
    await assert_settled(client, session, auth)
    second = await _sell(client, session, auth, lot)
    assert (await _doc_cogs(session, auth, first), await _doc_cogs(session, auth, second)) == (0.0, 40.0)
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_only_the_reverted_line_gives_up_its_cost(client, session, auth):
    a, b = await _item(client, auth, 40.0), await _item(client, auth, 70.0)
    doc = await _sell(client, session, auth, a, b)
    assert await _doc_cogs(session, auth, doc) == 110.0
    await _revert(client, auth, doc, a)
    assert await _doc_cogs(session, auth, doc) == 70.0
    await assert_settled(client, session, auth)


@pytest.mark.asyncio
async def test_a_shipped_line_reserved_back_to_the_invoice_keeps_its_cost(client, session, auth):
    lot = await _item(client, auth, 40.0)
    doc = await _sell(client, session, auth, lot)
    r = await client.post(f"/docs/{doc}/reserve-lines", headers=auth["headers"],
                          json={"line_entity_ids": [lot], "new_status": "reserved"})
    assert r.status_code == 200, r.text
    state = await _state(session, auth, lot)
    assert (state["status"], state["status_doc_id"]) == ("reserved", doc)
    assert await _doc_cogs(session, auth, doc) == 40.0

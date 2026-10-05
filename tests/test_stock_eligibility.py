# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Which stock a demand document can consume, decided in one place.

An item is free stock for any document while it is available, reserved stock only for the
document it is reserved to, and nothing at all in every other status. Fulfillment and Demand
Planning read that same answer, so the board never counts stock an order could not ship.
"""
from __future__ import annotations

import pytest
from celerp.models.projections import Projection
from test_mfg_outstanding_demand import _invoice, _row, _stocked

pytestmark = pytest.mark.asyncio

_DOC, _OTHER = "doc:mine", "doc:other"

# Every legal item status, classified for the document that wants it. Adding a status to
# ITEM_STATUSES without classifying it here fails test_every_item_status_is_classified.
_EXPECTED: dict[str, tuple[str | None, str | None]] = {
    # status: (reserved to this document, reserved to another document)
    "available": ("free", "free"),
    "active": ("free", "free"),
    "reserved": ("reserved", None),
    "draft": (None, None),
    "sold": (None, None),
    "archived": (None, None),
    "merged": (None, None),
    "expired": (None, None),
    "memo_out": (None, None),
    "returned": (None, None),
    "disposed": (None, None),
}


def test_every_item_status_is_classified():
    from celerp_inventory.projections import demand_claim
    from celerp_inventory.routes import ITEM_STATUSES

    assert set(_EXPECTED) == set(ITEM_STATUSES)
    for status, (own, other) in _EXPECTED.items():
        assert demand_claim({"status": status, "status_doc_id": _DOC}, _DOC) == own, status
        assert demand_claim({"status": status, "status_doc_id": _OTHER}, _DOC) == other, status
    # Reserved to no document: no document can ship it.
    assert demand_claim({"status": "reserved"}, _DOC) is None
    assert demand_claim({"status": "reserved"}, None) is None
    # An item with no status holds the projection default, available.
    assert demand_claim({}, _DOC) == "free"


async def _set_status(session, auth, item: str, status: str, owner: str | None) -> None:
    """Put the item straight into ``status`` (held by ``owner``), as its own action would leave it."""
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": item})
    row.state = {**row.state, "status": status, "status_doc_id": owner}
    await session.commit()


async def test_a_returned_lot_does_not_cover_demand(client, session, auth):
    """A status edit can set "returned"; fulfillment cannot ship such stock, so it covers nothing."""
    fg, _ = await _stocked(client, auth, 5)
    order = await _invoice(client, auth, (fg, 5))
    r = await client.post(f"/items/{fg}/status", headers=auth["headers"], json={"new_status": "returned"})
    assert r.status_code == 200, r.text

    row = await _row(client, auth, fg)
    assert (row["on_hand"], row["to_make"]) == (0, 5)
    assert [(d["doc_id"], d["covered"], d["shortfall"]) for d in row["docs"]] == [(order, 0, 5)]
    r = await client.post(f"/docs/{order}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [fg]})
    assert r.status_code == 422, r.text


_CASES = [(s, "mine") for s in sorted(_EXPECTED)] + [("reserved", "other"), ("reserved", "nobody")]


@pytest.mark.parametrize("where", ["line", "sibling lot"])
@pytest.mark.parametrize("status,holder", _CASES)
async def test_planner_and_fulfillment_agree(client, session, auth, status, holder, where):
    """One order for five. The stock in ``status`` is either the line's own item (all five) or a
    sibling lot of four beside one free unit on the line. Demand Planning counts it as covering
    the order exactly when fulfillment can ship the order from it."""
    fg, lots = await _stocked(client, auth, *((5,) if where == "line" else (1, 4)))
    order = await _invoice(client, auth, (fg, 5))
    owner = {"mine": order, "other": "doc:someone-else", "nobody": None}[holder]
    await _set_status(session, auth, lots[0] if lots else fg, status, owner)
    claim = _EXPECTED[status][0 if holder == "mine" else 1] if holder != "nobody" else None

    row = await _row(client, auth, fg)
    covered = {d["doc_id"]: d["covered"] for d in row["docs"]}.get(order)
    r = await client.post(f"/docs/{order}/fulfill-lines", headers=auth["headers"], json={"line_entity_ids": [fg]})

    assert (covered == 5) == (r.status_code == 200), (covered, r.status_code, r.text)
    assert (r.status_code == 200) == (claim is not None), r.text

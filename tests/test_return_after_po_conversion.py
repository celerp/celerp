# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods a purchase order received can go back to the supplier after the order becomes a bill.

Converting an order to a bill changes its type, not where its receipts put the goods. A
receipt onto a lot already on hand still added to that lot, and a receipt that made a new
parcel still made it, so return by line finds both kinds of lot, sends back what was asked
from the lot the goods are in, and the books and what the bill owes follow. Receipts made
before lots recorded what they added are read from the receipt events themselves.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_cost_restatement import _item, _state
from test_receipt_accounting import _OPENING, _books, _doc, _finalize
from test_receive_selected_lines import _post, _stamp_line_ids
from test_return_selected_lines import _qty, _return_lines

pytestmark = pytest.mark.asyncio


async def _ordered_and_received(client, session, auth, lines: list[dict], *qtys: float):
    """A purchase order with ``lines``, each received in full (or ``qtys``), as stock."""
    po = await _doc(client, auth, "purchase_order", lines)
    ids = await _stamp_line_ids(session, auth, po)
    for line_id, line, qty in zip(ids, lines, qtys or [li["quantity"] for li in lines]):
        r = await _post(client, auth, po, {"source_line_id": line_id, "quantity_received": qty,
                                           "receive_as": "stock",
                                           **{k: line[k] for k in ("item_id", "sku", "name") if k in line}})
        assert r.status_code == 200, r.text
    return po, ids


async def _converted(client, session, auth, doc_id: str) -> dict:
    await _finalize(client, auth, doc_id)
    doc = await _state(session, auth, doc_id)
    assert doc["doc_type"] == "bill"
    return doc


async def test_po_receipt_into_existing_lot_returns_after_conversion(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po, [line_id] = await _ordered_and_received(
        client, session, auth, [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    assert await _qty(session, auth, lot) == 15
    await _converted(client, session, auth, po)
    before = await _books(session, auth, "1130-OB", "2110")
    assert before == {"1130-OB": _OPENING + 70.0, "2110": -70.0}

    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 5

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 200, r.text
    # The lot keeps its own ten and the two received units that stayed.
    lot_state = await _state(session, auth, lot)
    assert (lot_state["quantity"], lot_state["cost_base"]) == (12, pytest.approx(_OPENING + 28.0))
    [entry] = (await _state(session, auth, po))["returned_items"]
    assert (entry["item_id"], entry["quantity_returned"], entry["source_line_id"],
            entry["lot_quantity_taken"], entry["lot_cost_taken"]) == (lot, 3, line_id, 3, 42.0)
    # The three units leave at what they were received for, and come off what the bill owes.
    assert await _books(session, auth, "1130-OB", "2110") == {"1130-OB": _OPENING + 28.0, "2110": -28.0}
    doc = await _state(session, auth, po)
    assert doc["amount_outstanding"] == pytest.approx(28.0)
    assert doc["status"] == "partial_returned"

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_line_not_on_hand"
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 10
    assert await _books(session, auth, "1130-OB", "2110") == {"1130-OB": _OPENING, "2110": 0.0}
    assert (await _state(session, auth, po))["status"] == "returned"


async def test_po_receipt_into_new_lot_and_existing_lot_return_after_conversion(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    sku = f"PCN-{uuid.uuid4().hex[:6]}"
    po, [on_hand, fresh] = await _ordered_and_received(client, session, auth, [
        {"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0},
        {"sku": sku, "name": "Fresh", "quantity": 4, "unit_price": 3.0},
    ])
    [parcel] = (await _state(session, auth, po))["received_item_ids"]
    await _converted(client, session, auth, po)
    before = await _books(session, auth, "1130-OB", "1130-P", "2110")

    r = await _return_lines(client, auth, po, {"line_id": on_hand, "quantity_returned": 1},
                            {"line_id": fresh, "quantity_returned": 4})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 14
    assert (await _state(session, auth, parcel))["status"] == "disposed"
    returned = (await _state(session, auth, po))["returned_items"]
    assert [(x["item_id"], x["quantity_returned"], x["source_line_id"]) for x in returned] == [
        (lot, 1, on_hand), (parcel, 4, fresh)]
    after = await _books(session, auth, "1130-OB", "1130-P", "2110")
    assert after == {"1130-OB": pytest.approx(before["1130-OB"] - 14.0),
                     "1130-P": pytest.approx(before["1130-P"] - 12.0),
                     "2110": pytest.approx(before["2110"] + 26.0)}


async def _strip_lot_provenance(session, auth, doc_id: str) -> None:
    """Store the document's receipts as an earlier version recorded them: a purchase order
    receipt onto a lot on hand said nothing of what it added, in the event or the document."""
    session.expire_all()
    for entry in (await session.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == doc_id,
            LedgerEntry.event_type == "doc.received"))).scalars():
        entry.data = {**entry.data, "received_items": [
            {k: v for k, v in x.items() if k not in ("lot_quantity_added", "lot_cost_added")}
            for x in entry.data["received_items"]]}
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": doc_id})
    row.state = {**row.state, "received_items": [
        {k: v for k, v in x.items() if k not in ("lot_quantity_added", "lot_cost_added")}
        for x in row.state["received_items"]]}
    await session.commit()


async def test_legacy_po_receipt_returns_before_and_after_conversion(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    sku = f"PCL-{uuid.uuid4().hex[:6]}"
    po, [on_hand, fresh] = await _ordered_and_received(client, session, auth, [
        {"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 10.0},
        {"sku": sku, "name": "Fresh", "quantity": 2, "unit_price": 3.0},
    ])
    [parcel] = (await _state(session, auth, po))["received_item_ids"]
    await _strip_lot_provenance(session, auth, po)

    r = await _return_lines(client, auth, po, {"line_id": on_hand, "quantity_returned": 1})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 14
    await _converted(client, session, auth, po)

    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert [li.get("returnable_quantity") for li in doc["line_items"]] == [4, 2]
    before = await _books(session, auth, "1130-OB", "2110")
    r = await _return_lines(client, auth, po, {"line_id": on_hand, "quantity_returned": 2},
                            {"line_id": fresh, "quantity_returned": 1})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 12
    assert await _qty(session, auth, parcel) == 1
    # Without a record of what the receipt added, the units leave at the lot's own unit cost.
    after = await _books(session, auth, "1130-OB", "2110")
    assert after["1130-OB"] == pytest.approx(before["1130-OB"] - 20.0)
    r = await _return_lines(client, auth, po, {"line_id": on_hand, "quantity_returned": 3})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_line_not_on_hand"
    # By lot, the same goods are still the bill's to send back.
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 10

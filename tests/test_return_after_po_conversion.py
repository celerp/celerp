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
from stock_books import assert_books_carry_stock

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


async def _imported_received_po(client, auth, lot: str, qty: float = 5,
                                received: float = 5) -> tuple[str, str]:
    """A purchase order for ``qty`` imported with ``received`` of it already received: that
    receipt onto ``lot`` is part of the document as imported, with no receipt event of its
    own. -> (doc id, line id)."""
    doc_id, line_id = f"doc:{uuid.uuid4()}", str(uuid.uuid4())
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": doc_id, "event_type": "doc.created", "source": "test",
        "idempotency_key": uuid.uuid4().hex, "data": {
            "doc_type": "purchase_order", "status": "received" if received >= qty else "partially_received",
            "doc_number": f"PO-IMP-{uuid.uuid4().hex[:5]}",
            "line_items": [{"item_id": lot, "name": "Lot", "quantity": qty, "unit_price": 14.0, "line_id": line_id}],
            "subtotal": 14.0 * qty, "total": 14.0 * qty, "amount_outstanding": 14.0 * qty,
            "received_items": [{"item_id": lot, "po_line_index": 0, "quantity_received": float(received),
                                "receive_as": "stock"}],
            "received_item_ids": []}})
    assert r.status_code == 200, r.text
    return doc_id, line_id


async def test_imported_po_receipt_returns_by_item_and_by_line(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot)

    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 5
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 200, r.text
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 1})
    assert r.status_code == 200, r.text
    # The import carried the order's stock separately, so the returns take it off the lot's ten.
    assert await _qty(session, auth, lot) == 7
    returned = (await _state(session, auth, po))["returned_items"]
    assert [(x["item_id"], x["quantity_returned"]) for x in returned] == [(lot, 2), (lot, 1)]
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 2



async def _stock_books(session, auth) -> dict[str, float]:
    return await _books(session, auth, "1130-OB", "1130-P", "2110")


async def test_undoing_a_receipt_the_order_booked_then_receiving_and_returning_keeps_the_books(client, session, auth):
    """A receipt onto a lot on hand is undone after the order becomes a bill, received again
    on the bill and partly sent back: each stock account holds what its lots hold and
    accounts payable what the bill owes, as on a bill that never was an order."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    [line_id] = await _stamp_line_ids(session, auth, po)

    async def receive():
        r = await _post(client, auth, po, {"source_line_id": line_id, "item_id": lot,
                                           "quantity_received": 5, "receive_as": "stock"})
        assert r.status_code == 200, r.text

    await receive()
    await _finalize(client, auth, po)
    assert (await _state(session, auth, po))["doc_type"] == "bill"
    assert await _stock_books(session, auth) == {"1130-OB": _OPENING + 70.0, "1130-P": 0.0, "2110": -70.0}

    r = await client.delete(f"/docs/{po}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    lot_state = await _state(session, auth, lot)
    assert (lot_state["quantity"], lot_state["cost_total"]) == (10, _OPENING)
    # The lot's account holds the lot again; the bill still owes for goods it booked and has
    # not received, as a bill finalized before its goods came in does.
    undone = {"1130-OB": _OPENING, "1130-P": 70.0, "2110": -70.0}
    assert await _stock_books(session, auth) == undone

    r = await client.delete(f"/docs/{po}/receive", headers=auth["headers"])
    assert r.status_code == 200 and r.json().get("already_undone") is True, r.text
    assert await _stock_books(session, auth) == undone

    await receive()
    [parcel] = (await _state(session, auth, po))["received_item_ids"]
    assert (await _state(session, auth, parcel))["cost_total"] == 70.0
    assert await _stock_books(session, auth) == {"1130-OB": _OPENING, "1130-P": 70.0, "2110": -70.0}

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 200, r.text
    [entry] = (await _state(session, auth, po))["returned_items"]
    assert entry["item_id"] == parcel
    assert (await _state(session, auth, parcel))["cost_total"] == 28.0
    assert (await _state(session, auth, lot))["cost_total"] == _OPENING
    assert await _stock_books(session, auth) == {"1130-OB": _OPENING, "1130-P": 28.0, "2110": -28.0}
    assert (await _state(session, auth, po))["amount_outstanding"] == 28.0


_BOOKS = ("1130-OB", "1130-P", "2110")


async def _held(session, auth, doc_id: str, lot: str) -> tuple:
    """What a refused return must leave alone: the books, the lot and the document."""
    session.expire_all()
    lot_state, doc = await _state(session, auth, lot), await _state(session, auth, doc_id)
    return (await _books(session, auth, *_BOOKS), lot_state.get("quantity"), lot_state.get("cost_base"),
            doc.get("returned_items"), doc.get("amount_outstanding"), doc.get("status"))


async def test_imported_po_receipt_cannot_be_returned_once_a_bill(client, session, auth):
    """The bill books goods the order held when imported as not yet received, while the lot
    already carries them, so returning them on the bill would leave the books unsettled."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot)
    await _converted(client, session, auth, po)
    before = await _held(session, auth, po, lot)

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.return_imported_on_bill"
    sku = (await _state(session, auth, lot)).get("sku") or lot
    assert detail["message"] == (f"Cannot return 2 of {sku} on this bill: at most 0 of it was received here. "
                                 "Goods this bill already held when it was imported cannot be returned on it. "
                                 + _NEXT_REVERT)
    assert detail["params"]["next_step"]["message_key"] == "docs.imported_on_bill_next.revert"
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_imported_on_bill"
    assert await _held(session, auth, po, lot) == before


async def test_bill_returns_what_it_received_but_not_what_it_was_imported_with(client, session, auth):
    """An order imported with 4 of 10 received, then 3 more received on it, is a bill holding
    7 in the lot: the 3 it received go back, a return reaching into the imported 4 is refused
    whole, by line or by lot."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot, qty=10, received=4)
    r = await _post(client, auth, po, {"source_line_id": line_id, "item_id": lot, "quantity_received": 3,
                                       "receive_as": "stock"})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 13
    ordered = await _books(session, auth, *_BOOKS)
    await _converted(client, session, auth, po)
    before = await _held(session, auth, po, lot)

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 4})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_imported_on_bill"
    sku = (await _state(session, auth, lot)).get("sku") or lot
    params = r.json()["detail"]["params"]
    assert {k: params[k] for k in ("qty", "sku", "received")} == {"qty": "4", "sku": sku, "received": "3"}
    assert params["next_step"]["message_key"] == "docs.imported_on_bill_next.revert"
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 3
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2},
                                          {"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_imported_on_bill"
    assert await _held(session, auth, po, lot) == before

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 10
    # The 3 went back at what the receipt booked them for, settling the bill's 42 owing on them.
    books = await _books(session, auth, *_BOOKS)
    assert books["1130-OB"] == before[0]["1130-OB"] - 42.0
    assert books["2110"] == before[0]["2110"] + 42.0
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 1}]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_imported_on_bill"

    # Nothing received here is left on it, so it goes back to the order, holding the 4 it
    # was imported with, which go back from there. The order keeps its receipts: its books
    # are as before it became a bill, less the 3 that went back.
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 0
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    doc = await _state(session, auth, po)
    assert (doc["doc_type"], doc["status"]) == ("purchase_order", "partial_returned")
    assert await _books(session, auth, *_BOOKS) == {
        "1130-OB": ordered["1130-OB"] - 42.0, "1130-P": ordered["1130-P"], "2110": ordered["2110"] + 42.0}
    await _balanced(session, auth)
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 4
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 4})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 6
    assert (await _state(session, auth, po))["status"] == "returned"
    await _balanced(session, auth)


_NEXT_REVERT = ("To send them back, return anything received here first, then revert the bill to draft "
                "and return them from the purchase order.")
_NEXT_VOID = "To cancel the bill, void it. The goods stay in stock."
_NEXT_CANCEL = ("To cancel the bill, revert it to draft. The purchase order keeps its receipt and what went "
                "back from it.")


async def _balanced(session, auth) -> None:
    """The posted entries balance."""
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    assert round(sum(float(e.get("debit") or 0) - float(e.get("credit") or 0) for p in rows
                     if p.state.get("status") == "posted" for e in p.state.get("entries") or []), 6) == 0


async def _settled(session, auth) -> None:
    """The lot accounts hold what the lots carry, and the posted entries balance."""
    await assert_books_carry_stock(session, auth["company_id"])
    await _balanced(session, auth)


async def _owes(session, auth, doc_id: str) -> None:
    """Accounts payable holds what the document owes, and the posted entries balance."""
    doc = await _state(session, auth, doc_id)
    assert (await _books(session, auth, "2110"))["2110"] == pytest.approx(
        -float(doc.get("amount_outstanding") or 0)), doc.get("status")
    await _balanced(session, auth)


async def _je_status(session, auth, doc_id: str) -> dict[str, str]:
    """JE id suffix -> status, for the document's automatic entries."""
    session.expire_all()
    prefix = f"je:auto:{doc_id}:"
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
        Projection.entity_id.startswith(prefix)))).scalars().all()
    return {r.entity_id[len(prefix):]: r.state.get("status") for r in rows}


async def test_bill_from_an_imported_order_reverts_to_the_order_and_its_goods_go_back_there(client, session, auth):
    """Reverting gives back only the bill's own entry: the order keeps the receipt it was
    imported with, so its books are as they were before it became a bill, and the goods go
    back to the supplier from the order with accounts payable following what it owes."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot)
    ordered = await _books(session, auth, *_BOOKS)
    assert ordered == {"1130-OB": _OPENING, "1130-P": 70.0, "2110": -70.0}
    await _owes(session, auth, po)
    await _converted(client, session, auth, po)
    await _owes(session, auth, po)
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 0

    r = await client.delete(f"/docs/{po}/receive", headers=auth["headers"])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.undo_receipt_imported"
    assert detail["message"] == ("Goods this bill already held when it was imported have no receipt on it "
                                 "to undo. " + _NEXT_REVERT)

    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    doc = await _state(session, auth, po)
    assert (doc["doc_type"], doc["status"], doc["finalized"]) == ("purchase_order", "received", False)
    assert len(doc["received_items"]) == 1
    lot_state = await _state(session, auth, lot)
    assert (lot_state["quantity"], lot_state["cost_total"]) == (10, _OPENING)
    assert await _books(session, auth, *_BOOKS) == ordered
    assert (await _je_status(session, auth, po))["rcv"] == "posted"
    await _owes(session, auth, po)

    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 5
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 8
    await _owes(session, auth, po)
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 5
    await _owes(session, auth, po)

    # Made a bill again, it books nothing more: the order's receipt already carries it.
    await _converted(client, session, auth, po)
    await _owes(session, auth, po)
    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.void_imported_returned"
    await _owes(session, auth, po)
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, po))["doc_type"] == "purchase_order"
    await _owes(session, auth, po)


async def test_bill_from_an_imported_order_with_goods_sent_back_reverts_but_does_not_void(client, session, auth):
    """Goods the order was imported with went back to the supplier before it became a bill.
    Voiding the bill would take away the order's receipt and leave that return booked
    against nothing, so the bill goes back to the order instead, which keeps both."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot)
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    await _owes(session, auth, po)
    returned = await _books(session, auth, *_BOOKS)
    await _converted(client, session, auth, po)
    await _owes(session, auth, po)
    before = await _held(session, auth, po, lot)

    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.void_imported_returned"
    assert detail["message"] == (
        "This bill cannot be voided: goods it already held when it was imported went back to the supplier "
        "from the purchase order, and that return stays with the order's receipt. " + _NEXT_CANCEL)
    assert detail["params"]["next_step"]["message_key"] == "docs.imported_on_bill_next.cancel"
    assert await _held(session, auth, po, lot) == before

    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    doc = await _state(session, auth, po)
    assert (doc["doc_type"], doc["status"]) == ("purchase_order", "partial_returned")
    assert await _books(session, auth, *_BOOKS) == returned
    await _owes(session, auth, po)
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 5
    await _owes(session, auth, po)


async def test_bill_from_an_imported_order_voids_keeping_its_goods(client, session, auth):
    """Voiding the bill takes away the receipt it was imported with along with its own entry,
    and unvoiding puts both back, as often as it is done; going back to the order after an
    unvoid keeps the receipt the unvoid restored."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot)
    ordered = await _books(session, auth, *_BOOKS)
    for _ in range(2):
        await _converted(client, session, auth, po)
        billed = await _books(session, auth, *_BOOKS)
        await _owes(session, auth, po)
        for _ in range(2):
            r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
            assert r.status_code == 200, r.text
            doc = await _state(session, auth, po)
            assert (doc["status"], len(doc["received_items"])) == ("void", 1)
            assert await _books(session, auth, *_BOOKS) == {"1130-OB": _OPENING, "1130-P": 0.0, "2110": 0.0}
            assert await _qty(session, auth, lot) == 10
            await _settled(session, auth)
            await _owes(session, auth, po)

            r = await client.post(f"/docs/{po}/unvoid", headers=auth["headers"], json={})
            assert r.status_code == 200, r.text
            assert await _books(session, auth, *_BOOKS) == billed
            await _owes(session, auth, po)
        r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
        assert r.status_code == 200, r.text
        assert await _books(session, auth, *_BOOKS) == ordered
        await _owes(session, auth, po)
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 5})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 5
    await _owes(session, auth, po)


async def _as_legacy_receipt(session, auth, doc_id: str) -> None:
    """Store the document's one receipt entry as receipts were stored before they carried
    a key of their own: at the bare je:auto:{doc}:rcv."""
    from sqlalchemy import update

    [suffix] = [s for s in await _je_status(session, auth, doc_id) if s.startswith("rcv:")]
    old, new = f"je:auto:{doc_id}:{suffix}", f"je:auto:{doc_id}:rcv"
    for model in (Projection, LedgerEntry):
        await session.execute(update(model).where(
            model.company_id == auth["company_id"], model.entity_id == old).values(entity_id=new))
    await session.commit()


@pytest.mark.parametrize("legacy", [False, True])
async def test_receipt_stored_the_earlier_way_stays_with_the_bill_through_void_and_revert(
        client, session, auth, legacy):
    """An order's real receipt stored at the bare receipt entry, as receipts once were, is
    no bill entry: once the receipt is undone, voiding, unvoiding and reverting the bill
    treat it exactly as they treat a receipt stored today."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, _ = await _ordered_and_received(
        client, session, auth, [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    if legacy:
        await _as_legacy_receipt(session, auth, po)
        assert await _je_status(session, auth, po) == {"rcv": "posted"}

    def receipt(statuses: dict[str, str]) -> list[str]:
        return [v for k, v in sorted(statuses.items()) if k == "rcv" or k.startswith("rcv:")]

    await _owes(session, auth, po)
    await _converted(client, session, auth, po)
    await _owes(session, auth, po)
    r = await client.delete(f"/docs/{po}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    await _owes(session, auth, po)
    held = receipt(await _je_status(session, auth, po))
    assert held == ["posted", "posted"]
    for step in ("void", "unvoid", "void", "unvoid"):
        r = await client.post(f"/docs/{po}/{step}", headers=auth["headers"], json={})
        assert r.status_code == 200, (step, r.text)
        assert receipt(await _je_status(session, auth, po)) == held, step
    # Back to draft, the receipt and its undo reverse together, as the goods movements of a
    # document holding no goods do.
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert receipt(await _je_status(session, auth, po)) == ["void", "void"]


async def _imported_bill(client, auth, lot: str) -> tuple[str, str]:
    """A bill for 5 imported with all 5 already received onto ``lot``. -> (doc id, line id)."""
    doc_id, line_id = f"doc:{uuid.uuid4()}", str(uuid.uuid4())
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": doc_id, "event_type": "doc.created", "source": "test",
        "idempotency_key": uuid.uuid4().hex, "data": {
            "doc_type": "bill", "status": "awaiting_payment", "doc_number": f"B-IMP-{uuid.uuid4().hex[:5]}",
            "line_items": [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0, "line_id": line_id}],
            "subtotal": 70.0, "total": 70.0, "amount_outstanding": 70.0,
            "received_items": [{"item_id": lot, "po_line_index": 0, "quantity_received": 5.0,
                                "receive_as": "stock"}],
            "received_item_ids": []}})
    assert r.status_code == 200, r.text
    return doc_id, line_id


async def test_imported_bill_names_void_as_the_way_out(client, session, auth):
    """A bill imported holding its goods has no order to go back to: it cannot return them or
    revert, and says to void it, which leaves the goods in stock and can be undone."""
    lot = await _item(client, auth, _OPENING, qty=10)
    bill, line_id = await _imported_bill(client, auth, lot)
    sku = (await _state(session, auth, lot)).get("sku") or lot
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 0

    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message"] == (
        f"Cannot return 2 of {sku} on this bill: at most 0 of it was received here. "
        "Goods this bill already held when it was imported cannot be returned on it. " + _NEXT_VOID)
    r = await client.post(f"/docs/{bill}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.revert_imported_bill"
    assert detail["message"] == ("This bill cannot go back to draft: it already held goods when it was "
                                 "imported, and a draft bill holds none. " + _NEXT_VOID)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message"] == ("Goods this bill already held when it was imported have no "
                                             "receipt on it to undo. " + _NEXT_VOID)

    r = await client.post(f"/docs/{bill}/void", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert await _books(session, auth, *_BOOKS) == {"1130-OB": _OPENING, "1130-P": 0.0, "2110": 0.0}
    lot_state = await _state(session, auth, lot)
    assert (lot_state["quantity"], lot_state["cost_total"]) == (10, _OPENING)
    await _settled(session, auth)
    r = await client.post(f"/docs/{bill}/unvoid", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, bill))["status"] == "awaiting_payment"


async def test_bill_holding_goods_it_received_still_reverts_and_voids_only_once_they_are_back(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po, _ = await _ordered_and_received(
        client, session, auth, [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}])
    await _converted(client, session, auth, po)
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == ("Cannot revert to draft while goods received on this document are still "
                                  "in stock. Select those lines and use Return Goods first, then revert.")
    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Cannot void a document with received items; return the goods first"


async def test_bill_that_received_goods_itself_beside_imported_ones_names_no_way_out(client, session, auth):
    """Goods received while it is a bill were booked against the bill, so reverting it would
    leave their receipt and return unsettled: the refusal says the imported goods stay."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot, qty=10, received=4)
    await _converted(client, session, auth, po)
    r = await _post(client, auth, po, {"source_line_id": line_id, "item_id": lot, "quantity_received": 3,
                                       "receive_as": "stock"})
    assert r.status_code == 200, r.text
    # A bill receives into a parcel of its own: the 3 the line offers back are in that
    # parcel, not in the lot the line names, so by lot nothing received here can go back,
    # and by line or by parcel the 3 the line offers do.
    [parcel] = (await _state(session, auth, po))["received_item_ids"]
    assert await _qty(session, auth, parcel) == 3
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 3
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 3}]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["params"]["received"] == "0"
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": parcel, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, parcel))["status"] == "disposed"
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 0
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 1})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["params"]["next_step"] == {
        "message": "They stay in stock on this bill.", "message_key": "docs.imported_on_bill_next.none",
        "params": {}}
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message"] == (
        "This bill cannot go back to draft: it already held goods when it was imported, and a draft bill "
        "holds none. They stay in stock on this bill.")
    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message"] == (
        "This bill cannot be voided: goods it received itself were booked against it, beside goods it "
        "already held when it was imported. They stay in stock on this bill.")

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
from test_imported_document_cutover import _opening
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


async def _imported_received_po(client, auth, lot: str, qty: float = 5, received: float = 5,
                                treatment: str = "opening_balances") -> tuple[str, str]:
    """A purchase order for ``qty`` imported with ``received`` of it already received onto
    ``lot``. Under opening_balances the lot already holds those goods, the import posts
    nothing and the receipt is part of the document as imported, with no receipt event of
    its own; under record_now the receipt brings them in now. -> (doc id, line id)."""
    doc_id, line_id = f"doc:{uuid.uuid4()}", str(uuid.uuid4())
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": doc_id, "event_type": "doc.created", "source": "test",
        "idempotency_key": uuid.uuid4().hex, "data": {
            "doc_type": "purchase_order", "status": "received" if received >= qty else "partially_received",
            "doc_number": f"PO-IMP-{uuid.uuid4().hex[:5]}", "import_treatment": treatment,
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


@pytest.mark.parametrize("treatment", ["opening_balances", "record_now"])
async def test_imported_po_receipt_goes_back_on_the_bill_and_the_bill_owes_less(client, session, auth, treatment):
    """Goods an order held when it was imported are real stock. Once the order is a bill
    they go back to the supplier at the value they were imported at, by line and by lot,
    and the credit comes off what the bill owes."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot, treatment=treatment)
    await _converted(client, session, auth, po)
    lot_before = await _state(session, auth, lot)
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 5

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    lot_state = await _state(session, auth, lot)
    assert (lot_state["quantity"], lot_state["cost_total"]) == (
        lot_before["quantity"] - 3, pytest.approx(lot_before["cost_total"] - 42.0))
    assert (await _state(session, auth, po))["amount_outstanding"] == pytest.approx(28.0)
    await _settled(session, auth)
    if treatment == "record_now":
        await _owes(session, auth, po)


async def test_bill_returns_what_it_received_and_what_it_was_imported_with(client, session, auth):
    """An order for 7 imported with 4 received, then the other 3 received on it, is a bill
    holding 7 in the lot: all 7 go back, by line or by lot, and no more. What the bill owes
    falls by each return. Once nothing is held it still does not go back to the order: the
    goods it was imported with are opening stock, which no revert takes back."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot, qty=7, received=4)
    r = await _post(client, auth, po, {"source_line_id": line_id, "item_id": lot, "quantity_received": 3,
                                       "receive_as": "stock"})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 13
    await _converted(client, session, auth, po)
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 7

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 4})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 200, r.text
    before = await _held(session, auth, po, lot)
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 2}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "docs.return_lot_more_than_received"
    assert await _held(session, auth, po, lot) == before
    r = await client.post(f"/docs/{po}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 1}]})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 6
    assert (await _state(session, auth, po))["amount_outstanding"] == pytest.approx(0.0)
    await _settled(session, auth)
    before = await _held(session, auth, po, lot)

    assert (await _state(session, auth, po))["status"] == "returned"
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.revert_imported_receipt"
    assert await _held(session, auth, po, lot) == before
    assert (await _state(session, auth, po))["doc_type"] == "bill"


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


async def test_bill_from_an_order_imported_and_received_now_goes_back_to_the_order(client, session, auth):
    """An order imported with its goods recorded now holds them through a receipt of its
    own, so as a bill it behaves as any bill made from a received order: it keeps the goods
    until they go back, then reverts to the order, and accounts payable follows what it owes
    at every step."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot, treatment="record_now")
    ordered = await _books(session, auth, *_BOOKS)
    assert ordered == {"1130-OB": _OPENING + 70.0, "1130-P": 0.0, "2110": -70.0}
    await _owes(session, auth, po)
    await _converted(client, session, auth, po)
    await _owes(session, auth, po)
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 13
    await _owes(session, auth, po)
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 10
    await _owes(session, auth, po)

    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, po))["doc_type"] == "purchase_order"
    await _owes(session, auth, po)
    await _settled(session, auth)


async def test_bill_from_an_imported_order_with_goods_sent_back_keeps_both_and_is_not_voided(
        client, session, auth):
    """Goods the order was imported with went back to the supplier before it became a bill.
    The bill holds the rest, which are opening stock: it is not voided while they are in
    stock, nor once they have gone back too, and the books keep holding what the lots carry."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot)
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    await _converted(client, session, auth, po)
    before = await _held(session, auth, po, lot)

    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.void_imported_receipt"
    assert await _held(session, auth, po, lot) == before

    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 5
    assert (await _state(session, auth, po))["amount_outstanding"] == pytest.approx(0.0)
    await _settled(session, auth)
    empty = await _held(session, auth, po, lot)
    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.void_imported_receipt"
    assert await _held(session, auth, po, lot) == empty
    await _settled(session, auth)


async def test_bill_from_an_imported_order_is_not_voided_once_its_goods_are_back(client, session, auth):
    """A bill made from an order imported holding its goods sends them back, and the goods
    stay off the lot. Those goods are opening stock, so the bill is not voided or reverted
    before or after they go back, and a refusal leaves the books and the lot as they were."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot)
    await _converted(client, session, auth, po)
    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.void_imported_receipt"
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 5})
    assert r.status_code == 200, r.text
    returned = await _held(session, auth, po, lot)
    for step, key in (("void", "docs.void_imported_receipt"), ("revert-to-draft", "docs.revert_imported_receipt")):
        r = await client.post(f"/docs/{po}/{step}", headers=auth["headers"], json={})
        assert r.status_code == 409, (step, r.text)
        assert r.json()["detail"]["message_key"] == key
        assert await _held(session, auth, po, lot) == returned
        assert await _qty(session, auth, lot) == 5
        await _settled(session, auth)


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
    no bill entry: once the receipt is undone, voiding the bill is refused and reverting it
    reverses the receipt and its undo together, exactly as for a receipt stored today."""
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
    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.void_order_receipt"
    assert receipt(await _je_status(session, auth, po)) == held
    await _owes(session, auth, po)
    # Back to draft, the receipt and its undo reverse together, as the goods movements of a
    # document holding no goods do.
    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert receipt(await _je_status(session, auth, po)) == ["void", "void"]
    assert (await _books(session, auth, "2110"))["2110"] == 0
    await _settled(session, auth)


@pytest.mark.parametrize("received", [5, 3])
async def test_bill_whose_order_receipt_was_undone_goes_back_to_the_order_rather_than_void(
        client, session, auth, received):
    """Undoing the receipt leaves the order's receipt entry and its undo on accounts payable,
    and voiding reverses only the bill's own entries, so a void would leave payables
    standing against a bill that owes nothing. Voiding is refused; reverting to the order
    reverses all of it."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, _ = await _ordered_and_received(
        client, session, auth, [{"item_id": lot, "name": "Lot", "quantity": 5, "unit_price": 14.0}], received)
    await _converted(client, session, auth, po)
    await _owes(session, auth, po)
    r = await client.delete(f"/docs/{po}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 10
    await _owes(session, auth, po)
    before = await _books(session, auth, *_BOOKS)

    r = await client.post(f"/docs/{po}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.void_order_receipt"
    assert detail["message"] == ("This bill cannot be voided: the purchase order it was made from booked its "
                                 "receipt, and voiding the bill would leave that entry standing. To cancel the "
                                 "bill, revert it to draft.")
    assert await _books(session, auth, *_BOOKS) == before
    await _owes(session, auth, po)

    r = await client.post(f"/docs/{po}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, po))["doc_type"] == "purchase_order"
    assert await _books(session, auth, *_BOOKS) == {"1130-OB": _OPENING, "1130-P": 0.0, "2110": 0.0}
    await _settled(session, auth)


async def test_receipts_cannot_be_written_by_an_edit(client, session, auth):
    """What a document received is written only by receiving and its undo, never by editing
    the document, even while it is a draft."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _doc(client, auth, "purchase_order", [{"item_id": lot, "name": "Lot", "quantity": 5,
                                                      "unit_price": 14.0}])
    forged = [{"item_id": lot, "po_line_index": 0, "quantity_received": 5.0, "receive_as": "stock"}]
    for field, value in (("received_items", forged), ("received_item_ids", [lot])):
        r = await client.patch(f"/docs/{po}", headers=auth["headers"],
                               json={"fields_changed": {field: {"old": None, "new": value}}})
        assert r.status_code == 422, (field, r.text)
        assert field in r.json()["detail"]
    doc = await _state(session, auth, po)
    assert (doc["status"], doc.get("received_items"), doc.get("received_item_ids")) == ("draft", None, None)


async def _imported_bill(client, auth, lot: str, qty: float = 5, received: float = 5) -> tuple[str, str]:
    """A bill for ``qty`` imported with ``received`` of it already received onto ``lot``, the
    opening balances holding it. -> (doc id, line id)."""
    doc_id, line_id = f"doc:{uuid.uuid4()}", str(uuid.uuid4())
    r = await client.post("/docs/import", headers=auth["headers"], json={
        "entity_id": doc_id, "event_type": "doc.created", "source": "test",
        "idempotency_key": uuid.uuid4().hex, "data": {
            "doc_type": "bill", "status": "awaiting_payment", "doc_number": f"B-IMP-{uuid.uuid4().hex[:5]}",
            "import_treatment": "opening_balances",
            "line_items": [{"item_id": lot, "name": "Lot", "quantity": qty, "unit_price": 14.0, "line_id": line_id}],
            "subtotal": 14.0 * qty, "total": 14.0 * qty, "amount_outstanding": 14.0 * qty,
            "received_items": [{"item_id": lot, "po_line_index": 0, "quantity_received": float(received),
                                "receive_as": "stock"}],
            "received_item_ids": []}})
    assert r.status_code == 200, r.text
    return doc_id, line_id


async def test_imported_bill_sends_its_goods_back_and_is_not_voided(client, session, auth):
    """A bill imported holding its goods sends them back at their imported value, by line or
    by lot, owing less for each. Their receipt was never made here, so it cannot be undone,
    and the bill neither reverts nor voids, while it holds them or once they are back."""
    lot = await _item(client, auth, _OPENING, qty=10)
    bill, line_id = await _imported_bill(client, auth, lot)
    doc = (await client.get(f"/docs/{bill}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 5

    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.undo_imported_receipt"
    r = await client.post(f"/docs/{bill}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.revert_imported_receipt"
    r = await client.post(f"/docs/{bill}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.void_imported_receipt"

    r = await _return_lines(client, auth, bill, {"line_id": line_id, "quantity_returned": 2})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, bill))["amount_outstanding"] == pytest.approx(42.0)
    r = await client.post(f"/docs/{bill}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": lot, "quantity_returned": 3}]})
    assert r.status_code == 200, r.text
    lot_state = await _state(session, auth, lot)
    assert (lot_state["quantity"], lot_state["cost_total"]) == (5, pytest.approx(_OPENING - 70.0))
    await _settled(session, auth)

    returned = await _held(session, auth, bill, lot)
    for step, key in (("void", "docs.void_imported_receipt"), ("revert-to-draft", "docs.revert_imported_receipt")):
        r = await client.post(f"/docs/{bill}/{step}", headers=auth["headers"], json={})
        assert r.status_code == 409, (step, r.text)
        assert r.json()["detail"]["message_key"] == key
        assert await _held(session, auth, bill, lot) == returned
    await _settled(session, auth)


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


async def _receive_on(client, auth, doc_id: str, line_id: str, lot: str, qty: float):
    return await _post(client, auth, doc_id, {"source_line_id": line_id, "item_id": lot,
                                              "quantity_received": qty, "receive_as": "stock"})


async def _receipts(session, auth, doc_id: str) -> tuple:
    doc = await _state(session, auth, doc_id)
    return doc.get("received_items"), doc.get("received_item_ids")


async def test_bill_from_an_imported_order_receives_the_rest_on_the_bill(client, session, auth):
    """A bill made from an order for 7 imported with 4 received takes the other 3 on the bill
    itself, as a new parcel beside the goods it was imported with (a bill's receipt makes
    parcels), and sends any of them back, owing less for each."""
    lot = await _item(client, auth, _OPENING, qty=10)
    po, line_id = await _imported_received_po(client, auth, lot, qty=7, received=4)
    await _converted(client, session, auth, po)
    r = await _receive_on(client, auth, po, line_id, lot, 3)
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, po))["received_item_ids"]
    assert (await _qty(session, auth, lot), await _qty(session, auth, parcel)) == (10, 3)
    await _settled(session, auth)
    doc = (await client.get(f"/docs/{po}", headers=auth["headers"])).json()
    assert doc["line_items"][0]["returnable_quantity"] == 7
    r = await _return_lines(client, auth, po, {"line_id": line_id, "quantity_returned": 3})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) + await _qty(session, auth, parcel) == 10
    assert (await _state(session, auth, po))["amount_outstanding"] == pytest.approx(98.0 - 42.0)
    await _settled(session, auth)


@pytest.mark.parametrize("historical", [False, True])
async def test_imported_bill_receives_the_rest(client, session, auth, historical):
    """A bill imported with 3 of 5 received takes the other 2 on a receipt, or on a receipt
    recorded from the books it came from, beside the 3 it was imported with. The opening
    balances hold the bill, so neither receipt posts an entry: the live one brings in a parcel
    at the bill's price, the recorded one moves no stock."""
    from celerp_docs.routes import record_historical_receipt

    lot = await _item(client, auth, _OPENING, qty=10)
    bill, line_id = await _imported_bill(client, auth, lot, received=3)
    # The opening balances hold the bill, and the 2 not received yet as goods billed in
    # transit unless they came in before the books came here.
    await _opening(client, auth, 70.0, in_transit=0.0 if historical else 28.0)
    books = await _books(session, auth, *_BOOKS)
    if historical:
        await record_historical_receipt(
            session, auth["company_id"], bill, lines=[{"line": 0, "item_id": lot, "quantity": 2, "cost": 28}],
            received_on="2025-01-02", actor_id=auth["user_id"], source="migration",
            idempotency_key=f"m:{bill}:received")
        await session.commit()
    else:
        r = await _receive_on(client, auth, bill, line_id, lot, 2)
        assert r.status_code == 200, r.text
    received, made = await _receipts(session, auth, bill)
    assert [float(x["quantity_received"]) for x in received] == [3.0, 2.0]
    assert await _books(session, auth, *_BOOKS) == books
    assert await _qty(session, auth, lot) == 10
    if not historical:
        [parcel] = made
        assert (await _state(session, auth, parcel))["cost_total"] == 28.0
    await _settled(session, auth)


@pytest.mark.parametrize("from_order", [True, False])
async def test_bill_holding_imported_goods_and_its_own_sends_both_back_and_is_not_voided(
        client, session, auth, from_order):
    """A bill for 7 holding 4 goods it was imported with beside 3 it received itself sends
    both back. The 4 are opening stock, so it is not voided while it holds any, nor once all
    are back, and the books keep holding what the lots carry."""
    lot = await _item(client, auth, _OPENING, qty=10)
    if from_order:
        doc_id, line_id = await _imported_received_po(client, auth, lot, qty=7, received=4)
        await _converted(client, session, auth, doc_id)
    else:
        doc_id, line_id = await _imported_bill(client, auth, lot, qty=7, received=4)
        await _opening(client, auth, 98.0, in_transit=42.0)
    r = await _receive_on(client, auth, doc_id, line_id, lot, 3)
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, doc_id))["received_item_ids"]
    before = await _held(session, auth, doc_id, lot)
    r = await client.post(f"/docs/{doc_id}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.void_imported_receipt"
    assert await _held(session, auth, doc_id, lot) == before

    r = await client.post(f"/docs/{doc_id}/return-items", headers=auth["headers"],
                          json={"items": [{"item_id": parcel, "quantity_returned": 3}]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc_id}/void", headers=auth["headers"], json={})
    assert r.status_code == 409, r.text
    r = await _return_lines(client, auth, doc_id, {"line_id": line_id, "quantity_returned": 4})
    assert r.status_code == 200, r.text
    assert await _qty(session, auth, lot) == 6
    assert (await _state(session, auth, parcel))["status"] == "disposed"
    await _settled(session, auth)
    returned = await _held(session, auth, doc_id, lot)
    for step, key in (("void", "docs.void_imported_receipt"), ("revert-to-draft", "docs.revert_imported_receipt")):
        r = await client.post(f"/docs/{doc_id}/{step}", headers=auth["headers"], json={})
        assert r.status_code == 409, (step, r.text)
        assert r.json()["detail"]["message_key"] == key
        assert await _held(session, auth, doc_id, lot) == returned

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A receipt is undone only while what it brought in is still as it left it. Once a parcel
has been split, partly sold or reserved, undoing would leave that part outside the receipt
and let the document be received in full again, so the undo is refused and says why."""
from __future__ import annotations

import re
import uuid

import pytest

from celerp_docs.routes import _parcel_moved_on
from test_cost_restatement import _item, _set_cost, _state
from test_landed_cost_pools import _ORDERS, _goods, _po_into
from test_helpers import sell_item
from test_invoice_unsellable_lot import _doc as _lot_doc
from test_landed_cost_removals import _split
from test_landed_cost_structural import _undo
from test_receipt_accounting import _doc, _finalize, _receive


async def _received_parcel(client, session, auth) -> tuple[str, str]:
    h = auth["headers"]
    line = {"sku": "UNDO-G", "name": "Goods", "quantity": 4, "unit_price": 10.0, "line_total": 40.0,
            "receive_as": "stock"}
    bill = await _doc(client, auth, "bill", [line], total=40.0)
    assert (await client.post(f"/docs/{bill}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=h, json={"location_id": "", "received_items": [
        {"po_line_index": 0, "sku": "UNDO-G", "name": "Goods", "quantity_received": 4, "receive_as": "stock"}]})
    assert r.status_code == 200, r.text
    [parcel] = (await _state(session, auth, bill))["received_item_ids"]
    return bill, parcel


async def _sell_one(client, auth, lot) -> None:
    h = auth["headers"]
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "total": 25.0, "line_items": [
        {"entity_id": lot, "sku": "UNDO-G", "name": "Goods", "quantity": 1, "unit_price": 25.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    assert (await client.post(f"/docs/{inv}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=h, json={"line_entity_ids": [lot]})
    assert r.status_code == 200, r.text


async def _split_one(client, auth, lot) -> None:
    r = await client.post(f"/items/{lot}/split", headers=auth["headers"], json={"children": [{"quantity": 1}]})
    assert r.status_code == 200, r.text


async def _reserve_one(client, auth, lot) -> None:
    r = await client.post(f"/items/{lot}/reserve", headers=auth["headers"], json={"quantity": 1})
    assert r.status_code == 200, r.text


def _assert_split_refusal(r, parcel_sku: str) -> None:
    """The one answer when a received parcel was split: keyed, names the SKU, and points to
    Return to supplier, where the goods (all on hand in the split lots) go back at cost."""
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == "docs.undo_receipt_split", detail
    assert detail["params"] == {"sku": parcel_sku}, detail
    assert parcel_sku in detail["message"] and "Return to supplier" in detail["message"], detail
    assert "cannot archive" not in detail["message"] and "manually correct" not in detail["message"], detail


@pytest.mark.asyncio
async def test_a_receipt_whose_parcel_was_partly_split_points_to_return_to_supplier(client, session, auth):
    bill, parcel = await _received_parcel(client, session, auth)
    await _split_one(client, auth, parcel)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    _assert_split_refusal(r, "UNDO-G")
    assert (await _state(session, auth, bill))["received_item_ids"] == [parcel]


@_ORDERS
@pytest.mark.asyncio
async def test_a_receipt_whose_parcel_was_split_whole_points_to_return_to_supplier(client, session, auth, from_order):
    """Splitting all of a parcel archives it. The goods are on hand in the split lots, so the
    refusal must not call the parent 'archived - cannot archive': the way back is a return."""
    goods = _goods(14.0, 5)
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [goods], shipping=7.0)
    if not from_order:
        await _finalize(client, auth, doc)
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": goods["sku"], "name": goods["name"],
                                           "quantity_received": 5})
    assert r.status_code == 200, r.text
    [lot] = (await _state(session, auth, doc))["received_item_ids"]
    if from_order:
        await _finalize(client, auth, doc)
    await _split(client, auth, lot, 2, 3)
    r = await _undo(client, auth, doc)
    _assert_split_refusal(r, goods["sku"])
    assert (await _state(session, auth, doc))["received_item_ids"] == [lot]


async def _topped_up_lot(client, session, auth) -> tuple[str, str, str]:
    """(bill, lot, sku): a purchase order received into a lot already on hand."""
    lot = await _item(client, auth, 100.0, qty=10)
    doc = await _po_into(client, auth, lot, 5, 14.0, shipping=7.0)
    return doc, lot, (await _state(session, auth, lot))["sku"]


@pytest.mark.asyncio
async def test_a_receipt_into_a_lot_whose_cost_was_restated_says_how_to_go_back(client, session, auth):
    """The lot's goods cost was set below what the receipt added, so the receipt cannot be
    taken back off it. The refusal says the cost changed and names both ways back: set the
    cost back, or Return to supplier."""
    doc, lot, sku = await _topped_up_lot(client, session, auth)
    assert (await _set_cost(client, auth, lot, 5.0)).status_code == 200
    r = await _undo(client, auth, doc)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == "docs.undo_receipt_cost_changed", detail
    assert detail["params"] == {"sku": sku}, detail
    assert sku in detail["message"] and "Return to supplier" in detail["message"], detail
    assert "cost" in detail["message"] and "manually correct" not in detail["message"], detail
    assert float((await _state(session, auth, lot))["quantity"]) == 15.0


@pytest.mark.asyncio
async def test_a_receipt_into_a_lot_whose_cost_is_unchanged_is_undone(client, session, auth):
    doc, lot, _ = await _topped_up_lot(client, session, auth)
    r = await _undo(client, auth, doc)
    assert r.status_code == 200, r.text
    assert float((await _state(session, auth, lot))["quantity"]) == 10.0


_REASON_KEYS = ("docs.undo_lot_missing", "docs.undo_lot_sold", "docs.undo_lot_on_memo", "docs.undo_lot_reserved_on_doc",
                "docs.undo_lot_not_available", "docs.undo_lot_holds_fewer", "docs.undo_receipt_lot_holds_more",
                "docs.undo_return_lot_holds_more",
                "docs.undo_lot_reserved", "docs.undo_lot_short")
_WRAPPER_KEYS = ("docs.undo_receipt_blocked", "docs.undo_return_blocked")
_NEW_KEYS = ("docs.undo_receipt_split", "docs.undo_receipt_cost_changed", "item.deleted",
             *_REASON_KEYS, *_WRAPPER_KEYS)


@pytest.mark.parametrize("key", _NEW_KEYS)
def test_undo_receipt_and_item_deleted_copy_is_translated_in_every_locale(key):
    """Read from each catalog file, so a missing key or an English placeholder cannot hide
    behind the fallback to English."""
    import json
    from pathlib import Path

    catalogs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (Path(__file__).resolve().parent.parent / "ui" / "locales").glob("*.json")}
    english = catalogs.pop("en")[key]
    assert len(catalogs) == 11
    placeholders = set(re.findall(r"\{[a-z_]+\}", english))
    for code, catalog in catalogs.items():
        assert catalog.get(key), (key, code)
        assert catalog[key] != english, (key, code)
        assert placeholders <= set(re.findall(r"\{[a-z_]+\}", catalog[key])), (key, code)


@pytest.mark.asyncio
async def test_a_receipt_whose_parcel_was_partly_sold_cannot_be_undone(client, session, auth):
    """A unit sold from the parcel took its cost with it, so the receipt cannot be taken
    back as if it never happened: the one refusal for units gone, pointing to a return."""
    bill, parcel = await _received_parcel(client, session, auth)
    await _sell_one(client, auth, parcel)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "docs.undo_receipt_units_sold", detail
    assert detail["params"] == {"sku": "UNDO-G", "gone": "1", "needed": "4"}, detail
    assert (await _state(session, auth, bill))["received_item_ids"] == [parcel]


@pytest.mark.asyncio
async def test_a_receipt_left_as_it_came_in_is_undone(client, session, auth):
    bill, parcel = await _received_parcel(client, session, auth)
    r = await client.delete(f"/docs/{bill}/receive", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, parcel))["status"] == "archived"


# --- the reasons a parcel blocks an undo: one key each, on both paths that reach it ----------


class _Parcel:
    """A parcel one document brought in and the undo that takes it back: the receipt undo on a
    bill, or the credit-note undo on a return, which share the one parcel check."""

    def __init__(self, path: str, doc: str, lot: str, sku: str, qty: float):
        self.wrapper = "docs.undo_receipt_blocked" if path == "receipt" else "docs.undo_return_blocked"
        self.doc, self.lot, self.sku, self.qty = doc, lot, sku, qty
        self.route = f"/docs/{doc}/" + ("receive" if path == "receipt" else "receive-return")

    async def undo(self, client, auth):
        return await client.delete(self.route, headers=auth["headers"])


async def _parcel(path: str, client, session, auth) -> _Parcel:
    if path == "receipt":
        bill, lot = await _received_parcel(client, session, auth)
        return _Parcel(path, bill, lot, "UNDO-G", 4)
    h = auth["headers"]
    sku = f"RR-{uuid.uuid4().hex[:6]}"
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": sku, "name": "Widget", "quantity": 2, "cost_price": 40.0, "sell_by": "piece"})
    assert r.status_code == 200, r.text
    inv = await sell_item(client, h, r.json()["id"], unit_price=50.0)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "credit_note", "original_doc_id": inv, "total": 100.0,
        "line_items": [{"name": "Widget", "sku": sku, "quantity": 2, "unit_price": 50.0}]})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    assert (await client.post(f"/docs/{cn}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{cn}/receive-return", headers=h, json={"items": [{"sku": sku, "quantity": 2}]})
    assert r.status_code == 200, r.text
    [lot] = [x["item_id"] for x in (await _state(session, auth, cn))["return_received_items"]]
    return _Parcel(path, cn, lot, sku, 2)


_PATHS = pytest.mark.parametrize("path", ["receipt", "return"])


def _assert_blocked(r, p: _Parcel, *reasons: tuple[str, dict]) -> None:
    """One keyed wrapper, whose nested reasons are each keyed with their own params, so the UI
    names every SKU and its reason in the user's language. No advice to fix the ledger by hand."""
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == p.wrapper, detail
    got = [(x["message_key"], x["params"]) for x in detail["params"]["reasons"]]
    assert got == list(reasons), got
    for x in detail["params"]["reasons"]:
        assert x["params"]["sku"] in x["message"] and x["params"]["sku"] in detail["message"], x
    for text in (detail["message"], *(x["message"] for x in detail["params"]["reasons"])):
        assert "manually correct" not in text and "cannot archive" not in text, text


@_PATHS
@pytest.mark.asyncio
async def test_a_parcel_with_units_reserved_says_to_release_the_reservation(client, session, auth, path):
    p = await _parcel(path, client, session, auth)
    await _reserve_one(client, auth, p.lot)
    _assert_blocked(await p.undo(client, auth), p, ("docs.undo_lot_reserved", {"sku": p.sku, "reserved": "1"}))
    r = await client.post(f"/items/{p.lot}/unreserve", headers=auth["headers"], json={"quantity": 1})
    assert r.status_code == 200, r.text
    assert (await p.undo(client, auth)).status_code == 200


@_PATHS
@pytest.mark.asyncio
async def test_a_parcel_set_aside_says_which_status_it_is_in_and_how_to_set_it_available(client, session, auth, path):
    p = await _parcel(path, client, session, auth)
    h = auth["headers"]
    assert (await client.post(f"/items/{p.lot}/status", headers=h, json={"new_status": "archived"})).status_code == 200
    _assert_blocked(await p.undo(client, auth), p,
                    ("docs.undo_lot_not_available", {"sku": p.sku, "lot_status": "archived"}))
    assert (await client.post(f"/items/{p.lot}/status", headers=h, json={"new_status": "available"})).status_code == 200
    assert (await p.undo(client, auth)).status_code == 200


@_PATHS
@pytest.mark.asyncio
async def test_a_parcel_out_on_a_memo_says_to_take_it_back_on_the_memo(client, session, auth, path):
    p = await _parcel(path, client, session, auth)
    h = auth["headers"]
    memo = await _lot_doc(client, auth, "memo", p.lot, p.sku, p.qty)
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{memo}/fulfill-lines", headers=h, json={"line_entity_ids": [p.lot]})
    assert r.status_code == 200, r.text
    _assert_blocked(await p.undo(client, auth), p, ("docs.undo_lot_on_memo", {"sku": p.sku}))
    r = await client.post(f"/docs/{memo}/set-available", headers=h, json={"line_entity_ids": [p.lot]})
    assert r.status_code == 200, r.text
    assert (await p.undo(client, auth)).status_code == 200


@_PATHS
@pytest.mark.asyncio
async def test_a_parcel_reserved_on_a_document_says_to_release_it_there(client, session, auth, path):
    p = await _parcel(path, client, session, auth)
    h = auth["headers"]
    memo = await _lot_doc(client, auth, "memo", p.lot, p.sku, p.qty)
    assert (await client.post(f"/docs/{memo}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{memo}/reserve-lines", headers=h,
                          json={"new_status": "reserved", "line_entity_ids": [p.lot]})
    assert r.status_code == 200, r.text
    _assert_blocked(await p.undo(client, auth), p, ("docs.undo_lot_reserved_on_doc", {"sku": p.sku}))
    r = await client.post(f"/docs/{memo}/set-available", headers=h, json={"line_entity_ids": [p.lot]})
    assert r.status_code == 200, r.text
    assert (await p.undo(client, auth)).status_code == 200


@pytest.mark.asyncio
async def test_a_received_parcel_adjusted_up_says_to_use_return_to_supplier(client, session, auth):
    """A count taken off again would itself count as units gone, so the way back is a return of
    what came in, which goes through with the extra units still on hand."""
    p = await _parcel("receipt", client, session, auth)
    h = auth["headers"]
    assert (await client.post(f"/items/{p.lot}/adjust", headers=h, json={"new_qty": 6})).status_code == 200
    r = await p.undo(client, auth)
    _assert_blocked(r, p, ("docs.undo_receipt_lot_holds_more", {"sku": p.sku, "held": "6", "came_in": "4"}))
    assert "Return to supplier" in r.json()["detail"]["params"]["reasons"][0]["message"]
    r = await client.post(f"/docs/{p.doc}/return-items", headers=h,
                          json={"items": [{"item_id": p.lot, "quantity_returned": 4}]})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_returned_parcel_adjusted_up_says_to_set_the_count_back(client, session, auth):
    p = await _parcel("return", client, session, auth)
    h = auth["headers"]
    assert (await client.post(f"/items/{p.lot}/adjust", headers=h, json={"new_qty": 4})).status_code == 200
    _assert_blocked(await p.undo(client, auth), p,
                    ("docs.undo_return_lot_holds_more", {"sku": p.sku, "held": "4", "came_in": "2"}))
    assert (await client.post(f"/items/{p.lot}/adjust", headers=h, json={"new_qty": 2})).status_code == 200
    assert (await p.undo(client, auth)).status_code == 200


@pytest.mark.asyncio
async def test_a_returned_parcel_that_lost_units_to_a_split_says_to_undo_that_first(client, session, auth):
    """Only the credit-note undo reaches this: a receipt whose parcel lost units is refused
    earlier, by the units-sold and split answers, which point to Return to supplier."""
    p = await _parcel("return", client, session, auth)
    await _split_one(client, auth, p.lot)
    _assert_blocked(await p.undo(client, auth), p, ("docs.undo_lot_holds_fewer", {
        "sku": p.sku, "held": "1", "came_in": "2"}))


@pytest.mark.asyncio
async def test_a_returned_parcel_that_was_sold_out_says_to_take_the_sale_back(client, session, auth):
    """Only the credit-note undo reaches this: the lot sold whole, so it is 'sold', not merely
    short. The way back is to take the invoice line back, which puts the lot on hand again."""
    p = await _parcel("return", client, session, auth)
    h = auth["headers"]
    inv = await _lot_doc(client, auth, "invoice", p.lot, p.sku, p.qty)
    assert (await client.post(f"/docs/{inv}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{inv}/fulfill-lines", headers=h, json={"line_entity_ids": [p.lot]})
    assert r.status_code == 200, r.text
    _assert_blocked(await p.undo(client, auth), p, ("docs.undo_lot_sold", {"sku": p.sku}))
    r = await client.post(f"/docs/{inv}/set-available", headers=h, json={"line_entity_ids": [p.lot]})
    assert r.status_code == 200, r.text
    assert (await p.undo(client, auth)).status_code == 200


@pytest.mark.asyncio
async def test_a_receipt_into_a_lot_with_its_stock_reserved_names_how_much_is_free(client, session, auth):
    """The lot holds the receipt's units, but too many are reserved to take them off: the
    shortfall is named in units and the way back is to release the reservation."""
    doc, lot, sku = await _topped_up_lot(client, session, auth)
    r = await client.post(f"/items/{lot}/reserve", headers=auth["headers"], json={"quantity": 12})
    assert r.status_code == 200, r.text
    p = _Parcel("receipt", doc, lot, sku, 5)
    _assert_blocked(await p.undo(client, auth), p,
                    ("docs.undo_lot_short", {"sku": sku, "free": "3", "qty": "5"}))
    r = await client.post(f"/items/{lot}/unreserve", headers=auth["headers"], json={"quantity": 12})
    assert r.status_code == 200, r.text
    assert (await p.undo(client, auth)).status_code == 200


@pytest.mark.asyncio
async def test_every_blocking_lot_is_named_with_its_own_reason(client, session, auth):
    """Two parcels block at once, for different reasons: one answer names both SKUs."""
    h = auth["headers"]
    lines = [{"sku": f"MULTI-{n}", "name": "Goods", "quantity": 4, "unit_price": 10.0, "line_total": 40.0,
              "receive_as": "stock"} for n in (1, 2)]
    bill = await _doc(client, auth, "bill", lines, total=80.0)
    assert (await client.post(f"/docs/{bill}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/docs/{bill}/receive", headers=h, json={"location_id": "", "received_items": [
        {"po_line_index": i, "sku": f"MULTI-{i + 1}", "name": "Goods", "quantity_received": 4, "receive_as": "stock"}
        for i in (0, 1)]})
    assert r.status_code == 200, r.text
    lots = (await _state(session, auth, bill))["received_item_ids"]
    skus = {(await _state(session, auth, lot))["sku"]: lot for lot in lots}
    await _reserve_one(client, auth, skus["MULTI-1"])
    assert (await client.post(f"/items/{skus['MULTI-2']}/status", headers=h,
                              json={"new_status": "archived"})).status_code == 200
    p = _Parcel("receipt", bill, skus["MULTI-1"], "MULTI-1", 4)
    _assert_blocked(await p.undo(client, auth), p,
                    ("docs.undo_lot_reserved", {"sku": "MULTI-1", "reserved": "1"}),
                    ("docs.undo_lot_not_available", {"sku": "MULTI-2", "lot_status": "archived"}))


def test_a_parcel_that_is_gone_is_a_keyed_reason_naming_no_internal_id():
    why = _parcel_moved_on(None, "item:abc", 2.0, receipt=True)
    assert why["message_key"] == "docs.undo_lot_missing" and why["params"] == {}, why
    assert "item:abc" not in why["message"], why

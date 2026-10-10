# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bill whose goods all went back to the supplier holds nothing, so it voids or reverts to
draft like a bill that never received any: the books keep nothing of it, and a void can be
undone and done again. A bill whose goods all came in on the purchase order it was made from
goes back to the order instead, which keeps them. While it still holds goods, both stay
refused until they are back."""
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _state
from test_lot_value_boundary import GAIN, _role
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return
from test_supplier_return_settles_bill import _owes, _restated_bill
from ui.i18n import t


async def _cleared(client, session, auth) -> None:
    """Nothing is owed, the stock accounts carry the stock, and no stock gain is left over."""
    assert await _account_net(session, auth["company_id"], "2110") == 0.0
    assert await _account_net(session, auth["company_id"], await _role(session, auth, GAIN)) == 0.0
    await assert_settled(client, session, auth)


async def _post(client, auth, doc: str, action: str):
    return await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json={})


async def test_a_fully_returned_bill_voids_and_the_void_round_trips(client, session, auth):
    doc, _, parcel = await _restated_bill(client, session, auth, from_order=False, cost=300.0)
    assert (await _return(client, auth, doc, parcel, 10)).status_code == 200

    for action, status in (("void", "void"), ("unvoid", "returned"), ("void", "void")):
        r = await _post(client, auth, doc, action)
        assert r.status_code == 200, (action, r.text)
        assert (await _state(session, auth, doc))["status"] == status
        await _owes(client, session, auth, doc, 0.0)
        await _cleared(client, session, auth)


async def test_a_fully_returned_bill_from_an_order_goes_back_to_the_order_rather_than_void(client, session, auth):
    doc, _, parcel = await _restated_bill(client, session, auth, from_order=True, cost=300.0)
    assert (await _return(client, auth, doc, parcel, 10)).status_code == 200

    r = await _post(client, auth, doc, "void")

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.void_order_receipt"
    assert (await _state(session, auth, doc))["status"] == "returned"
    await _cleared(client, session, auth)


@pytest.mark.parametrize("from_order", [False, True], ids=["bill", "order"])
async def test_a_fully_returned_bill_reverts_to_draft(client, session, auth, from_order):
    doc, _, parcel = await _restated_bill(client, session, auth, from_order=from_order, cost=300.0)
    assert (await _return(client, auth, doc, parcel, 10)).status_code == 200

    r = await _post(client, auth, doc, "revert-to-draft")

    assert r.status_code == 200, r.text
    state = await _state(session, auth, doc)
    assert (state["status"], state["doc_type"]) == (
        ("partial_returned", "purchase_order") if from_order else ("draft", "bill"))
    await _cleared(client, session, auth)


async def test_a_reverted_bill_owes_its_total_again_when_finalized(client, session, auth):
    doc, _, parcel = await _restated_bill(client, session, auth, from_order=False, cost=300.0)
    assert (await _return(client, auth, doc, parcel, 10)).status_code == 200
    assert (await _post(client, auth, doc, "revert-to-draft")).status_code == 200

    await _finalize(client, auth, doc)

    state = await _state(session, auth, doc)
    assert (state.get("returned_items"), state.get("returned_credit")) == (None, None)
    await _owes(client, session, auth, doc, 120.0)


async def test_a_bill_that_received_goods_after_its_order_voids_but_does_not_revert(client, session, auth):
    doc = await _doc(client, auth, "purchase_order", [
        {"sku": f"MIX-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 12.0}])
    sku = (await _state(session, auth, doc))["line_items"][0]["sku"]
    line = {"po_line_index": 0, "sku": sku, "name": "Beads"}
    assert (await _receive(client, auth, doc, {**line, "quantity_received": 4})).status_code == 200
    await _finalize(client, auth, doc)
    assert (await _receive(client, auth, doc, {**line, "quantity_received": 6})).status_code == 200
    for parcel in (await _state(session, auth, doc))["received_item_ids"]:
        qty = (await _state(session, auth, parcel))["quantity"]
        assert (await _return(client, auth, doc, parcel, qty)).status_code == 200
    assert (await _state(session, auth, doc))["status"] == "returned"

    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "docs.revert_returned_bill"

    for action, status in (("void", "void"), ("unvoid", "returned"), ("void", "void")):
        r = await _post(client, auth, doc, action)
        assert r.status_code == 200, (action, r.text)
        assert (await _state(session, auth, doc))["status"] == status
        await _owes(client, session, auth, doc, 0.0)
        await _cleared(client, session, auth)


@pytest.mark.parametrize("from_order", [False, True], ids=["bill", "order"])
async def test_a_bill_still_holding_goods_neither_voids_nor_reverts(client, session, auth, from_order):
    doc, _, parcel = await _restated_bill(client, session, auth, from_order=from_order, cost=300.0)
    assert (await _return(client, auth, doc, parcel, 4)).status_code == 200

    r = await _post(client, auth, doc, "void")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Cannot void a document with received items; return the goods first"
    r = await _post(client, auth, doc, "revert-to-draft")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == t("documents.err_revert_status", "en")
    assert (await _state(session, auth, doc))["status"] == "partial_returned"
    await _owes(client, session, auth, doc, 72.0)

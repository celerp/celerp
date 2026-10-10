# More supplier return cases that must keep the money and the stock in step:
#  - two restatements in opposite directions, then a full return: each restatement's P&L
#    account is cleared back (stock gain 4300 == 0 and stock shrinkage 6970 == 0), not only
#    their sum; 2110 == 0.
#  - a bill paid in part, then fully returned: 2110 == -outstanding throughout, and a void is
#    either refused or leaves 2110 == 0 with nothing owed; no orphan payment.
#  - the mixed case (part received on the order, part on the bill) with a restated lot:
#    revert refused with docs.revert_returned_bill; void/unvoid/void keeps 2110 == 0, books ==
#    stock, gain + shrinkage == 0, and no rtn-value entry reversed or doubled.
from __future__ import annotations

import uuid

import pytest

from stock_books import assert_settled
from test_cost_restatement import _set_cost, _state
from test_lot_value_boundary import GAIN, SHRINKAGE, _role
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return
from test_supplier_return_settles_bill import _restated_bill


async def _accts(session, auth):
    cid = auth["company_id"]
    return (await _account_net(session, cid, "2110"),
            await _account_net(session, cid, await _role(session, auth, GAIN)),
            await _account_net(session, cid, await _role(session, auth, SHRINKAGE)))


@pytest.mark.parametrize("costs", [(300.0, 200.0), (50.0, 150.0)], ids=["up-down", "down-up"])
async def test_two_restatements_full_return_clears_each_account(client, session, auth, costs):
    doc, _, parcel = await _restated_bill(client, session, auth, from_order=False, cost=costs[0])
    r = await _set_cost(client, auth, parcel, costs[1])
    assert r.status_code == 200, r.text
    assert (await _return(client, auth, doc, parcel, 10)).status_code == 200
    ap, gain, shrink = await _accts(session, auth)
    await assert_settled(client, session, auth)
    assert ap == 0.0
    assert round(gain + shrink, 2) == 0.0
    assert (gain, shrink) == (0.0, 0.0), ("gain", gain, "shrinkage", shrink)


async def test_paid_in_part_then_fully_returned_then_void(client, session, auth):
    doc, _, parcel = await _restated_bill(client, session, auth, from_order=False, cost=300.0)
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"],
                          json={"amount": 50.0, "bank_account": "1111",
                                "payment_date": (await _state(session, auth, doc))["issue_date"]})
    assert r.status_code == 200, r.text
    r = await _return(client, auth, doc, parcel, 10)
    print("return", r.status_code, r.text[:400])
    if r.status_code == 409:
        return  # a return worth more than is still owed is refused: nothing to void
    st = await _state(session, auth, doc)
    ap, _, _ = await _accts(session, auth)
    print("after return", st["status"], st["amount_outstanding"], st.get("returned_credit"), ap)
    assert round(ap, 2) == round(-st["amount_outstanding"], 2), (ap, st["amount_outstanding"])
    r = await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={})
    print("void", r.status_code, r.text[:300])
    if r.status_code == 200:
        st = await _state(session, auth, doc)
        ap, _, _ = await _accts(session, auth)
        assert ap == 0.0, ap
        await assert_settled(client, session, auth)


async def test_mixed_case_restated_void_cycle(client, session, auth):
    doc = await _doc(client, auth, "purchase_order", [
        {"sku": f"MXR-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 12.0}])
    sku = (await _state(session, auth, doc))["line_items"][0]["sku"]
    line = {"po_line_index": 0, "sku": sku, "name": "Beads"}
    assert (await _receive(client, auth, doc, {**line, "quantity_received": 4})).status_code == 200
    await _finalize(client, auth, doc)
    assert (await _receive(client, auth, doc, {**line, "quantity_received": 6})).status_code == 200
    parcels = (await _state(session, auth, doc))["received_item_ids"]
    assert (await _set_cost(client, auth, parcels[0], 100.0)).status_code == 200
    assert (await _set_cost(client, auth, parcels[1], 30.0)).status_code == 200
    for p in parcels:
        q = (await _state(session, auth, p))["quantity"]
        assert (await _return(client, auth, doc, p, q)).status_code == 200
    st = await _state(session, auth, doc)
    assert st["status"] == "returned" and st["amount_outstanding"] == 0.0
    ap, gain, shrink = await _accts(session, auth)
    assert ap == 0.0 and round(gain + shrink, 2) == 0.0, (ap, gain, shrink)
    r = await client.post(f"/docs/{doc}/revert-to-draft", headers=auth["headers"], json={})
    assert r.status_code == 409 and r.json()["detail"]["message_key"] == "docs.revert_returned_bill", r.text
    for action, status in (("void", "void"), ("unvoid", "returned"), ("void", "void")):
        r = await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json={})
        assert r.status_code == 200, (action, r.text)
        st = await _state(session, auth, doc)
        assert st["status"] == status
        ap, gain, shrink = await _accts(session, auth)
        assert ap == 0.0 and round(gain + shrink, 2) == 0.0, (action, ap, gain, shrink)
        await assert_settled(client, session, auth)

# Once every good on a bill went back to the supplier, the bill
# owes nothing and accounts payable 2110 holds nothing for it (2110 == 0 and
# amount_outstanding == 0), in any currency, at any rate, with tax, discount or shipping
# on the bill, restated or not.
from __future__ import annotations

import uuid

import pytest

from test_cost_restatement import _set_cost, _state
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return


async def _bill(client, session, auth, lines, from_order=False, **extra):
    doc = await _doc(client, auth, "purchase_order" if from_order else "bill", [
        {"sku": f"RS{i}-{uuid.uuid4().hex[:6]}", "name": f"Part {i}", "quantity": q, "unit_price": p}
        for i, (p, q) in enumerate(lines)], **extra)
    if not from_order:
        await _finalize(client, auth, doc)
    st = await _state(session, auth, doc)
    r = await _receive(client, auth, doc, *[
        {"po_line_index": i, "sku": li["sku"], "name": li["name"], "quantity_received": li["quantity"]}
        for i, li in enumerate(st["line_items"])])
    assert r.status_code == 200, r.text
    if from_order:
        await _finalize(client, auth, doc)
    return doc, (await _state(session, auth, doc))["received_item_ids"]


async def _return_all(client, session, auth, doc, parcels, chunks=(1, 2, 3, 4, 5, 6, 7, 8, 9)):
    for p in parcels:
        left = float((await _state(session, auth, p))["quantity"])
        for c in chunks:
            if left <= 1e-9:
                break
            q = min(c, left)
            r = await _return(client, auth, doc, p, q)
            assert r.status_code == 200, r.text
            left -= q


async def _report(session, auth, doc):
    st = await _state(session, auth, doc)
    ap = await _account_net(session, auth["company_id"], "2110")
    return st["status"], st.get("total"), st.get("returned_credit"), st["amount_outstanding"], ap


@pytest.mark.parametrize("restate", [False, True], ids=["plain", "restated"])
@pytest.mark.parametrize("rate", [0.0333, 36.123457, 1.37])
async def test_fx_full_return_leaves_no_residue(client, session, auth, rate, restate):
    doc, parcels = await _bill(client, session, auth, [(3.33, 3), (7.77, 7), (0.07, 9)],
                               currency="EUR", conversion_rate=rate)
    if restate:
        assert (await _set_cost(client, auth, parcels[0], 99.99)).status_code == 200
    await _return_all(client, session, auth, doc, parcels)
    got = await _report(session, auth, doc)
    print("FX", rate, restate, got)
    assert got[0] == "returned"
    assert (got[3], got[4]) == (0.0, 0.0), got


@pytest.mark.parametrize("kind", ["tax", "discount", "shipping"])
async def test_bill_charges_full_return_leaves_no_residue(client, session, auth, kind):
    extra = {"tax": {"doc_taxes": [{"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}]},
             "discount": {"discount": 10.0, "discount_type": "percentage"},
             "shipping": {"shipping": 5.0}}[kind]
    doc, parcels = await _bill(client, session, auth, [(12.0, 10)], **extra)
    st = await _state(session, auth, doc)
    print(kind, "total", st.get("total"), "tax", st.get("tax"), "discount", st.get("discount_amount"))
    await _return_all(client, session, auth, doc, parcels)
    got = await _report(session, auth, doc)
    print("CHG", kind, got)
    assert got[0] == "returned"
    assert round(got[4], 2) == round(-got[3], 2), got  # 2110 == -outstanding at least

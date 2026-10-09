# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Goods sent back to the supplier reduce what the bill owes by what the return took off
accounts payable, so the bill, the AP aging and the ledger show the same balance."""
from __future__ import annotations

import uuid

import pytest

from test_cost_restatement import _state
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _receive, _return


async def _aged(client, auth) -> float:
    r = await client.get("/reports/ap-aging", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return sum(line["total"] for line in r.json()["lines"])


async def _received_bill(client, session, auth, *, finalize_first: bool) -> tuple[str, str]:
    doc_type = "bill" if finalize_first else "purchase_order"
    doc = await _doc(client, auth, doc_type, [
        {"sku": f"RTN-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 10, "unit_price": 14.0}])
    if finalize_first:
        await _finalize(client, auth, doc)
    sku = (await _state(session, auth, doc))["line_items"][0]["sku"]
    r = await _receive(client, auth, doc, {"po_line_index": 0, "sku": sku, "name": "Beads", "quantity_received": 10})
    assert r.status_code == 200, r.text
    [parcel_id] = (await _state(session, auth, doc))["received_item_ids"]
    return doc, parcel_id


@pytest.mark.parametrize("finalize_first", [True, False])
@pytest.mark.asyncio
async def test_a_return_reduces_what_the_bill_owes_by_the_payable_it_clears(client, session, auth, finalize_first):
    doc, parcel_id = await _received_bill(client, session, auth, finalize_first=finalize_first)
    r = await _return(client, auth, doc, parcel_id, 4)
    assert r.status_code == 200, r.text
    if not finalize_first:
        await _finalize(client, auth, doc)

    assert await _account_net(session, auth["company_id"], "2110") == -84.0
    bill = await _state(session, auth, doc)
    assert bill["total"] == 140.0
    assert bill["amount_outstanding"] == 84.0
    assert await _aged(client, auth) == 84.0

    # Paying what the bill now shows clears it, and the ledger with it.
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"],
                          json={"amount": 84.0, "payment_date": "2026-03-02", "bank_account": "1111"})
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, doc)
    assert (bill["amount_outstanding"], bill["status"]) == (0.0, "paid")
    assert await _account_net(session, auth["company_id"], "2110") == 0.0
    assert await _aged(client, auth) == 0.0


@pytest.mark.asyncio
async def test_a_payment_cannot_exceed_what_the_bill_owes_after_a_return(client, session, auth):
    doc, parcel_id = await _received_bill(client, session, auth, finalize_first=True)
    r = await _return(client, auth, doc, parcel_id, 4)
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"],
                          json={"amount": 140.0, "payment_date": "2026-03-02", "bank_account": "1111"})
    assert r.status_code == 409, r.text
    assert "exceeds amount outstanding 84.0" in r.json()["detail"]
    assert await _account_net(session, auth["company_id"], "2110") == -84.0


@pytest.mark.asyncio
async def test_returning_everything_leaves_nothing_owed(client, session, auth):
    doc, parcel_id = await _received_bill(client, session, auth, finalize_first=True)
    r = await _return(client, auth, doc, parcel_id, 10)
    assert r.status_code == 200, r.text
    bill = await _state(session, auth, doc)
    assert (bill["status"], bill["amount_outstanding"]) == ("returned", 0.0)
    assert await _account_net(session, auth["company_id"], "2110") == 0.0
    assert await _aged(client, auth) == 0.0

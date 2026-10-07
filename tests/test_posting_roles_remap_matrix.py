# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Remapping a posting role twice never disturbs what already happened.

Each role is exercised through a full state-transition matrix: recognize on
account A, remap the role to account B, settle or reverse the A-era object
(it must still use A), recognize a new object (it must use B), then remap
back to A and prove the B-era object's own settlement, reversal, or
consumption still uses B while anything recognized after the remap back uses
A again. A statement of account and a credit-note application are checked
the same way: history from every era adds up, and a reversal always returns
to the account its own object was recognized on.

Landed-cost clearing is covered by test_posting_roles_landed.py and is not
repeated here.
"""
from __future__ import annotations

import pytest

from celerp.services.account_roles import set_role
from test_cost_restatement import _state
from test_money_stock_and_contact_invariants import _account_net
from test_posting_roles_lots import _credits, _lot, _new_inventory_account, _sell
from test_posting_roles_lots import _remap as _remap_inventory_opening


async def _account(client, auth, code: str, account_type: str, parent_code: str | None = None) -> None:
    body = {"code": code, "name": f"Account {code}", "account_type": account_type}
    if parent_code:
        body["parent_code"] = parent_code
    r = await client.post("/accounting/accounts", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text


async def _remap(session, auth, role: str, code: str) -> None:
    await set_role(session, auth["company_id"], role, code)
    await session.commit()


async def _invoice(client, auth, total: float, **extra) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"name": "Service", "quantity": 1, "unit_price": total}],
        "total": total, **extra,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc_id


async def _bill(client, auth, total: float) -> str:
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "bill", "line_items": [{"name": "Rent", "quantity": 1, "unit_price": total,
                                            "receive_as": "expense"}], "total": total,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return doc_id


async def _pay(client, auth, doc_id: str, amount: float, **extra):
    return await client.post(f"/docs/{doc_id}/payment", headers=auth["headers"], json={
        "amount": amount, "payment_date": "2026-03-02", "bank_account": "1111", **extra})


async def _void_payment(client, auth, doc_id: str, payment_index: int):
    r = await client.post(f"/docs/{doc_id}/void-payment", headers=auth["headers"], json={"payment_index": payment_index})
    assert r.status_code == 200, r.text


async def _void(client, auth, doc_id: str):
    r = await client.post(f"/docs/{doc_id}/void", headers=auth["headers"], json={"reason": "test"})
    assert r.status_code == 200, r.text


async def _contact(client, auth) -> str:
    r = await client.post("/crm/contacts", headers=auth["headers"], json={"name": "Cust", "contact_type": "customer"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _cogs_debit(je: dict) -> dict[str, float]:
    """The COGS debits of an entry, by account."""
    return {e["account"]: e["debit"] for e in je["entries"] if "cogs" in (e.get("account_roles") or [])}


@pytest.mark.asyncio
async def test_a_receivable_settles_and_resettles_on_each_eras_own_account_through_two_remaps(session, client, auth):
    cid = auth["company_id"]
    # 1. Recognize on A (the seeded receivable, 1120).
    inv_a = await _invoice(client, auth, 100.0)
    je = await _state(session, auth, f"je:auto:{inv_a}:fin")
    assert {e["account"] for e in je["entries"] if "receivable" in (e.get("account_roles") or [])} == {"1120"}

    # 2. Remap receivable to B.
    await _account(client, auth, "1121", "asset", "1100")
    await _remap(session, auth, "receivable", "1121")

    # 3. Settle, then reverse, the A-era invoice: it must still clear on A.
    assert (await _pay(client, auth, inv_a, 100.0)).status_code == 200
    assert await _account_net(session, cid, "1120") == 0.0
    assert await _account_net(session, cid, "1121") == 0.0
    await _void_payment(client, auth, inv_a, 0)
    assert await _account_net(session, cid, "1120") == 100.0
    assert await _account_net(session, cid, "1121") == 0.0

    # 4. A new invoice now recognizes on B.
    inv_b = await _invoice(client, auth, 40.0)
    je = await _state(session, auth, f"je:auto:{inv_b}:fin")
    assert {e["account"] for e in je["entries"] if "receivable" in (e.get("account_roles") or [])} == {"1121"}

    # 5. The split holds: the unpaid A-era invoice on A, the new one on B.
    assert await _account_net(session, cid, "1120") == 100.0
    assert await _account_net(session, cid, "1121") == 40.0

    # 6. Remap back to A. The B-era invoice still settles and reverses on B.
    await _remap(session, auth, "receivable", "1120")
    assert (await _pay(client, auth, inv_b, 40.0)).status_code == 200
    assert await _account_net(session, cid, "1121") == 0.0
    assert await _account_net(session, cid, "1120") == 100.0
    await _void_payment(client, auth, inv_b, 0)
    assert await _account_net(session, cid, "1121") == 40.0
    assert await _account_net(session, cid, "1120") == 100.0
    # A new invoice recognized after the remap back lands on A again.
    inv_c = await _invoice(client, auth, 25.0)
    je = await _state(session, auth, f"je:auto:{inv_c}:fin")
    assert {e["account"] for e in je["entries"] if "receivable" in (e.get("account_roles") or [])} == {"1120"}
    assert await _account_net(session, cid, "1120") == 125.0
    assert await _account_net(session, cid, "1121") == 40.0


@pytest.mark.asyncio
async def test_a_payable_settles_and_resettles_on_each_eras_own_account_through_two_remaps(session, client, auth):
    cid = auth["company_id"]
    # 1. Recognize on A (the seeded payable, 2110).
    bill_a = await _bill(client, auth, 100.0)
    je = await _state(session, auth, f"je:auto:{bill_a}:bill")
    assert {e["account"] for e in je["entries"] if "payable" in (e.get("account_roles") or [])} == {"2110"}

    # 2. Remap payable to B.
    await _account(client, auth, "2111", "liability", "2100")
    await _remap(session, auth, "payable", "2111")

    # 3. Settle, then reverse, the A-era bill: it must still clear on A.
    assert (await _pay(client, auth, bill_a, 100.0)).status_code == 200
    assert await _account_net(session, cid, "2110") == 0.0
    assert await _account_net(session, cid, "2111") == 0.0
    await _void_payment(client, auth, bill_a, 0)
    assert await _account_net(session, cid, "2110") == -100.0
    assert await _account_net(session, cid, "2111") == 0.0

    # 4. A new bill now recognizes on B.
    bill_b = await _bill(client, auth, 60.0)
    je = await _state(session, auth, f"je:auto:{bill_b}:bill")
    assert {e["account"] for e in je["entries"] if "payable" in (e.get("account_roles") or [])} == {"2111"}

    # 5. The split holds: the unpaid A-era bill on A, the new one on B.
    assert await _account_net(session, cid, "2110") == -100.0
    assert await _account_net(session, cid, "2111") == -60.0

    # 6. Remap back to A. The B-era bill still settles and reverses on B.
    await _remap(session, auth, "payable", "2110")
    assert (await _pay(client, auth, bill_b, 60.0)).status_code == 200
    assert await _account_net(session, cid, "2111") == 0.0
    assert await _account_net(session, cid, "2110") == -100.0
    await _void_payment(client, auth, bill_b, 0)
    assert await _account_net(session, cid, "2111") == -60.0
    assert await _account_net(session, cid, "2110") == -100.0
    # A new bill recognized after the remap back lands on A again.
    bill_c = await _bill(client, auth, 25.0)
    je = await _state(session, auth, f"je:auto:{bill_c}:bill")
    assert {e["account"] for e in je["entries"] if "payable" in (e.get("account_roles") or [])} == {"2110"}
    assert await _account_net(session, cid, "2110") == -125.0
    assert await _account_net(session, cid, "2111") == -60.0


@pytest.mark.asyncio
async def test_a_lot_relieves_and_rebuys_on_each_eras_own_inventory_account_through_two_remaps(session, client, auth):
    # 1. A lot entered on A (the seeded inventory_opening account, 1130-OB).
    lot_a = await _lot(client, auth, 30.0)
    assert (await _state(session, auth, lot_a))["inventory_account_code"] == "1130-OB"

    # 2. Remap inventory_opening to B.
    await _remap_inventory_opening(session, auth, await _new_inventory_account(client, auth, "1131"))

    # 3. Selling the A-era lot relieves it on A, not B.
    inv1 = await _sell(client, auth, (lot_a, 1))
    je1 = await _state(session, auth, f"je:auto:{inv1}:fin")
    assert _credits(je1) == {"1130-OB": 30.0}

    # 4. A new lot entered now is booked on B.
    lot_b = await _lot(client, auth, 12.0)
    assert (await _state(session, auth, lot_b))["inventory_account_code"] == "1131"

    # 5. Both eras' lots keep their own account.
    assert (await _state(session, auth, lot_a))["inventory_account_code"] == "1130-OB"
    assert (await _state(session, auth, lot_b))["inventory_account_code"] == "1131"

    # 6. Remap back to A. Selling the B-era lot still relieves it on B.
    await _remap_inventory_opening(session, auth, "1130-OB")
    inv2 = await _sell(client, auth, (lot_b, 1))
    je2 = await _state(session, auth, f"je:auto:{inv2}:fin")
    assert _credits(je2) == {"1131": 12.0}
    # A lot entered after the remap back is booked on A again.
    lot_c = await _lot(client, auth, 8.0)
    assert (await _state(session, auth, lot_c))["inventory_account_code"] == "1130-OB"


@pytest.mark.asyncio
async def test_an_invoices_cogs_stays_on_its_own_account_through_two_cogs_remaps(session, client, auth):
    cid = auth["company_id"]
    # 1. A sale recognizes COGS on A (the seeded cogs account, 5100).
    lot1 = await _lot(client, auth, 30.0)
    inv1 = await _sell(client, auth, (lot1, 1))
    je1 = await _state(session, auth, f"je:auto:{inv1}:fin")
    assert _cogs_debit(je1) == {"5100": 30.0}
    assert await _account_net(session, cid, "5100") == 30.0

    # 2. Remap cogs to B.
    await _account(client, auth, "5101", "cogs", "5000")
    await _remap(session, auth, "cogs", "5101")

    # 3. Voiding the A-era invoice leaves its COGS line naming A, unchanged.
    await _void(client, auth, inv1)
    je1 = await _state(session, auth, f"je:auto:{inv1}:fin")
    assert je1["status"] == "void"
    assert _cogs_debit(je1) == {"5100": 30.0}
    assert await _account_net(session, cid, "5100") == 0.0
    assert await _account_net(session, cid, "5101") == 0.0

    # 4. A new sale now recognizes COGS on B.
    lot2 = await _lot(client, auth, 20.0)
    inv2 = await _sell(client, auth, (lot2, 1))
    je2 = await _state(session, auth, f"je:auto:{inv2}:fin")
    assert _cogs_debit(je2) == {"5101": 20.0}
    assert await _account_net(session, cid, "5101") == 20.0
    assert await _account_net(session, cid, "5100") == 0.0

    # 5. The voided A-era invoice's own line still names A after the new B-era sale.
    je1 = await _state(session, auth, f"je:auto:{inv1}:fin")
    assert _cogs_debit(je1) == {"5100": 30.0}

    # 6. Remap back to A. Voiding the B-era invoice leaves its COGS line naming B.
    await _remap(session, auth, "cogs", "5100")
    await _void(client, auth, inv2)
    je2 = await _state(session, auth, f"je:auto:{inv2}:fin")
    assert je2["status"] == "void"
    assert _cogs_debit(je2) == {"5101": 20.0}
    assert await _account_net(session, cid, "5101") == 0.0
    assert await _account_net(session, cid, "5100") == 0.0
    # A sale recognized after the remap back posts COGS on A again.
    lot3 = await _lot(client, auth, 15.0)
    inv3 = await _sell(client, auth, (lot3, 1))
    je3 = await _state(session, auth, f"je:auto:{inv3}:fin")
    assert _cogs_debit(je3) == {"5100": 15.0}
    assert await _account_net(session, cid, "5100") == 15.0
    assert await _account_net(session, cid, "5101") == 0.0


@pytest.mark.asyncio
async def test_an_exchange_gain_reverses_on_its_own_account_through_two_fx_gain_remaps(session, client, auth):
    cid = auth["company_id"]
    # 1. An exchange gain on settlement posts on A.
    await _account(client, auth, "4910", "revenue")
    await _remap(session, auth, "fx_gain", "4910")
    inv1 = await _invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    assert (await _pay(client, auth, inv1, 100.0, conversion_rate=1.2)).status_code == 200
    assert await _account_net(session, cid, "4910") == -10.0

    # 2. Remap fx_gain to B.
    await _account(client, auth, "4920", "revenue")
    await _remap(session, auth, "fx_gain", "4920")

    # 3. Voiding the A-era payment reverses the gain on A, not B.
    await _void_payment(client, auth, inv1, 0)
    assert await _account_net(session, cid, "4910") == 0.0
    assert await _account_net(session, cid, "4920") == 0.0

    # 4. A new settlement now posts its gain on B.
    inv2 = await _invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    assert (await _pay(client, auth, inv2, 100.0, conversion_rate=1.2)).status_code == 200
    assert await _account_net(session, cid, "4920") == -10.0
    assert await _account_net(session, cid, "4910") == 0.0

    # 5. (verified above: each era's gain sits on its own account.)

    # 6. Remap back to A. Voiding the B-era payment reverses the gain on B.
    await _remap(session, auth, "fx_gain", "4910")
    await _void_payment(client, auth, inv2, 0)
    assert await _account_net(session, cid, "4920") == 0.0
    assert await _account_net(session, cid, "4910") == 0.0
    # A settlement after the remap back posts its gain on A again.
    inv3 = await _invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)
    assert (await _pay(client, auth, inv3, 100.0, conversion_rate=1.2)).status_code == 200
    assert await _account_net(session, cid, "4910") == -10.0
    assert await _account_net(session, cid, "4920") == 0.0


@pytest.mark.asyncio
async def test_a_statement_of_account_totals_every_receivable_era_including_the_return_to_the_first(session, client, auth):
    await _account(client, auth, "1121", "asset", "1100")
    contact = await _contact(client, auth)
    await _invoice(client, auth, 100.0, contact_id=contact, issue_date="2026-01-05")
    await _remap(session, auth, "receivable", "1121")
    await _invoice(client, auth, 40.0, contact_id=contact, issue_date="2026-02-05")
    await _remap(session, auth, "receivable", "1120")
    await _invoice(client, auth, 25.0, contact_id=contact, issue_date="2026-03-05")

    r = await client.get(f"/accounting/soa/{contact}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    soa = r.json()
    assert [row["debit"] for row in soa["rows"]] == [100.0, 40.0, 25.0]
    assert soa["closing_balance"] == pytest.approx(165.0)


@pytest.mark.asyncio
async def test_a_credit_note_application_and_its_unapply_clear_on_the_invoices_own_receivable_account(session, client, auth):
    cid = auth["company_id"]
    contact = await _contact(client, auth)
    inv = await _invoice(client, auth, 100.0, contact_id=contact)

    # Remap receivable to B after the invoice is already recognized on A.
    await _account(client, auth, "1121", "asset", "1100")
    await _remap(session, auth, "receivable", "1121")

    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "credit_note", "original_doc_id": inv, "contact_id": contact,
        "line_items": [{"name": "CN", "quantity": 1, "unit_price": 40.0, "line_total": 40.0}], "total": 40.0,
    })
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    r = await client.post(f"/docs/{cn}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text

    r = await client.post(f"/docs/{cn}/apply-to-invoice", headers=auth["headers"], json={
        "target_doc_id": inv, "amount": 40.0, "date": "2026-02-01",
    })
    assert r.status_code == 200, r.text

    # Both legs land on the invoice's own account: the credit note, issued in Celerp
    # with no recognition of its own, clears against the invoice's origin rather than
    # the account the receivable role currently points at, so the net balance on 1120
    # does not move and 1121 is never touched.
    je = await _state(session, auth, f"je:auto:{inv}:cnapply:{cn}:0")
    assert {(e["account"], e["debit"], e["credit"]) for e in je["entries"]} == {
        ("1120", 0.0, 40.0), ("1120", 40.0, 0.0),
    }
    assert await _account_net(session, cid, "1120") == 100.0
    assert await _account_net(session, cid, "1121") == 0.0
    doc = (await client.get(f"/docs/{inv}", headers=auth["headers"])).json()
    assert doc["amount_paid"] == 40.0
    assert doc["amount_outstanding"] == 60.0

    # Unapply: the reversal clears the same original account, even though receivable
    # now points at B.
    await _void_payment(client, auth, inv, 0)
    je = await _state(session, auth, f"je:auto:{inv}:cnapply:{cn}:0")
    assert je["status"] == "void"
    assert {(e["account"], e["debit"], e["credit"]) for e in je["entries"]} == {
        ("1120", 0.0, 40.0), ("1120", 40.0, 0.0),
    }
    assert await _account_net(session, cid, "1120") == 100.0
    assert await _account_net(session, cid, "1121") == 0.0
    doc = (await client.get(f"/docs/{inv}", headers=auth["headers"])).json()
    assert doc["amount_paid"] == 0.0
    assert doc["amount_outstanding"] == 100.0

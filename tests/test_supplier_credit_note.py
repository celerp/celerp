# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Supplier credit notes: what a supplier owes the company back.

A return on a bill that is already paid raises one for the part the bill no longer owes.
One can also be issued by hand for a price or charge adjustment. Either kind is applied
to a later bill of the same supplier, refunded into a bank account, or voided. Voiding a
note a return raised undoes the whole return: the goods are back in stock at their cost,
the return's entries are reversed and the bill holds them again."""
from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.output.pdf import _doc_title
from celerp_inventory.services import goods_basis
from stock_books import assert_settled
from test_cost_restatement import _state
from test_lot_value_boundary import _role
from test_money_stock_and_contact_invariants import _account_net
from test_receipt_accounting import _doc, _finalize, _return
from test_supplier_return_settles_bill import (
    _aged, _pay, _received_bill, journal, paid_bill_returned, supplier_credits,
)

pytestmark = pytest.mark.asyncio

AP, BANK = "2110", "1111"
_VAT = {"doc_taxes": [{"code": "VAT", "rate": 7.0, "order": 1, "is_compound": False, "label": "VAT"}]}


def _charge(amount: float) -> dict:
    return {"name": "Handling", "quantity": 1, "unit_price": amount}


async def _post(client, auth, doc: str, action: str, body: dict | None = None):
    return await client.post(f"/docs/{doc}/{action}", headers=auth["headers"], json=body or {})


async def _bill(client, auth, amount: float, **extra) -> str:
    doc = await _doc(client, auth, "bill", [_charge(amount)], **extra)
    await _finalize(client, auth, doc)
    return doc


async def _note(client, session, auth) -> tuple[str, str]:
    """A bill paid in full and every good sent back: its supplier credit of 140.
    -> (bill, note)."""
    doc, _parcel = await paid_bill_returned(client, session, auth)
    [note] = await supplier_credits(session, auth, doc)
    return doc, note["id"]


async def _apply(client, auth, note: str, target: str, amount: float):
    return await _post(client, auth, note, "apply-to-bill",
                       {"target_doc_id": target, "amount": amount, "date": "2026-03-05"})


async def _refund(client, auth, note: str, amount: float, key: str | None = None):
    body = {"amount": amount, "date": "2026-03-05", "bank_account": BANK}
    if key:
        body["idempotency_key"] = key
    return await _post(client, auth, note, "cn-refund", body)


def _index(state: dict, method: str) -> int:
    [p] = [p for p in state.get("payments") or [] if p.get("method") == method and not p.get("voided")]
    return p["index"]


async def _trial_balance(client, auth) -> dict[str, float]:
    r = await client.get("/accounting/trial-balance", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return {line["code"]: round(line["net"], 2) for line in r.json()["lines"] if round(line["net"], 2)}


def _code(r) -> str:
    detail = r.json()["detail"]
    return detail.get("message_key") if isinstance(detail, dict) else detail


async def test_void_bill_payment_after_return_restores_owed(client, session, auth):
    """The note stays open for what was paid; voiding the payment makes the bill owe it."""
    doc, note = await _note(client, session, auth)
    [payment] = (await _state(session, auth, doc))["payments"]
    r = await _post(client, auth, doc, "void-payment", {"payment_index": payment["index"]})
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, doc))["amount_outstanding"] == 140.0
    assert (await _state(session, auth, note))["amount_outstanding"] == 140.0
    assert await _aged(client, auth) == 0.0 == -(await _account_net(session, auth["company_id"], AP))


async def test_apply_supplier_credit_to_later_bill(client, session, auth):
    _doc_id, note = await _note(client, session, auth)
    later = await _bill(client, auth, 200.0)
    r = await _apply(client, auth, note, later, 140.0)
    assert r.status_code == 200, r.text
    bill, credit = await _state(session, auth, later), await _state(session, auth, note)
    assert (bill["amount_outstanding"], bill["status"]) == (60.0, "partial")
    assert (credit["amount_outstanding"], credit["status"]) == (0.0, "paid")
    [paid_by] = bill["payments"]
    assert (paid_by["method"], paid_by["source_doc_id"]) == ("supplier_credit", note)
    assert not bill.get("credited")
    [entry] = (await journal(session, auth, f"je:auto:{later}:dnapply:{note}:")).values()
    on_ap = [e for e in entry["entries"] if e["account"] == AP]
    assert sum(e.get("debit") or 0 for e in on_ap) == sum(e.get("credit") or 0 for e in on_ap) == 140.0
    assert await _account_net(session, auth["company_id"], AP) == -60.0
    assert await _aged(client, auth) == 60.0


async def test_apply_supplier_credit_refuses_other_supplier_currency_or_excess(client, session, auth):
    _doc_id, note = await _note(client, session, auth)
    other = await _bill(client, auth, 200.0, contact_id="supplier:2")
    foreign = await _bill(client, auth, 200.0, currency="EUR", conversion_rate=1.1)
    same = await _bill(client, auth, 200.0)

    r = await _apply(client, auth, note, other, 10.0)
    assert r.status_code == 422 and "different suppliers" in r.text, r.text
    r = await _apply(client, auth, note, foreign, 10.0)
    assert r.status_code == 422 and "different currencies" in r.text, r.text
    r = await _apply(client, auth, note, same, 141.0)
    assert r.status_code == 409 and "supplier credit note" in r.text, r.text
    assert (await _state(session, auth, same))["amount_outstanding"] == 200.0


async def test_apply_supplier_credit_refused_without_record_payments(client, session, auth):
    """Applying a supplier credit is a payment on the bill: a role without
    record_payments is refused and neither document moves."""
    from test_helpers import invite_user

    _doc_id, note = await _note(client, session, auth)
    later = await _bill(client, auth, 200.0)
    viewer = await invite_user(client, session, auth["headers"], "viewer@scn.example", "viewer")
    r = await _apply(client, {"headers": {"Authorization": f"Bearer {viewer}"}}, note, later, 140.0)
    assert r.status_code == 403, r.text
    assert (await _state(session, auth, later))["amount_outstanding"] == 200.0
    assert (await _state(session, auth, note))["amount_outstanding"] == 140.0


async def test_refund_supplier_credit_books_bank_debit_ap_credit(client, session, auth):
    _doc_id, note = await _note(client, session, auth)
    bank = await _account_net(session, auth["company_id"], BANK)
    for _ in range(2):
        r = await _refund(client, auth, note, 140.0, key="refund-once")
        assert r.status_code == 200, r.text
    assert await _account_net(session, auth["company_id"], BANK) == round(bank + 140.0, 3)
    assert await _account_net(session, auth["company_id"], AP) == 0.0
    credit = await _state(session, auth, note)
    assert (credit["status"], credit["amount_outstanding"], len(credit["payments"])) == ("paid", 0.0, 1)


async def test_native_supplier_credit_note_finalize_books_swapped_bill_entry(client, session, auth):
    note = await _doc(client, auth, "debit_note", [_charge(50.0)], **_VAT)
    await _finalize(client, auth, note)
    [entry] = (await journal(session, auth, f"je:auto:{note}:fin")).values()
    expense = await _role(session, auth, "general_expense")
    vat = await _role(session, auth, "tax_input")
    sides = {(e["account"], e.get("debit") or 0.0, e.get("credit") or 0.0) for e in entry["entries"]}
    assert sides == {(AP, 53.5, 0.0), (expense, 0.0, 50.0), (vat, 0.0, 3.5)}, sides
    assert await _aged(client, auth) == -53.5


async def test_native_supplier_credit_note_against_bill_settles_it_through_apply(client, session, auth):
    bill = await _bill(client, auth, 200.0)
    note = await _doc(client, auth, "debit_note", [_charge(50.0)], original_doc_id=bill)
    await _finalize(client, auth, note)
    billed, credit = await _state(session, auth, bill), await _state(session, auth, note)
    assert (billed["amount_outstanding"], billed["status"]) == (150.0, "partial")
    assert [p["method"] for p in billed["payments"]] == ["supplier_credit"]
    assert not billed.get("credited")
    assert (credit["amount_outstanding"], credit["status"]) == (0.0, "paid")
    assert await _account_net(session, auth["company_id"], AP) == -150.0
    assert await _aged(client, auth) == 150.0


async def test_native_supplier_credit_refuses_stock_lines(client, session, auth):
    note = await _doc(client, auth, "debit_note", [
        {"sku": f"SCN-{uuid.uuid4().hex[:6]}", "name": "Beads", "quantity": 2, "unit_price": 14.0}])
    r = await _post(client, auth, note, "finalize")
    assert r.status_code == 409, r.text
    assert _code(r) == "debit_note.stock_line"
    assert "Return items" in r.json()["detail"]["message"]
    assert (await _state(session, auth, note))["status"] == "draft"


async def test_native_scn_refuses_original_from_other_supplier(client, session, auth):
    other = await _bill(client, auth, 200.0, contact_id="supplier:2")
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "debit_note", "contact_id": "supplier:1", "line_items": [_charge(20.0)],
        "original_doc_id": other})
    assert r.status_code == 409, r.text
    assert _code(r) == "debit_note.original_mismatch"


async def test_void_native_scn_reverses_its_entry(client, session, auth):
    note = await _doc(client, auth, "debit_note", [_charge(50.0)])
    await _finalize(client, auth, note)
    before = await _trial_balance(client, auth)
    r = await _post(client, auth, note, "void")
    assert r.status_code == 200, r.text
    assert await journal(session, auth, f"je:auto:{note}:fin") == {}
    assert await _account_net(session, auth["company_id"], AP) == 0.0
    # A note issued by hand comes back as it was.
    r = await _post(client, auth, note, "unvoid")
    assert r.status_code == 200, r.text
    assert await _trial_balance(client, auth) == before


async def test_void_return_created_scn_undoes_the_return(client, session, auth):
    doc, parcel = await _received_bill(client, session, auth, finalize_first=True)
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"],
                          json={"amount": 140.0, "payment_date": "2026-03-02", "bank_account": BANK})
    assert r.status_code == 200, r.text
    books, bill_before = await _trial_balance(client, auth), await _state(session, auth, doc)
    r = await _return(client, auth, doc, parcel, 4)
    assert r.status_code == 200, r.text
    [note] = await supplier_credits(session, auth, doc)
    assert note["total"] == 56.0
    [returned] = (await _state(session, auth, doc))["returned_items"]
    part = returned["returned_lot_id"]

    r = await _post(client, auth, note["id"], "void")
    assert r.status_code == 200, r.text

    # The goods are back in stock, as their own lot, at what they cost.
    lot = await _state(session, auth, part)
    assert (lot["status"], lot["quantity"], goods_basis(lot)) == ("available", 4, 56.0)
    kept = await _state(session, auth, parcel)
    assert (kept["quantity"], goods_basis(kept)) == (6, 84.0)
    # The return's entries are reversed and the books are where they were before it.
    assert await journal(session, auth, f"je:auto:{doc}:rtn") == {}
    assert await _trial_balance(client, auth) == books
    # The bill holds the goods again.
    bill = await _state(session, auth, doc)
    assert not bill.get("returned_items") and not bill.get("returned_credit")
    assert (bill["status"], bill["amount_outstanding"]) == (bill_before["status"], 0.0)
    assert (await _state(session, auth, note["id"]))["status"] == "void"
    session.expire_all()
    undone = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.entity_id == doc,
        LedgerEntry.event_type == "doc.supplier_return_undone"))).scalars().all()
    assert len(undone) == 1
    await assert_settled(client, session, auth)
    # And can go back again.
    r = await _return(client, auth, doc, part, 4)
    assert r.status_code == 200, r.text
    assert len([n for n in await supplier_credits(session, auth, doc) if n["status"] != "void"]) == 1


async def test_void_scn_refused_while_applied_then_works_after_unapply(client, session, auth):
    doc, note = await _note(client, session, auth)
    later = await _bill(client, auth, 200.0)
    assert (await _apply(client, auth, note, later, 50.0)).status_code == 200
    assert (await _refund(client, auth, note, 20.0)).status_code == 200
    later_number = (await _state(session, auth, later))["ref_id"]

    r = await _post(client, auth, note, "void")
    assert r.status_code == 409, r.text
    assert _code(r) == "docs.void_under_supplier_credit"
    assert later_number in r.json()["detail"]["message"] and "refund" in r.json()["detail"]["message"]

    credit = await _state(session, auth, note)
    r = await _post(client, auth, note, "void-payment", {"payment_index": _index(credit, "applied")})
    assert r.status_code == 200, r.text
    r = await _post(client, auth, note, "void")
    assert r.status_code == 409 and _code(r) == "docs.void_under_supplier_credit", r.text
    r = await _post(client, auth, note, "void-payment", {"payment_index": _index(credit, "refund")})
    assert r.status_code == 200, r.text

    r = await _post(client, auth, note, "void")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, later))["amount_outstanding"] == 200.0
    assert (await _state(session, auth, doc))["status"] == "paid"
    await assert_settled(client, session, auth)


async def test_double_void_scn_refused(client, session, auth):
    doc, note = await _note(client, session, auth)
    assert (await _post(client, auth, note, "void")).status_code == 200
    books = await _trial_balance(client, auth)
    r = await _post(client, auth, note, "void")
    assert r.status_code == 409, r.text
    assert _code(r) == "docs.void_already_void"
    assert await _trial_balance(client, auth) == books
    assert len((await _state(session, auth, doc))["received_item_ids"]) == 1


async def test_unvoid_return_created_scn_refused(client, session, auth):
    _doc_id, note = await _note(client, session, auth)
    assert (await _post(client, auth, note, "void")).status_code == 200
    r = await _post(client, auth, note, "unvoid")
    assert r.status_code == 409, r.text
    assert _code(r) == "debit_note.unvoid_return"
    assert (await _state(session, auth, note))["status"] == "void"


async def test_revert_return_created_scn_refused(client, session, auth):
    """The credit is the return's: a draft could be edited or deleted while the bill stays
    settled by it. Void is the way back, and it takes the return back with it."""
    doc, note = await _note(client, session, auth)
    books = await _trial_balance(client, auth)
    r = await _post(client, auth, note, "revert-to-draft")
    assert r.status_code == 409, r.text
    assert _code(r) == "debit_note.revert_return"
    assert (await _state(session, auth, note))["status"] == "final"
    assert (await _state(session, auth, doc))["status"] == "returned"
    assert await _trial_balance(client, auth) == books
    await assert_settled(client, session, auth)


async def test_native_scn_reverts_to_draft(client, session, auth):
    note = await _doc(client, auth, "debit_note", [_charge(20.0)])
    await _finalize(client, auth, note)
    ap = await _account_net(session, auth["company_id"], "2110")
    assert (await _post(client, auth, note, "revert-to-draft")).status_code == 200
    assert (await _state(session, auth, note))["status"] == "draft"
    assert round(await _account_net(session, auth["company_id"], "2110") - ap, 2) == -20.0


async def test_void_bill_refused_while_supplier_credit_open(client, session, auth):
    doc, note = await _note(client, session, auth)
    number = (await _state(session, auth, note))["ref_id"]
    for action in ("void", "revert-to-draft"):
        r = await _post(client, auth, doc, action)
        assert r.status_code == 409, r.text
        assert number in r.json()["detail"]["message"]
    assert (await _state(session, auth, doc))["status"] == "returned"


async def test_debit_note_numbering_and_pdf_title(client, session, auth):
    note = await _doc(client, auth, "debit_note", [_charge(20.0)])
    await _finalize(client, auth, note)
    state = await _state(session, auth, note)
    assert state["ref_id"].startswith("SCN")
    assert _doc_title(state).startswith(f"Supplier Credit Note {state['ref_id']}")


async def test_apply_picker_lists_only_open_same_party_targets(client, session, auth):
    """The bill picker on a supplier credit note is filled by the page itself, with only the
    supplier's open bills in the note's currency: nothing is fetched after the page loads."""
    from test_helpers import ui_get

    doc, note = await _note(client, session, auth)
    later = await _bill(client, auth, 200.0)
    other = await _bill(client, auth, 200.0, contact_id="supplier:2")
    foreign = await _bill(client, auth, 200.0, currency="EUR", conversion_rate=1.1)
    settled = await _bill(client, auth, 50.0)
    await _pay(client, auth, settled, 50.0)

    html = (await ui_get(auth["headers"], f"/docs/{note}")).text
    offered = set(re.findall(r'data-value="(doc:[^"]+)"', html))
    assert later in offered
    assert not offered & {doc, other, foreign, settled}
    assert "open-invoices" not in html


async def test_return_on_paid_bill_names_the_supplier_credit_in_a_notice(client, session, auth, monkeypatch):
    """Sending back goods the bill was already paid for raises a supplier credit note; the
    notice after the return names it and stays until it is closed."""
    import ui.api_client as api_client
    from test_receive_goods_form import _Request, _Routes
    from ui.routes import documents

    doc, parcel = await _received_bill(client, session, auth, finalize_first=True)
    await _pay(client, auth, doc, 140.0)

    async def return_goods(_tok, entity_id, data):
        return api_client._raise(await client.post(
            f"/docs/{entity_id}/return-items", headers=auth["headers"], json=data)).json()

    monkeypatch.setattr(documents, "_token", lambda request: "tok")
    monkeypatch.setattr(api_client, "return_goods", return_goods)
    routes = _Routes()
    documents.setup_routes(routes)
    handler = routes.routes[("post", "/docs/{entity_id}/return-goods")]

    resp = await handler(_Request([("lot_0_0", parcel), ("lot_qty_0_0", "10")]), doc)
    assert resp.status_code == 204, resp.body
    [note] = await supplier_credits(session, auth, doc)
    toast = json.loads(resp.headers["HX-Trigger"])["celerpToast"]
    assert note["ref_id"] in toast["message"]
    assert toast.get("persist") is True


@pytest.mark.parametrize(
    ("credit_rate", "bill_rate", "fx_role", "fx_net"),
    [(1.1, 1.2, "fx_gain", -10.0), (1.2, 1.1, "fx_loss", 10.0)],
)
async def test_supplier_credit_application_clears_each_rate_and_reverses_fx(
    client, session, auth, credit_rate, bill_rate, fx_role, fx_net,
):
    """An application settles AP at the note and bill's respective issue rates."""
    note = await _doc(client, auth, "debit_note", [_charge(100.0)],
                      currency="EUR", conversion_rate=credit_rate)
    await _finalize(client, auth, note)
    bill = await _bill(client, auth, 100.0, currency="EUR", conversion_rate=bill_rate)
    response = await _apply(client, auth, note, bill, 100.0)
    assert response.status_code == 200, response.text
    assert (await _state(session, auth, bill))["amount_outstanding"] == 0.0
    assert (await _state(session, auth, note))["amount_outstanding"] == 0.0
    assert round(await _account_net(session, auth["company_id"], AP), 2) == 0.0
    fx = await _role(session, auth, fx_role)
    assert round(await _account_net(session, auth["company_id"], fx), 2) == fx_net
    # Voiding the paired application restores the exact original entries, including FX.
    credit = await _state(session, auth, note)
    response = await _post(client, auth, note, "void-payment",
                           {"payment_index": _index(credit, "applied")})
    assert response.status_code == 200, response.text
    assert round(await _account_net(session, auth["company_id"], fx), 2) == 0.0
    assert round(await _account_net(session, auth["company_id"], AP), 2) == round(
        100.0 * (credit_rate - bill_rate), 2)


async def test_supplier_credit_rejects_unlinked_contact_on_either_side(client, session, auth):
    note = await _doc(client, auth, "debit_note", [_charge(40.0)])
    await _finalize(client, auth, note)
    unlinked = await _bill(client, auth, 50.0, contact_id=None)
    response = await _apply(client, auth, note, unlinked, 10.0)
    assert response.status_code == 422, response.text
    no_supplier_note = await _doc(client, auth, "debit_note", [_charge(20.0)], contact_id=None)
    await _finalize(client, auth, no_supplier_note)
    linked = await _bill(client, auth, 50.0)
    response = await _apply(client, auth, no_supplier_note, linked, 10.0)
    assert response.status_code == 422, response.text
    assert (await _state(session, auth, unlinked))["amount_outstanding"] == 50.0
    assert (await _state(session, auth, linked))["amount_outstanding"] == 50.0


async def test_credit_target_lookup_reaches_older_open_bills(monkeypatch):
    """An eligible document beyond 500 newer, foreign-currency bills is searchable."""
    from ui.routes import documents

    calls = []
    async def fake_list_docs(_token, params):
        calls.append((params["doc_type"], params["offset"]))
        if params["doc_type"] != "bill":
            return {"items": [], "total": 0}
        start = params["offset"]
        if start >= 501:
            return {"items": [], "total": 501}
        end = min(start + params["limit"], 501)
        rows = [
            {"id": f"doc:{i}", "doc_type": "bill", "contact_id": "supplier:1",
             "currency": "USD" if i == 500 else "EUR", "status": "final",
             "amount_outstanding": 25.0, "ref_id": f"B-{i}"}
            for i in range(start, end)
        ]
        return {"items": rows, "total": 501}

    monkeypatch.setattr(documents.api, "list_docs", fake_list_docs)
    note = {"doc_type": "debit_note", "contact_id": "supplier:1", "currency": "USD"}
    candidates = await documents._credit_target_options("token", note)
    assert [x["id"] for x in candidates] == ["doc:500"]
    assert ("bill", 500) in calls
    assert all(kind != "bill" or offset <= 500 for kind, offset in calls)



async def test_supplier_credit_identity_stays_fixed_after_issue_or_application(client, session, auth):
    """Reassigning either settled document cannot silently move a supplier's credit."""
    original, note = await _note(client, session, auth)
    later = await _bill(client, auth, 200.0)
    response = await _apply(client, auth, note, later, 20.0)
    assert response.status_code == 200, response.text
    before = await _trial_balance(client, auth)

    for doc_id in (original, note, later):
        state = await _state(session, auth, doc_id)
        response = await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json={
            "fields_changed": {"contact_id": {"old": state.get("contact_id"), "new": "supplier:2"}}
        })
        assert response.status_code == 409, (doc_id, response.text)
        assert _code(response) == "docs.contact_credit_fixed"
        assert (await _state(session, auth, doc_id)).get("contact_id") == state.get("contact_id")
    assert await _trial_balance(client, auth) == before

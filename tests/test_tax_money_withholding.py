# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Withholding tax: a share of what is owed that the payer keeps back and pays to the tax
authority instead. Rates are positive and the code's category gives the direction. A code
withheld on the document itself (an invoice, bill or supplier note) stores the withheld amount,
so the document still totals goods plus tax while the amount payable is the total less the
withheld share; a code withheld at payment is recorded with the payment, which then settles the
gross. Each withheld amount keeps what a certificate needs: party, tax id on the period date,
base, rate, nature code, period and the certificate file."""
from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from tax_money_support import (ISSUE, PAID, _moved, code, codes, company, contact, doc, figures, finalized,
                               get, line, pay, post, role_funds, roles, upload)

pytestmark = pytest.mark.asyncio

_ATTACH = "Attach the certificate to this document first"


def _wht(name: str, rate: float, side: str, point: str, **fields) -> dict:
    return code(name, rate, side, "withholding", withholding_point=point, **fields)


async def _page(doc: dict) -> str:
    """The document page as the browser gets it, from the document the API returned."""
    from test_helpers import make_test_token
    from test_ui import _company_stub
    from ui.app import app as ui_app
    company = {"id": "c1", "name": "Test Corp", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01-01"}
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)), \
         patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(company))), \
         patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})), \
         patch("ui.api_client.get_company_category_schemas", new=AsyncMock(return_value={})), \
         patch("ui.api_client.get_units", new=AsyncMock(return_value=[])):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            c.cookies.update({"celerp_token": make_test_token(role="owner")})
            r = await c.get(f"/docs/{doc['id']}")
    assert r.status_code == 200, r.text
    return r.text


def _total_row(html: str, label: str) -> str | None:
    """The figure printed beside ``label`` in the totals panel."""
    found = re.search(rf">\s*{re.escape(label)}\s*</span>\s*<span[^>]*>([^<]+)</span>", html)
    return found.group(1).strip() if found else None


async def _paid_state(client, auth, doc_id: str) -> tuple:
    state = await get(client, auth, doc_id)
    return state["status"], float(state.get("amount_paid") or 0), state


async def test_sales_withholding_on_invoice(client, session):
    auth = await company(session)
    saved = await codes(client, auth, [code("VAT", 7, "sales"), _wht("IRPF", 15, "sales", "document")])
    vat, irpf = saved["VAT"]["id"], saved["IRPF"]["id"]

    before = await roles(session, auth)
    inv = await finalized(client, auth, await doc(client, auth, "invoice", [line(1000.0, vat, irpf)]))
    assert (inv["tax"], inv["total"], inv["withheld"]) == (70.0, 1070.0, 150.0)
    snap = next(t for t in inv["line_items"][0]["taxes"] if t["code_id"] == irpf)
    assert (snap["category"], snap["amount"]) == ("withholding", 150.0)
    issued = await roles(session, auth)
    assert _moved(before, issued) == {"receivable": 920.0, "tax_withheld_credit": 150.0,
                                      "sales_revenue": -1000.0, "tax_output": -70.0}

    html = await _page(inv)
    assert "150.00" in (_total_row(html, "Withheld") or ""), html
    assert "920.00" in (_total_row(html, "Amount payable") or ""), html

    # Withheld on the invoice already, so not again at payment.
    r = await pay(client, auth, inv["id"], 770.0, [{"code_id": irpf, "amount": 150.0}])
    assert r.status_code == 422, r.text
    assert await roles(session, auth) == issued

    r = await pay(client, auth, inv["id"], 920.0)
    assert r.status_code == 200, r.text
    status, paid, _ = await _paid_state(client, auth, inv["id"])
    assert (status, paid) == ("paid", 920.0)

    # A credit note with the same taxes gives back its share of the withheld amount.
    other = await finalized(client, auth, await doc(client, auth, "invoice", [line(1000.0, vat, irpf)]))
    before = await roles(session, auth)
    note = await finalized(client, auth, await doc(client, auth, "credit_note", [line(500.0, vat, irpf)],
                                                   original_doc_id=other["id"]))
    assert note["withheld"] == 75.0
    moved = _moved(before, await roles(session, auth))
    assert (moved["receivable"], moved["tax_withheld_credit"]) == (-460.0, -75.0)


async def test_withholding_certificate_fields(client, session):
    auth = await company(session)
    supplier = await contact(client, auth, "Supplier Co", "vendor")
    customer = await contact(client, auth, "Customer Co", "customer")
    purchase = await codes(client, auth, [_wht("WHT 3", 3, "purchase", "document", report_tags=["WC158"])],
                           "purchase")
    sales = await codes(client, auth, [_wht("IRPF", 15, "sales", "document", report_tags=["irpf.pro"]),
                                       _wht("EWT 2", 2, "sales", "payment", report_tags=["WC160"])])

    for doc_type, party, row in (("bill", supplier, purchase["WHT 3"]), ("invoice", customer, sales["IRPF"])):
        doc_id = await doc(client, auth, doc_type, [line(1000.0, row["id"])], contact_id=party)
        file_id = await upload(client, auth, doc_id)
        state = await finalized(client, auth, doc_id)
        [snap] = state["line_items"][0]["taxes"]
        assert (state["contact_id"], state["issue_date"]) == (party, ISSUE)
        assert (snap["base"], snap["rate"], snap["report_tags"]) == (1000.0, row["versions"][0]["rate"],
                                                                    row["report_tags"])
        assert file_id in [f["id"] for f in state["files"]]

    inv = await finalized(client, auth, await doc(client, auth, "invoice", [line(1000.0)], contact_id=customer))
    file_id = await upload(client, auth, inv["id"])
    ewt = sales["EWT 2"]
    r = await pay(client, auth, inv["id"], 980.0, [{"code_id": ewt["id"], "amount": 20.0,
                                                    "certificate_file_id": file_id}])
    assert r.status_code == 200, r.text
    [payment] = (await get(client, auth, inv["id"]))["payments"]
    assert payment["payment_date"] == PAID
    assert payment["withheld"] == [{"code_id": ewt["id"], "amount": 20.0, "rate": 2.0, "base": 1000.0,
                                    "report_tags": ["WC160"], "certificate_file_id": file_id}]


async def _invoice_1070(client, auth) -> tuple[str, dict]:
    saved = await codes(client, auth, [code("VAT", 7, "sales"), _wht("EWT 2", 2, "sales", "payment")])
    inv = await finalized(client, auth, await doc(client, auth, "invoice", [line(1000.0, saved["VAT"]["id"])]))
    assert inv["total"] == 1070.0
    return inv["id"], saved


async def test_sales_withholding_at_payment_credit_asset(client, session):
    auth = await company(session)
    inv, saved = await _invoice_1070(client, auth)
    before = await roles(session, auth)
    r = await pay(client, auth, inv, 1050.0, [{"code_id": saved["EWT 2"]["id"], "amount": 20.0}])
    assert r.status_code == 200, r.text
    assert _moved(before, await roles(session, auth)) == {"default_deposit": 1050.0, "tax_withheld_credit": 20.0,
                                                          "receivable": -1070.0}
    status, paid, state = await _paid_state(client, auth, inv)
    assert (status, paid) == ("paid", 1070.0)

    r = await post(client, auth, f"/docs/{inv}/void-payment", {"payment_index": state["payments"][0]["index"]})
    assert r.status_code == 200, r.text
    assert _moved(before, await roles(session, auth)) == {}
    assert (await _paid_state(client, auth, inv))[1] == 0.0


async def test_withheld_payment_void_gives_back_each_fund(client, session):
    """With funds on, voiding a payment the customer withheld tax from gives back each fund's
    whole part of it, the withheld share with it, so every fund's books return to before."""
    from test_fund_movements import _setup

    auth = await company(session)
    f = await _setup(client, auth)
    saved = await codes(client, auth, [code("VAT", 7, "sales"), _wht("EWT 2", 2, "sales", "payment")])
    inv = (await finalized(client, auth, await doc(client, auth, "invoice", [
        {**line(1000.0, saved["VAT"]["id"]), "fund_id": f["R"]},
        {**line(500.0, saved["VAT"]["id"]), "fund_id": f["U1"]}])))["id"]
    books = ("receivable", "default_deposit", "tax_withheld_credit")
    before = await role_funds(session, auth, *books)
    r = await pay(client, auth, inv, 1575.0, [{"code_id": saved["EWT 2"]["id"], "amount": 30.0}])
    assert r.status_code == 200, r.text
    assert await role_funds(session, auth, *books) != before
    state = await get(client, auth, inv)
    r = await post(client, auth, f"/docs/{inv}/void-payment", {"payment_index": state["payments"][0]["index"]})
    assert r.status_code == 200, r.text
    assert await role_funds(session, auth, *books) == before


async def test_withheld_above_outstanding_refused(client, session):
    auth = await company(session)
    inv, saved = await _invoice_1070(client, auth)
    before = await roles(session, auth)
    r = await pay(client, auth, inv, 100.0, [{"code_id": saved["EWT 2"]["id"], "amount": 1000.0}])
    assert r.status_code == 422, r.text
    assert {1100.0, 1070.0} <= figures(r.text), r.text
    for withheld in ({"code_id": saved["VAT"]["id"], "amount": 20.0},
                     {"code_id": "u-000000000000", "amount": 20.0},
                     {"code_id": saved["EWT 2"]["id"], "amount": -20.0}):
        r = await pay(client, auth, inv, 1050.0, [withheld])
        assert r.status_code == 422, (withheld, r.text)
    assert await roles(session, auth) == before
    assert not (await get(client, auth, inv)).get("payments")


async def test_withheld_certificate_must_belong_to_document(client, session):
    auth = await company(session)
    inv, saved = await _invoice_1070(client, auth)
    ewt = saved["EWT 2"]["id"]
    elsewhere = await upload(client, auth, await doc(client, auth, "invoice", [line(10.0)]))
    stranger = await company(session)
    foreign = await upload(client, stranger, await doc(client, stranger, "invoice", [line(10.0)]))
    for file_id in (elsewhere, foreign):
        r = await pay(client, auth, inv, 1050.0, [{"code_id": ewt, "amount": 20.0, "certificate_file_id": file_id}])
        assert r.status_code == 422 and _ATTACH in r.text, r.text
    own = await upload(client, auth, inv)
    r = await pay(client, auth, inv, 1050.0, [{"code_id": ewt, "amount": 20.0, "certificate_file_id": own}])
    assert r.status_code == 200, r.text
    [payment] = (await get(client, auth, inv))["payments"]
    assert payment["withheld"][0]["certificate_file_id"] == own


async def test_purchase_wht_above_vat_balances(client, session):
    auth = await company(session)
    saved = await codes(client, auth, [code("Input VAT", 7, "purchase"), _wht("WHT 10", 10, "purchase", "document")],
                        "purchase")
    before = await roles(session, auth)
    bill = await finalized(client, auth, await doc(client, auth, "bill", [
        line(1000.0, saved["Input VAT"]["id"], saved["WHT 10"]["id"])]))
    assert (bill["tax"], bill["total"], bill["withheld"]) == (70.0, 1070.0, 100.0)
    moved = _moved(before, await roles(session, auth))
    assert (moved["payable"], moved["tax_withheld_payable"], moved["tax_input"]) == (-970.0, -100.0, 70.0)
    assert round(sum(moved.values()), 6) == 0
    r = await pay(client, auth, bill["id"], 970.0)
    assert r.status_code == 200, r.text
    assert (await get(client, auth, bill["id"]))["status"] == "paid"


async def test_supplier_credit_note_settles_bill_by_amount_due(client, session):
    auth = await company(session)
    saved = await codes(client, auth, [code("Input VAT", 7, "purchase"), _wht("WHT 3", 3, "purchase", "document")],
                        "purchase")
    taxes = (saved["Input VAT"]["id"], saved["WHT 3"]["id"])
    bill = await finalized(client, auth, await doc(client, auth, "bill", [line(1000.0, *taxes)]))
    assert bill["amount_outstanding"] == 1040.0
    before = await roles(session, auth)
    note = await finalized(client, auth, await doc(client, auth, "debit_note", [line(200.0, *taxes)],
                                                   original_doc_id=bill["id"]))
    assert note["withheld"] == 6.0
    moved = _moved(before, await roles(session, auth))
    assert (moved["payable"], moved["tax_withheld_payable"], moved["tax_input"]) == (208.0, 6.0, -14.0)
    assert (await get(client, auth, bill["id"]))["amount_outstanding"] == 832.0

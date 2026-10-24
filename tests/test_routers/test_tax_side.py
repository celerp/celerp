# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document carries only taxes for its own side.

Withholding tax (TH WHT, PH EWT, ID PPh, IN TDS) is a purchase tax with a negative rate.
On a sale it would be netted into output tax, so a sales document refuses it, naming the
tax, while a purchase bill keeps it as before. A sales-only tax is refused on a purchase.
"""
from __future__ import annotations

import pytest

from celerp.tax_regimes import TAX_REGIMES
from test_money_boundary import _company, _je

_WHT = {"code": "WHT 3%", "rate": -3.0, "order": 1, "is_compound": False}
_VAT = {"code": "VAT 7%", "rate": 7.0, "order": 0, "is_compound": False}


async def _with_taxes(client, session, country: str) -> dict:
    auth = await _company(session, "THB")
    r = await client.patch("/companies/me/taxes", headers=auth["headers"], json={"taxes": TAX_REGIMES[country]["taxes"]})
    assert r.status_code == 200, r.text
    return auth


def _doc(doc_type: str, *, doc_taxes=(), line_taxes=None, line_tax_rate=None) -> dict:
    line = {"description": "Service", "quantity": 1, "unit_price": 1000.0}
    if line_taxes is not None:
        line["taxes"] = line_taxes
    if line_tax_rate is not None:
        line["tax_rate"] = line_tax_rate
    return {"doc_type": doc_type, "line_items": [line], "doc_taxes": list(doc_taxes)}


@pytest.mark.asyncio
@pytest.mark.parametrize("doc_type", ["invoice", "credit_note", "memo", "proforma"])
async def test_a_sales_document_with_withholding_tax_is_refused_naming_the_tax(client, session, doc_type):
    auth = await _with_taxes(client, session, "TH")
    for payload in (_doc(doc_type, doc_taxes=[_VAT, _WHT]), _doc(doc_type, line_taxes=[_WHT])):
        r = await client.post("/docs", headers=auth["headers"], json=payload)
        assert r.status_code == 422, r.text
        assert "WHT 3%" in r.json()["detail"]
        assert "sales document" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_negative_rate_no_tax_list_names_is_refused_on_a_sale(client, session):
    auth = await _with_taxes(client, session, "TH")
    r = await client.post("/docs", headers=auth["headers"], json=_doc("invoice", line_tax_rate=-3.0))
    assert r.status_code == 422, r.text
    r = await client.post("/docs", headers=auth["headers"],
                          json=_doc("invoice", doc_taxes=[{**_WHT, "code": "Withheld"}]))
    assert r.status_code == 422 and "Withheld" in r.json()["detail"]


@pytest.mark.asyncio
async def test_withholding_added_to_a_draft_invoice_is_refused_and_the_draft_keeps_its_taxes(client, session):
    auth = await _with_taxes(client, session, "TH")
    r = await client.post("/docs", headers=auth["headers"], json=_doc("invoice", doc_taxes=[_VAT]))
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.patch(f"/docs/{doc_id}", headers=auth["headers"],
                           json={"fields_changed": {"doc_taxes": {"new": [_VAT, _WHT]}}})
    assert r.status_code == 422 and "WHT 3%" in r.json()["detail"]
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert [t["code"] for t in doc["doc_taxes"]] == ["VAT 7%"]
    assert doc["total"] == pytest.approx(1070.0)


@pytest.mark.asyncio
async def test_a_bill_with_withholding_tax_reduces_what_is_payable_and_finalizes(client, session):
    auth = await _with_taxes(client, session, "TH")
    r = await client.post("/docs", headers=auth["headers"], json=_doc("bill", doc_taxes=[_VAT, _WHT]))
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert {t["code"]: t["amount"] for t in doc["doc_taxes"]} == {"VAT 7%": 70.0, "WHT 3%": -30.0}
    assert doc["total"] == pytest.approx(1040.0)
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_sales_only_tax_is_refused_on_a_purchase_bill(client, session):
    auth = await _with_taxes(client, session, "MY")
    r = await client.post("/docs", headers=auth["headers"],
                          json=_doc("bill", line_taxes=[{"code": "Sales Tax 10%", "rate": 10.0, "order": 0, "is_compound": False}]))
    assert r.status_code == 422, r.text
    assert "Sales Tax 10%" in r.json()["detail"] and "purchase document" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_sale_whose_taxes_come_to_less_than_zero_does_not_finalize(client, session):
    """A sales document saved with a negative tax before sides were checked never posts
    a negative output tax line: finalize is refused and no entry is written."""
    auth = await _with_taxes(client, session, "TH")
    r = await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "line_items": [{"description": "Service", "quantity": 1, "unit_price": 1000.0}],
        "subtotal": 1000.0, "tax": -30.0, "total": 970.0})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])
    assert r.status_code == 409, r.text
    assert "less than zero" in r.json()["detail"]
    doc = (await client.get(f"/docs/{doc_id}", headers=auth["headers"])).json()
    assert doc["status"] == "draft"


@pytest.mark.asyncio
async def test_a_no_tax_invoice_posts_no_zero_lines(client, session):
    auth = await _with_taxes(client, session, "TH")
    r = await client.post("/docs", headers=auth["headers"], json=_doc("invoice"))
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.post(f"/docs/{doc_id}/finalize", headers=auth["headers"])).status_code == 200
    lines = await _je(session, auth, f"je:auto:{doc_id}:fin")
    assert lines and all(d or c for _, d, c in lines), lines
    assert sum(d for _, d, _ in lines) == pytest.approx(sum(c for _, _, c in lines)) == pytest.approx(1000.0)


def test_each_document_offers_only_taxes_for_its_side():
    """The tax picker and the taxes a new document starts with list only taxes for the
    document's side: no withholding on a sale, no sales-only tax on a purchase."""
    from fasthtml.common import to_xml

    from ui.routes.documents import _doc_detail

    th = TAX_REGIMES["TH"]["taxes"]

    def picker(doc_type: str) -> str:
        doc = {"entity_id": "doc:1", "doc_type": doc_type, "status": "draft", "ref_id": "D-1",
               "line_items": [{"description": "Service", "quantity": 1, "unit_price": 1000.0}]}
        return to_xml(_doc_detail(doc, company_taxes=th))

    assert "WHT 3%" not in picker("invoice") and "VAT 7%" in picker("invoice")
    assert "WHT 3%" in picker("bill")

    from celerp.tax_regimes import taxes_for

    assert [t["name"] for t in taxes_for(th, "invoice")] == ["VAT 7%", "Exempt"]
    assert [t["name"] for t in taxes_for(th, "bill")] == ["VAT 7%", "Exempt", "WHT 3%", "WHT 5%"]
    assert "Sales Tax 10%" not in [t["name"] for t in taxes_for(TAX_REGIMES["MY"]["taxes"], "bill")]


def test_a_new_line_starts_with_the_default_tax_for_its_side():
    """A new line takes its document side's default tax: never a withholding tax on a sale or a purchase."""
    from celerp.tax_regimes import default_line_taxes

    th = TAX_REGIMES["TH"]["taxes"]
    assert [t["code"] for t in default_line_taxes(th, "invoice", doc=None)] == ["VAT 7%"]
    assert [t["code"] for t in default_line_taxes(th, "bill", doc=None)] == ["VAT 7%"]

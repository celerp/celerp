# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every tax line keeps to its document's side wherever a document's entry is posted, not
only at the routes that issue it: a sale never posts withholding tax netted against its
output tax, a supplier document never posts a sales-only tax, and unvoiding a document
issues it again under the same rule."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.projections import Projection
from celerp.services import auto_je

pytestmark = pytest.mark.asyncio

_SALES = [{"name": "Output VAT", "rate": 7.0, "tax_type": "sales", "is_default": True}]
_PURCHASING = [{"name": "Input VAT", "rate": 10.0, "tax_type": "purchase", "is_default": True},
               {"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False}]
_VAT = {"code": "Output VAT", "rate": 7.0, "order": 0}
_WHT = {"code": "WHT 3%", "rate": -3.0, "order": 1}


async def _company(client) -> tuple[dict, uuid.UUID]:
    r = await client.post("/auth/register", json={
        "company_name": "Posted Taxes Co", "email": f"pt-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": _SALES})).status_code == 200
    r = await client.patch("/companies/me/purchasing-taxes", headers=h, json={"taxes": _PURCHASING})
    assert r.status_code == 200, r.text
    return h, uuid.UUID((await client.get("/companies/me", headers=h)).json()["id"])


async def _entries(session) -> list[str]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(Projection.entity_type == "journal_entry"))).scalars().all()
    return sorted(r.entity_id for r in rows)


def _doc(doc_type: str, taxes, tax: float, **extra) -> dict:
    return {"doc_type": doc_type, "status": "final", "issue_date": "2026-10-01", "currency": "USD",
            "line_items": [{"description": "Service", "quantity": 1, "unit_price": 1000.0, "line_total": 1000.0,
                            "taxes": [dict(x) for x in taxes]}],
            "subtotal": 1000.0, "tax": tax, "total": 1000.0 + tax, **extra}


async def _issued_invoice(client, h) -> str:
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"description": "Service", "quantity": 1, "unit_price": 1000.0, "taxes": [_VAT]}]})
    assert r.status_code == 200, r.text
    assert (await client.post(f"/docs/{r.json()['id']}/finalize", headers=h)).status_code == 200
    return r.json()["id"]


async def test_a_sale_posting_withholding_netted_into_its_output_tax_is_refused(client, session):
    """70 output VAT and -30 withholding come to 40, above zero, so the total alone passes:
    the withholding line is what the posting refuses."""
    _, company_id = await _company(client)
    before = await _entries(session)
    with pytest.raises(HTTPException) as refused:
        await auto_je.create_for_doc_finalized(session, company_id=company_id, user_id=None, doc_id="doc:post-inv",
                                               doc=_doc("invoice", (_VAT, _WHT), 40.0), base_currency="USD")
    assert refused.value.status_code == 422 and "WHT 3%" in str(refused.value.detail)
    assert await _entries(session) == before


async def test_a_customer_credit_note_posting_withholding_is_refused(client, session):
    h, company_id = await _company(client)
    invoice = await _issued_invoice(client, h)
    before = await _entries(session)
    with pytest.raises(HTTPException) as refused:
        await auto_je.create_for_credit_note_finalized(
            session, company_id=company_id, user_id=None, doc_id="doc:post-cn",
            doc=_doc("credit_note", (_VAT, _WHT), 40.0, original_doc_id=invoice), base_currency="USD")
    assert refused.value.status_code == 422 and "WHT 3%" in str(refused.value.detail)
    assert await _entries(session) == before


async def test_a_supplier_credit_note_posting_a_sales_tax_from_its_stored_totals_is_refused(client, session):
    """The supplier credit note's entry takes its tax from the stored total, which says
    nothing of the side: the posting reads the side off each tax line."""
    _, company_id = await _company(client)
    before = await _entries(session)
    with pytest.raises(HTTPException) as refused:
        await auto_je.create_for_debit_note_finalized(session, company_id=company_id, user_id=None,
                                                      doc_id="doc:post-dn", doc=_doc("debit_note", (_VAT,), 70.0),
                                                      base_currency="USD")
    assert refused.value.status_code == 422 and "Output VAT" in str(refused.value.detail)
    assert await _entries(session) == before


async def test_a_bill_posting_a_sales_tax_is_refused(client, session):
    _, company_id = await _company(client)
    before = await _entries(session)
    with pytest.raises(HTTPException) as refused:
        await auto_je.create_for_bill_conversion(session, company_id=company_id, user_id=None,
                                                 doc_id="doc:post-bill", doc=_doc("bill", (_VAT,), 70.0),
                                                 base_currency="USD")
    assert refused.value.status_code == 422 and "Output VAT" in str(refused.value.detail)
    assert await _entries(session) == before


async def test_documents_with_their_own_side_taxes_still_post(client, session):
    _, company_id = await _company(client)
    await auto_je.create_for_doc_finalized(session, company_id=company_id, user_id=None, doc_id="doc:ok-inv",
                                           doc=_doc("invoice", (_VAT,), 70.0), base_currency="USD")
    await auto_je.create_for_bill_conversion(session, company_id=company_id, user_id=None, doc_id="doc:ok-bill",
                                             doc=_doc("bill", ({"code": "Input VAT", "rate": 10.0},), 100.0),
                                             base_currency="USD")
    posted = await _entries(session)
    assert "je:auto:doc:ok-inv:fin" in posted and "je:auto:doc:ok-bill:bill" in posted


async def test_unvoiding_a_credit_note_checks_its_taxes_again(client, session):
    """A credit note voided while its tax was a sales tax, and unvoided after the company
    moved that tax to purchases only, would issue a purchase tax on a sale: the unvoid is
    refused and the note stays void."""
    h, _ = await _company(client)
    invoice = await _issued_invoice(client, h)
    r = await client.post("/docs", headers=h, json={"doc_type": "credit_note", "original_doc_id": invoice,
                                                     "line_items": [{"description": "Service", "quantity": 1,
                                                                     "unit_price": 100.0, "taxes": [_VAT]}]})
    assert r.status_code == 200, r.text
    cn = r.json()["id"]
    assert (await client.post(f"/docs/{cn}/finalize", headers=h, json={})).status_code == 200
    assert (await client.post(f"/docs/{cn}/void", headers=h, json={})).status_code == 200
    moved = {**_SALES[0], "tax_type": "purchase", "is_default": False}
    assert (await client.patch("/companies/me/purchasing-taxes", headers=h,
                               json={"taxes": [*_PURCHASING, moved]})).status_code == 200
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": [
        {"name": "Sales VAT", "rate": 7.0, "tax_type": "sales", "is_default": True}]})).status_code == 200
    before = await _entries(session)
    r = await client.post(f"/docs/{cn}/unvoid", headers=h, json={})
    assert r.status_code == 422 and "Output VAT" in r.text, r.text
    assert (await client.get(f"/docs/{cn}", headers=h)).json()["status"] == "void"
    assert await _entries(session) == before

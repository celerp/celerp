# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A document is issued only with the taxes of its own side, whatever made it.

Creating or editing a document already refuses a tax from the other side. An import, a
conversion or a subscription can still hand finalize a document that carries one, and an
import can bring one in already issued. Finalizing and importing an issued document check
the same rule: a sale or a customer credit note never carries withholding tax or any tax
that comes to less than zero, and a supplier document never carries a sales-only tax."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

_SALES = [{"name": "Output VAT", "rate": 7.0, "tax_type": "sales", "is_default": True}]
_PURCHASING = [{"name": "Input VAT", "rate": 10.0, "tax_type": "purchase", "is_default": True},
               {"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False}]
_VAT = {"code": "Output VAT", "rate": 7.0, "order": 0}
_WHT = {"code": "WHT 3%", "rate": -3.0, "order": 1}


async def _company(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Issued Taxes Co", "email": f"it-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": _SALES})).status_code == 200
    r = await client.patch("/companies/me/purchasing-taxes", headers=h, json={"taxes": _PURCHASING})
    assert r.status_code == 200, r.text
    return h


async def _import(client, h, data: dict):
    eid = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import", headers=h, json={
        "entity_id": eid, "event_type": "doc.created", "source": "csv", "idempotency_key": uuid.uuid4().hex,
        "data": data})
    return eid, r


def _doc(doc_type: str, taxes, status: str = "draft", **extra) -> dict:
    return {"doc_type": doc_type, "status": status, "issue_date": "2026-10-01",
            "line_items": [{"description": "Service", "quantity": 1, "unit_price": 1000.0, "line_total": 1000.0,
                            "taxes": [dict(x) for x in taxes]}], **extra}


async def _entries(session) -> list[str]:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(Projection.entity_type == "journal_entry"))).scalars().all()
    return [r.entity_id for r in rows]


async def _invoice(client, h) -> str:
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": [
        {"description": "Service", "quantity": 1, "unit_price": 1000.0, "taxes": [_VAT]}]})
    assert r.status_code == 200, r.text
    f = await client.post(f"/docs/{r.json()['id']}/finalize", headers=h)
    assert f.status_code == 200, f.text
    return r.json()["id"]


def _refused_for(r, name: str) -> None:
    assert r.status_code == 422, r.text
    assert name in r.text, r.text


async def test_an_imported_draft_sale_with_withholding_tax_is_refused_at_finalize(client, session):
    h = await _company(client)
    eid, r = await _import(client, h, _doc("invoice", (_VAT, _WHT)))
    assert r.status_code == 200, r.text
    before = await _entries(session)
    _refused_for(await client.post(f"/docs/{eid}/finalize", headers=h), "WHT 3%")
    doc = (await client.get(f"/docs/{eid}", headers=h)).json()
    assert doc["status"] == "draft" and not doc.get("finalized")
    assert await _entries(session) == before


async def test_a_quotation_converted_with_withholding_tax_never_finalizes(client, session):
    h = await _company(client)
    eid, r = await _import(client, h, _doc("quotation", (_VAT, _WHT)))
    assert r.status_code == 200, r.text
    c = await client.post(f"/docs/{eid}/convert", headers=h, json={})
    assert c.status_code == 200, c.text
    invoice = c.json()["target_doc_id"]
    before = await _entries(session)
    _refused_for(await client.post(f"/docs/{invoice}/finalize", headers=h), "WHT 3%")
    assert await _entries(session) == before


async def test_a_subscription_with_withholding_tax_generates_nothing(client, session):
    h = await _company(client)
    eid, r = await _import(client, h, {**_doc("subscription_invoice", (_VAT, _WHT)), "name": "Monthly",
                                       "frequency": "monthly"})
    assert r.status_code == 200, r.text
    before = await _entries(session)
    _refused_for(await client.post(f"/subscriptions/{eid}/generate", headers=h, json={}), "WHT 3%")
    assert await _entries(session) == before
    invoices = (await client.get("/docs", headers=h, params={"doc_type": "invoice"})).json()["items"]
    assert [d for d in invoices if d.get("finalized") or d.get("status") != "draft"] == []


async def test_an_issued_sale_imported_with_withholding_tax_is_refused(client, session):
    h = await _company(client)
    before = await _entries(session)
    eid, r = await _import(client, h, _doc("invoice", (_VAT, _WHT), status="final", subtotal=1000.0, tax=40.0,
                                           total=1040.0, amount_paid=0.0, amount_outstanding=1040.0))
    _refused_for(r, "WHT 3%")
    assert (await client.get(f"/docs/{eid}", headers=h)).status_code == 404
    assert await _entries(session) == before


async def test_an_issued_credit_note_imported_with_withholding_tax_is_refused(client, session):
    h = await _company(client)
    invoice = await _invoice(client, h)
    before = await _entries(session)
    eid, r = await _import(client, h, {**_doc("credit_note", (_WHT,), status="final"), "original_doc_id": invoice,
                                       "subtotal": 1000.0, "tax": -30.0, "total": 970.0, "amount_paid": 0.0,
                                       "amount_outstanding": 970.0})
    _refused_for(r, "WHT 3%")
    assert (await client.get(f"/docs/{eid}", headers=h)).status_code == 404
    assert await _entries(session) == before


async def test_an_issued_credit_note_whose_tax_is_below_zero_is_refused(client, session):
    """No rate to read the side from: the tax amount itself is below zero."""
    h = await _company(client)
    invoice = await _invoice(client, h)
    before = await _entries(session)
    eid, r = await _import(client, h, {"doc_type": "credit_note", "status": "final", "issue_date": "2026-10-01",
                                       "original_doc_id": invoice, "line_items": [
                                           {"description": "Service", "quantity": 1, "unit_price": 1000.0,
                                            "line_total": 1000.0}],
                                       "subtotal": 1000.0, "tax": -30.0, "total": 970.0, "amount_paid": 0.0,
                                       "amount_outstanding": 970.0})
    assert r.status_code in (409, 422), r.text
    assert (await client.get(f"/docs/{eid}", headers=h)).status_code == 404
    assert await _entries(session) == before


async def test_an_issued_bill_imported_with_a_sales_tax_is_refused(client, session):
    h = await _company(client)
    before = await _entries(session)
    eid, r = await _import(client, h, _doc("bill", (_VAT,), status="awaiting_payment", subtotal=1000.0, tax=70.0,
                                           total=1070.0, amount_paid=0.0, amount_outstanding=1070.0,
                                           import_treatment="record_now"))
    _refused_for(r, "Output VAT")
    assert await _entries(session) == before


async def test_a_sale_with_its_own_taxes_still_imports_and_finalizes(client, session):
    h = await _company(client)
    eid, r = await _import(client, h, _doc("invoice", (_VAT,)))
    assert r.status_code == 200, r.text
    f = await client.post(f"/docs/{eid}/finalize", headers=h)
    assert f.status_code == 200, f.text
    eid, r = await _import(client, h, _doc("invoice", (_VAT,), status="final", subtotal=1000.0, tax=70.0,
                                           total=1070.0, amount_paid=0.0, amount_outstanding=1070.0))
    assert r.status_code == 200, r.text

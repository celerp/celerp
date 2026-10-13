"""Peer cash receipts use existing document accounting, never snapshot replacement."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.accounting import UserCompany
from celerp.services.peer_payments import apply_source_payments, validate_source_payments
from test_payments_stripe import _register, _payable_invoice, _h, _company_id

pytestmark = pytest.mark.usefixtures("docs_running")


def _source_payment(index=0, amount=200.0):
    return {"index": index, "amount": amount, "payment_date": "2026-10-12",
            "currency": "USD", "bank_account": "1111", "method": "bank",
            "reference": "peer-bank-1", "status": "active"}


@pytest.mark.parametrize("status", ["paid", "partial"])
def test_settled_source_requires_real_receipts(status):
    with pytest.raises(HTTPException) as err:
        validate_source_payments({
            "status": status, "currency": "USD", "amount_paid": 200, "payments": []})
    assert err.value.status_code == 409


@pytest.mark.parametrize("change", [
    {"status": "voided"}, {"refunded": 1}, {"bank_account": None},
    {"index": True}, {"amount": -5}, {"payment_date": "not-a-date"},
])
def test_unsupported_or_invalid_cash_does_not_cross_domain(change):
    payment = {**_source_payment(), **change}
    with pytest.raises((HTTPException, ValueError, TypeError)):
        validate_source_payments({
            "status": "partial", "currency": "USD", "amount_paid": 200,
            "payments": [payment]})


@pytest.mark.asyncio
async def test_real_canonical_payment_posting_and_replay(client, session):
    tok = await _register(client)
    doc_id, _ = await _payable_invoice(client, tok)
    company = uuid.UUID(_company_id(tok))
    source = uuid.uuid4()
    grant = SimpleNamespace(company_id=company, source_company_id=source)
    owner = (await session.execute(select(UserCompany.user_id).where(
        UserCompany.company_id == company, UserCompany.role == "owner"))).scalar_one()
    source_payment = _source_payment()
    parsed = validate_source_payments({
        "status": "partial", "currency": "USD", "amount_paid": 200,
        "payments": [source_payment]})
    await apply_source_payments(
        session, grant=grant, source_entity_id="doc:source-1",
        target_doc_id=doc_id, payments=parsed,
        actor_id=owner, source_status="partial")
    await session.commit()
    state = (await client.get(f"/docs/{doc_id}", headers=_h(tok))).json()
    assert state["status"] == "partial" and state["amount_paid"] == 200
    assert len(state["payments"]) == 1
    await apply_source_payments(
        session, grant=grant, source_entity_id="doc:source-1",
        target_doc_id=doc_id, payments=parsed,
        actor_id=owner, source_status="partial")
    await session.commit()
    again = (await client.get(f"/docs/{doc_id}", headers=_h(tok))).json()
    assert len(again["payments"]) == 1 and again["amount_paid"] == 200
    conflicting = validate_source_payments({
        "status": "partial", "currency": "USD", "amount_paid": 201,
        "payments": [{**source_payment, "amount": 201}]})
    with pytest.raises(HTTPException) as reused:
        await apply_source_payments(
            session, grant=grant, source_entity_id="doc:source-1",
            target_doc_id=doc_id, payments=conflicting,
            actor_id=owner, source_status="partial")
    assert reused.value.status_code == 409

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



@pytest.mark.asyncio
async def test_refund_events_use_original_books_and_idempotent_canonical_reversal(client, session):
    from celerp.services.peer_refunds import replay_source_refunds
    from celerp.services.peer_payments import validate_source_refunds
    tok = await _register(client)
    doc_id, _ = await _payable_invoice(client, tok)
    company = uuid.UUID(_company_id(tok))
    grant = SimpleNamespace(company_id=company, source_company_id=uuid.uuid4())
    actor = (await session.execute(select(UserCompany.user_id).where(
        UserCompany.company_id == company, UserCompany.role == "owner"))).scalar_one()
    payment = _source_payment()
    parsed = validate_source_payments({
        "status": "partial", "currency": "USD", "amount_paid": 200,
        "payments": [payment]})
    await apply_source_payments(
        session, grant=grant, source_entity_id="doc:remote", target_doc_id=doc_id,
        payments=parsed, actor_id=actor, source_status="partial")
    event = {"event_id": 105, "payment_index": 0, "refund_number": 0,
             "amount": 50, "refund_date": "2026-10-13"}
    events = validate_source_refunds({"currency": "USD", "payment_events": [event]})
    await replay_source_refunds(session, grant=grant, source_doc_id="doc:remote",
                                target_doc_id=doc_id, refunds=events, actor_id=actor)
    await session.commit()
    state = (await client.get(f"/docs/{doc_id}", headers=_h(tok))).json()
    assert state["amount_paid"] == 150
    assert state["payments"][0]["refunded"] == 50
    await replay_source_refunds(session, grant=grant, source_doc_id="doc:remote",
                                target_doc_id=doc_id, refunds=events, actor_id=actor)
    await session.commit()
    assert (await client.get(f"/docs/{doc_id}", headers=_h(tok))).json()["amount_paid"] == 150
    with pytest.raises(HTTPException) as conflict:
        await replay_source_refunds(
            session, grant=grant, source_doc_id="doc:remote", target_doc_id=doc_id,
            refunds=[{**event, "amount": 55}], actor_id=actor)
    assert conflict.value.status_code == 409


def test_refund_history_validates_identity_and_effective_balance():
    from celerp.services.peer_payments import validate_source_refunds
    event = {"event_id": 9, "payment_index": 0, "refund_number": 0,
             "amount": 50, "refund_date": "2026-10-12"}
    assert validate_source_refunds({"payment_events": [event]}) == [event]
    with pytest.raises(HTTPException):
        validate_source_refunds({"payment_events": [event, event]})
    with pytest.raises(HTTPException):
        validate_source_refunds({"payment_events": [{**event, "amount": -10}]})
    with pytest.raises(HTTPException):
        validate_source_refunds({"payment_events": [{**event, "refund_date": "not-a-date"}]})
    payment = {**_source_payment(), "refunded": 50}
    verified = validate_source_payments({
        "status": "partial", "currency": "USD", "amount_paid": 150,
        "payments": [payment], "payment_events": [event]})
    assert verified[0][1]["amount"] == 200
    with pytest.raises(HTTPException):
        validate_source_payments({
            "status": "partial", "currency": "USD", "amount_paid": 200,
            "payments": [payment], "payment_events": [event]})

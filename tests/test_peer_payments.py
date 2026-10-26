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


@pytest.mark.parametrize("source_rate,target_rate,expected", [
    (1.25, 1.25, True),
    (1.25, 1.30, False),
    (1.25, None, False),
    (None, 1.25, False),
    (None, None, True),
])
def test_existing_payment_exchange_rate_must_match(source_rate, target_rate, expected):
    from celerp.services.peer_payments import _same_payment

    payment = _source_payment()
    payment.pop("status")
    payment["conversion_rate"] = source_rate
    stored = {**payment, "conversion_rate": target_rate}
    assert _same_payment(stored, payment, "USD") is expected


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



@pytest.mark.asyncio
async def test_canonical_refund_reversal_replays_once_and_keeps_receivable_consistent(client, session, monkeypatch):
    from celerp.models.ledger import LedgerEntry
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
        session, grant=grant, source_entity_id="doc:source-reversed",
        target_doc_id=doc_id, payments=parsed, actor_id=actor, source_status="partial")
    history = validate_source_refunds({"currency": "USD", "payment_events": [
        {"kind": "refunded", "event_id": 110, "payment_index": 0,
         "refund_number": 0, "amount": 50, "refund_date": "2026-10-12"},
        {"kind": "reversed", "event_id": 111, "payment_index": 0,
         "refund_number": 0, "amount": 50, "reversal_date": "2026-10-13"},
    ]})
    for _ in range(2):
        await replay_source_refunds(
            session, grant=grant, source_doc_id="doc:source-reversed",
            target_doc_id=doc_id, refunds=history, actor_id=actor)
        await session.commit()
    state = (await client.get(f"/docs/{doc_id}", headers=_h(tok))).json()
    assert state["amount_paid"] == 200
    assert state["payments"][0]["refunded"] == 0
    assert state["payments"][0]["refund_count"] == 1
    rows = (await session.scalars(select(LedgerEntry).where(
        LedgerEntry.company_id == company, LedgerEntry.entity_id == doc_id,
        LedgerEntry.event_type.in_(("doc.payment.refunded", "doc.payment.refund_reversed")),
    ).order_by(LedgerEntry.id))).all()
    assert [x.event_type for x in rows] == [
        "doc.payment.refunded", "doc.payment.refund_reversed"]
    assert rows[1].metadata_["request"]
    from contextlib import asynccontextmanager
    from celerp import db
    from celerp.connectors.celerp import _read_documents
    @asynccontextmanager
    async def test_session():
        yield session
    monkeypatch.setattr(db, "get_session_ctx", test_session)
    page = await _read_documents(company, "")
    copy = next(state for entity_id, _, state in page if entity_id == doc_id)
    assert [event.get("kind", "refunded") for event in copy["payment_events"]] == [
        "refunded", "reversed"]
    assert copy["payment_events"][1]["reversal_date"] == "2026-10-13"
    with pytest.raises(HTTPException) as reused:
        await replay_source_refunds(
            session, grant=grant, source_doc_id="doc:source-reversed",
            target_doc_id=doc_id, refunds=[{**history[1], "reversal_date": "2026-10-14"}],
            actor_id=actor)
    assert reused.value.status_code == 409


@pytest.mark.parametrize("events", [
    [{"kind": "reversed", "event_id": 2, "payment_index": 0,
      "refund_number": 0, "amount": 50, "reversal_date": "2026-10-13"}],
    [{"kind": "refunded", "event_id": 1, "payment_index": 0,
      "refund_number": 0, "amount": 50, "refund_date": "2026-10-12"},
     {"kind": "reversed", "event_id": 2, "payment_index": 0,
      "refund_number": 0, "amount": 40, "reversal_date": "2026-10-13"}],
    [{"kind": "refunded", "event_id": 1, "payment_index": 0,
      "refund_number": 0, "amount": 50, "refund_date": "2026-10-12"},
     {"kind": "reversed", "event_id": 2, "payment_index": 0,
      "refund_number": 0, "amount": 50, "reversal_date": "2026-10-13"},
     {"kind": "reversed", "event_id": 3, "payment_index": 0,
      "refund_number": 0, "amount": 50, "reversal_date": "2026-10-13"}],
])
def test_refund_history_fails_closed_on_unmatched_or_duplicate_reversal(events):
    from celerp.services.peer_payments import validate_source_refunds
    with pytest.raises(HTTPException):
        validate_source_refunds({"currency": "USD", "payment_events": events})



def test_peer_financial_status_must_match_net_source_receipts():
    receipt = _source_payment()
    base = {"currency": "USD", "status": "final", "payments": [receipt],
            "amount_paid": 200}
    with pytest.raises(HTTPException, match="status conflicts"):
        validate_source_payments(base)
    with pytest.raises(HTTPException, match="paid balance requires reconciliation"):
        validate_source_payments({**base, "amount_paid": 0})
    with pytest.raises(HTTPException, match="paid balance unavailable"):
        validate_source_payments({k: v for k, v in base.items() if k != "amount_paid"})
    with pytest.raises(HTTPException, match="no outstanding receipt"):
        validate_source_payments({**base, "status": "partial", "amount_paid": 0,
                                  "payments": [{**receipt, "refunded": 200}],
                                  "payment_events": [{"event_id": 1}]})

    fully_refunded = {**base, "amount_paid": 0,
                      "payments": [{**receipt, "refunded": 200}],
                      "payment_events": [{"event_id": 1}]}
    assert validate_source_payments(fully_refunded)[0][1]["amount"] == 200
    assert validate_source_payments({
        "currency": "USD", "status": "final", "payments": [], "amount_paid": 0}) == []


@pytest.mark.parametrize("side", [
    {"method": "credit_note", "source_doc_id": "doc:source-note"},
    {"method": "applied", "target_doc_id": "doc:source-invoice"},
])
def test_a_credit_applied_between_documents_is_entered_by_hand(side):
    payment = {"index": 1, "amount": 30, "payment_date": "2026-10-12", "currency": "USD",
               "paired_index": 0, "status": "active", **side}
    with pytest.raises(HTTPException) as err:
        validate_source_payments({"status": "paid", "currency": "USD", "amount_paid": 30,
                                  "payments": [payment]})
    assert err.value.status_code == 409
    assert err.value.detail == ("A credit note applied to a document is not copied automatically. "
                                "Enter it in the accountant's copy by hand.")

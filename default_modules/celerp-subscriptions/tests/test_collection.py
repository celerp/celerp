# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Automatic card collection on a sales agreement (Celerp Cloud collection protocol 1).

Initial conditions per test: a newly registered company (its owner holds every permission)
and a payer contact with an email address. Celerp Cloud is a stand-in (``cloud``) that records
each request the app sends it and answers what the test sets; its deliveries (collection.binding,
collection.attempt, invoice.payment) are handed to the app's delivery handlers directly, whose
own sessions join the test's transaction. The collection protocol flag is 1 unless a test
withdraws it. The business day is frozen per test.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

from agreement_kit import (
    agreement, billed_docs, company_id, contact, freeze_day, member, register, revoke, schedule, template, tick,
)

pytestmark = pytest.mark.asyncio

LINK = "https://cloud.test/consent/tok-1"


class _Cloud:
    """Celerp Cloud as the app sees it: ``calls`` holds (route, payload) per request; ``answers``
    maps a route's last segment to the (status, body) it answers, None for no readable answer,
    or a callable of the payload."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.answers: dict = {
            "setup-link": (200, {"link": LINK, "emailed": True}),
            "attempt": lambda p: (200, {"state": "submitted", "attempt_number": p["attempt_number"],
                                        "failure_code": None, "retry_eligible_at": None}),
            "cancel": (200, {"state": "canceled"}),
            "revoke": (200, {"state": "revoked"}),
        }

    async def __call__(self, path: str, payload: dict):
        self.calls.append((path, payload))
        answer = self.answers[path.rsplit("/", 1)[-1]]
        return answer(payload) if callable(answer) else answer

    def sent(self, route: str) -> list[dict]:
        return [p for path, p in self.calls if path == f"/billing/connect/collection/{route}"]


@pytest.fixture
def cloud(monkeypatch, session):
    from sqlalchemy.ext.asyncio import AsyncSession
    from celerp.gateway import state
    from celerp.services import collection

    saved = state.get_feature_flags()
    state.set_feature_flags({**saved, "payments_enabled": True, "collection_protocol": 1})
    monkeypatch.setattr("celerp.services.payments._own_session", lambda: AsyncSession(
        bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"))
    fake = _Cloud()
    monkeypatch.setattr(collection, "_ask", fake)
    yield fake
    state.set_feature_flags(saved)


def _flag(value) -> None:
    from celerp.gateway import state
    flags = {k: v for k, v in state.get_feature_flags().items() if k != "collection_protocol"}
    state.set_feature_flags({**flags, **({} if value is None else {"collection_protocol": value})})


def _says(r, key: str, **params) -> bool:
    detail = r.json().get("detail")
    return isinstance(detail, dict) and detail.get("message_key") == key and (
        not params or detail.get("params") == params)


async def _bind(cid: str, payer: str, state: str = "active", **fields) -> bool:
    """Cloud's collection.binding delivery (X5) for ``payer``."""
    from celerp.services import collection
    return await collection.receive_binding({
        "delivery_id": uuid.uuid4().hex, "company_id": cid, "payer_contact_id": payer, "state": state,
        "card_brand": "visa", "card_last4": "4242", "receipt_sent": True, **fields})


async def _attempt_outcome(cid: str, doc_id: str, n: int, state: str, **fields) -> bool:
    """Cloud's collection.attempt delivery (X4) for attempt ``n`` of ``doc_id``."""
    from celerp.services import collection
    return await collection.receive_attempt({
        "delivery_id": uuid.uuid4().hex, "company_id": cid, "entity_id": doc_id, "attempt_number": n,
        "state": state, "definitive": state in ("failed", "canceled"), "failure_code": None,
        "retry_eligible_at": None, "action_url": None, **fields})


async def _payer(client, hd, email: str | None = "payer@example.test") -> str:
    r = await client.post("/crm/contacts", json={"name": "Payer", **({"email": email} if email else {})}, headers=hd)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _collecting(client, hd, *, start: str = "2026-07-01") -> tuple[str, str, str]:
    """An agreement on Bill and collect automatically whose payer has a card on file:
    (company id, agreement id, payer id)."""
    cid = await company_id(client, hd)
    payer = await _payer(client, hd)
    eid = await agreement(client, hd, start=start, contact_id=payer)
    assert await _bind(cid, payer)
    r = await schedule(client, hd, eid, generation_mode="auto_invoice_collect")
    assert r.status_code == 200, r.text
    return cid, eid, payer


async def _collection(session, eid: str) -> list[dict]:
    return [d.get("collection") or {} for d in await billed_docs(session, eid)]


async def _outstanding_minor(session, doc: dict) -> int:
    from celerp.services.doc_balance import outstanding_balance
    from celerp.services.payments import to_stripe_amount
    return to_stripe_amount(outstanding_balance(doc), doc.get("currency") or "USD")


def _at(monkeypatch, instant: datetime) -> None:
    from celerp_subscriptions import billing
    monkeypatch.setattr(billing, "now", lambda: instant)


async def _pay(client, hd, doc_id: str, amount: float):
    r = await client.post(f"/docs/{doc_id}/payment", headers=hd, json={
        "amount": amount, "method": "bank_transfer", "payment_date": "2026-07-02"})
    assert r.status_code in (200, 201), r.text


# ── capability ───────────────────────────────────────────────────────────────

async def test_hello_declares_the_collection_delivery_types():
    """Cloud offers collection only to an installation that declared every type it delivers."""
    from celerp.gateway.client import GatewayClient
    hello = GatewayClient("tok", "inst", "wss://relay.test")._build_hello_payload("1", "1.0")
    assert {"collection.attempt", "collection.binding", "invoice.dispute",
            "invoice.refund", "invoice.payment_release"} <= set(hello["delivery_types"])


@pytest.mark.parametrize("flag", [None, 2, True, "1", 1.0])
async def test_collection_protocol_version_mismatch_fails_closed(client, session, monkeypatch, cloud, flag):
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    cid = await company_id(client, hd)
    payer = await _payer(client, hd)
    eid = await agreement(client, hd, start="2026-07-01", contact_id=payer)
    assert await _bind(cid, payer)
    _flag(flag)
    from celerp.services import collection
    assert collection.offered() is False
    r = await schedule(client, hd, eid, generation_mode="auto_invoice_collect")
    assert r.status_code == 409 and _says(r, "subscriptions.err_collection_off"), r.text
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=hd, json={"delivery": "copy"})
    assert r.status_code == 409 and _says(r, "subscriptions.err_collection_off"), r.text
    assert (await client.get(f"/subscriptions/{eid}/collection", headers=hd)).json()["offered"] is False
    assert cloud.calls == []
    _flag(1)
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice_collect")).status_code == 200


async def test_collect_mode_needs_its_permission_its_consent_and_a_card_on_file(client, session, monkeypatch, cloud):
    """Bill and collect automatically needs automate_collection (an admin's by default) and records
    its own consent beside billing's; a payer without an active card on file is refused naming it."""
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    cid = await company_id(client, hd)
    payer = await _payer(client, hd)
    eid = await agreement(client, hd, start="2026-07-01", contact_id=payer)
    r = await schedule(client, hd, eid, generation_mode="auto_invoice_collect")
    assert r.status_code == 409 and _says(r, "subscriptions.err_no_card_on_file"), r.text
    assert await _bind(cid, payer)
    manager, _ = await member(client, session, hd, "manager")
    assert (await schedule(client, manager, eid, generation_mode="auto_invoice_collect")).status_code == 403
    admin, admin_id = await member(client, session, hd, "admin")
    assert (await schedule(client, admin, eid, generation_mode="auto_invoice_collect")).status_code == 200
    consents = (await template(client, hd, eid))["consents"]
    assert consents["collection"]["by"] == admin_id and consents["generation"]["by"] == admin_id
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice")).status_code == 200
    assert "collection" not in (await template(client, hd, eid))["consents"]


# ── setup link (X1), card on file (X5), revoke (X9) ─────────────────────────

async def test_setup_link_validation_and_rate_limit(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    cid = await company_id(client, hd)
    no_email = await agreement(client, hd, start="2026-07-01", contact_id=await _payer(client, hd, None))
    r = await client.post(f"/subscriptions/{no_email}/collection/setup-link", headers=hd, json={"delivery": "email"})
    assert r.status_code == 409 and _says(r, "subscriptions.err_payer_no_email"), r.text
    stranger = await agreement(client, hd, start="2026-07-01")  # contact:test-001 is no contact of the company
    r = await client.post(f"/subscriptions/{stranger}/collection/setup-link", headers=hd, json={"delivery": "copy"})
    assert r.status_code == 409 and _says(r, "subscriptions.err_payer_not_contact"), r.text
    payer = await _payer(client, hd)
    eid = await agreement(client, hd, start="2026-07-01", contact_id=payer)
    manager, _ = await member(client, session, hd, "manager")
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=manager, json={"delivery": "copy"})
    assert r.status_code == 403, r.text
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=hd, json={"delivery": "fax"})
    assert r.status_code == 422, r.text
    assert cloud.calls == []
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=hd, json={"delivery": "copy"})
    assert r.status_code == 200 and r.json() == {"link": LINK, "emailed": True}, r.text
    [sent] = cloud.sent("setup-link")
    assert {k: sent[k] for k in ("protocol", "company_id", "payer_contact_id", "payer_email", "delivery")} == {
        "protocol": 1, "company_id": cid, "payer_contact_id": payer, "payer_email": "payer@example.test",
        "delivery": "copy"}
    assert sent["consent_text_version"] and sent["request_key"] and sent.get("resend_receipt") is False
    cloud.answers["setup-link"] = (429, {"detail": "setup_link_daily_limit"})
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=hd, json={"delivery": "copy"})
    assert r.status_code == 429 and _says(r, "subscriptions.err_setup_link_daily_limit"), r.text
    cloud.answers["setup-link"] = (429, {"detail": "setup_email_daily_limit"})
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=hd, json={"delivery": "email"})
    assert r.status_code == 429 and _says(r, "subscriptions.err_setup_email_daily_limit"), r.text


async def test_setup_link_cloud_unreachable_refuses_cleanly(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-07-01", contact_id=await _payer(client, hd))
    before = (await session.execute(select(LedgerEntry.id))).scalars().all()
    cloud.answers["setup-link"] = None
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=hd, json={"delivery": "email"})
    assert r.status_code == 502 and _says(r, "subscriptions.err_cloud_unreachable"), r.text
    assert (await session.execute(select(LedgerEntry.id))).scalars().all() == before


async def test_setup_link_email_failure_returns_copyable_link(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-07-01", contact_id=await _payer(client, hd))
    cloud.answers["setup-link"] = (200, {"link": LINK, "emailed": False})
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=hd, json={"delivery": "email"})
    assert r.status_code == 200 and r.json() == {"link": LINK, "emailed": False}, r.text


async def test_consent_receipt_email_failure_keeps_removal_link_reachable(client, session, monkeypatch, cloud):
    """A binding whose receipt was not delivered reads so on the agreement, and Resend receipt asks
    Cloud to send it again (X1 with resend_receipt)."""
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    cid = await company_id(client, hd)
    payer = await _payer(client, hd)
    eid = await agreement(client, hd, start="2026-07-01", contact_id=payer)
    assert await _bind(cid, payer, receipt_sent=False)
    view = (await client.get(f"/subscriptions/{eid}/collection", headers=hd)).json()
    assert view["binding"] == {"state": "active", "card_brand": "visa", "card_last4": "4242", "receipt_sent": False}
    cloud.answers["setup-link"] = (200, {"receipt_sent": True})
    r = await client.post(f"/subscriptions/{eid}/collection/setup-link", headers=hd,
                          json={"delivery": "email", "resend_receipt": True})
    assert r.status_code == 200 and r.json() == {"receipt_sent": True}, r.text
    assert cloud.sent("setup-link")[-1]["resend_receipt"] is True
    # The later delivery that says the receipt went out is the one kept.
    assert await _bind(cid, payer, receipt_sent=True)
    assert (await client.get(f"/subscriptions/{eid}/collection", headers=hd)).json()["binding"]["receipt_sent"] is True


async def test_payer_removes_card_revokes_binding(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, payer = await _collecting(client, hd)
    assert await _bind(cid, payer, "revoked", card_brand=None, card_last4=None)
    assert (await client.get(f"/subscriptions/{eid}/collection", headers=hd)).json()["binding"]["state"] == "revoked"
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    assert doc["status"] == "final" and cloud.sent("attempt") == []
    assert doc["collection"]["state"] == "stopped" and doc["collection"]["reason"] == "no_card_on_file"


async def test_operator_revokes_consent_binding_revoked(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    cid, eid, payer = await _collecting(client, hd)
    manager, _ = await member(client, session, hd, "manager")
    assert (await client.post(f"/subscriptions/{eid}/collection/revoke", headers=manager)).status_code == 403
    cloud.answers["revoke"] = None
    r = await client.post(f"/subscriptions/{eid}/collection/revoke", headers=hd)
    assert r.status_code == 502 and _says(r, "subscriptions.err_cloud_unreachable"), r.text
    assert (await client.get(f"/subscriptions/{eid}/collection", headers=hd)).json()["binding"]["state"] == "active"
    cloud.answers["revoke"] = (200, {"state": "revoked"})
    r = await client.post(f"/subscriptions/{eid}/collection/revoke", headers=hd)
    assert r.status_code == 200, r.text
    assert cloud.sent("revoke")[-1]["payer_contact_id"] == payer
    assert (await client.get(f"/subscriptions/{eid}/collection", headers=hd)).json()["binding"]["state"] == "revoked"
    r = await client.post(f"/subscriptions/{eid}/collection/revoke", headers=hd)
    assert r.status_code == 409 and _says(r, "subscriptions.err_no_active_binding"), r.text


async def test_account_deauthorized_stops_attempts_and_fails_closed(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, payer = await _collecting(client, hd)
    assert await _bind(cid, payer, "access_removed")
    _flag(None)  # Cloud withdraws the flag with the access
    notes = (await client.get("/notifications", headers=hd)).json()
    assert "Automatic collection stopped: Stripe access was removed" in str(notes)
    await tick(session, cid, "2026-07-01")
    assert cloud.sent("attempt") == []
    other = await agreement(client, hd, start="2026-07-01", contact_id=payer)
    r = await schedule(client, hd, other, generation_mode="auto_invoice_collect")
    assert r.status_code == 409, r.text


# ── attempts (X2), outcomes (X4), retries and cancels (X3) ───────────────────

async def test_collection_request_after_commit_only(client, session, monkeypatch, cloud):
    """The attempt is asked for after the bill commits, with the live outstanding as the amount;
    a bill that fails asks for nothing."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, payer = await _collecting(client, hd)
    from datetime import date
    from celerp_subscriptions import billing
    report = await billing.run_due(session=session, company_id=uuid.UUID(cid), today=date(2026, 7, 1), budget=10)
    assert cloud.sent("attempt") == []  # nothing leaves inside the billing transaction
    [doc] = await billed_docs(session, eid)
    assert doc["collection"]["state"] == "pending"
    for step in report.after_commit:
        await step(lambda: _same(session))
    [sent] = cloud.sent("attempt")
    assert sent["protocol"] == 1 and sent["company_id"] == cid and sent["entity_id"] == doc["entity_id"]
    assert sent["attempt_number"] == 1 and sent["request_key"] == f"collect:{doc['entity_id']}:1"
    assert sent["amount_minor"] == await _outstanding_minor(session, doc) and sent["currency"] == doc["currency"]
    assert sent["payer_contact_id"] == payer and isinstance(sent["generation"], int)
    assert sent["context"]["deposit_account"]
    [doc] = await billed_docs(session, eid)
    assert doc["collection"]["state"] == "submitted"


def _same(session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield session
    return _cm()


async def test_protocol_attempt_request_idempotent_by_attempt_number(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    cloud.answers["attempt"] = None
    await tick(session, cid, "2026-07-01")
    [c] = await _collection(session, eid)
    assert c["state"] == "pending"
    cloud.answers["attempt"] = lambda p: (200, {"state": "submitted", "attempt_number": p["attempt_number"]})
    await tick(session, cid, "2026-07-02")
    first, again = cloud.sent("attempt")
    assert {k: again[k] for k in ("attempt_number", "request_key", "amount_minor")} == {
        k: first[k] for k in ("attempt_number", "request_key", "amount_minor")}
    await tick(session, cid, "2026-07-03")
    assert len(cloud.sent("attempt")) == 2
    [c] = await _collection(session, eid)
    assert c["state"] == "submitted"


async def test_requires_action_not_failure(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    assert await _attempt_outcome(cid, doc["entity_id"], 1, "requires_action", action_url="https://bank.test/3ds")
    [c] = await _collection(session, eid)
    assert c["state"] == "requires_action" and c["action_url"] == "https://bank.test/3ds"
    _at(monkeypatch, datetime(2026, 7, 20, tzinfo=timezone.utc))
    await tick(session, cid, "2026-07-20")
    assert len(cloud.sent("attempt")) == 1


async def test_retry_requested_by_app_with_live_outstanding_only_after_eligible(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    eligible = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)
    assert await _attempt_outcome(cid, doc["entity_id"], 1, "failed", failure_code="card_declined",
                                  retry_eligible_at=eligible.isoformat())
    [c] = await _collection(session, eid)
    assert c["state"] == "failed" and c["retry_eligible_at"] == eligible.isoformat()
    await _pay(client, hd, doc["entity_id"], 10.0)
    _at(monkeypatch, eligible - timedelta(hours=1))
    await tick(session, cid, "2026-07-04")
    assert len(cloud.sent("attempt")) == 1
    _at(monkeypatch, eligible)
    await tick(session, cid, "2026-07-04")
    first, retry = cloud.sent("attempt")
    [doc] = await billed_docs(session, eid)
    assert retry["attempt_number"] == 2 and retry["request_key"] == f"collect:{doc['entity_id']}:2"
    assert retry["amount_minor"] == await _outstanding_minor(session, doc) == first["amount_minor"] - 1000


async def test_no_retry_once_settled_or_back_to_manual(client, session, monkeypatch, cloud):
    """A retry is asked for only while the invoice is open and the mode still collects; otherwise
    the document records why the retry stopped."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    eligible = datetime(2026, 7, 4, tzinfo=timezone.utc)
    assert await _attempt_outcome(cid, doc["entity_id"], 1, "failed", retry_eligible_at=eligible.isoformat())
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice")).status_code == 200
    _at(monkeypatch, eligible)
    await tick(session, cid, "2026-07-04")
    assert len(cloud.sent("attempt")) == 1
    [c] = await _collection(session, eid)
    assert c["state"] == "stopped" and c["reason"] == "mode_manual"


async def test_cancel_request_retried_until_acknowledged(client, session, monkeypatch, cloud):
    """A payment by hand while an attempt is open asks Cloud to cancel it, every pass until Cloud
    answers."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    cloud.answers["attempt"] = lambda p: (200, {"state": "requires_action", "attempt_number": p["attempt_number"]})
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    await _pay(client, hd, doc["entity_id"], float(doc["total"]))
    cloud.answers["cancel"] = None
    await tick(session, cid, "2026-07-02")
    await tick(session, cid, "2026-07-03")
    cloud.answers["cancel"] = (200, {"state": "canceled"})
    await tick(session, cid, "2026-07-04")
    await tick(session, cid, "2026-07-05")
    assert [p["entity_id"] for p in cloud.sent("cancel")] == [doc["entity_id"]] * 3
    assert len(cloud.sent("attempt")) == 1
    [c] = await _collection(session, eid)
    assert c["state"] == "canceled"


async def test_manual_payment_during_pending_attempt_cancels_or_unmatched(client, session, monkeypatch, cloud):
    """A charge Cloud could no longer cancel that succeeds after a payment by hand settled the
    invoice parks among the unmatched payments, whole."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    [sent] = cloud.sent("attempt")
    await _pay(client, hd, doc["entity_id"], float(doc["total"]))
    cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})
    await tick(session, cid, "2026-07-02")
    await tick(session, cid, "2026-07-03")
    assert len(cloud.sent("cancel")) == 1  # Cloud said it runs to its outcome: not asked again
    from celerp.services import payments
    assert await payments.receive_payment({
        "company_id": cid, "entity_id": doc["entity_id"], "reference": "pi_late", "amount_minor": sent["amount_minor"],
        "currency": sent["currency"], "paid_at": "2026-07-03T09:00:00+00:00", "managed": True,
        "context": sent["context"]})
    from celerp.models.payment_closure import UnmatchedPayment
    assert (await session.get(UnmatchedPayment, "pi_late")) is not None


async def test_mode_manual_cancels_unsubmitted_attempts(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    cloud.answers["attempt"] = None
    await tick(session, cid, "2026-07-01")
    assert (await schedule(client, hd, eid, generation_mode="manual_due")).status_code == 200
    await tick(session, cid, "2026-07-02")
    assert len(cloud.sent("attempt")) == 1  # not asked again once the mode is manual
    [doc] = await billed_docs(session, eid)
    assert [p["entity_id"] for p in cloud.sent("cancel")] == [doc["entity_id"]]
    [c] = await _collection(session, eid)
    assert c["state"] == "canceled"


async def test_offsession_success_through_apply_doc_payment(client, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    await tick(session, cid, "2026-07-01")
    [sent] = cloud.sent("attempt")
    from celerp.services import payments
    assert await payments.receive_payment({
        "company_id": cid, "entity_id": sent["entity_id"], "reference": "pi_1", "amount_minor": sent["amount_minor"],
        "currency": sent["currency"], "paid_at": "2026-07-01T13:00:00+00:00", "managed": True,
        "context": sent["context"]})
    [doc] = await billed_docs(session, eid)
    assert doc["status"] == "paid"
    assert [(p["method"], p["reference"]) for p in doc["payments"]] == [("stripe", "pi_1")]
    await tick(session, cid, "2026-07-02")
    assert len(cloud.sent("attempt")) == 1 and cloud.sent("cancel") == []


async def test_collection_deliveries_validated_at_app(client, session, monkeypatch, cloud):
    """Deliveries naming another company, a document that is not there, an attempt the document never
    asked for or a payer who is not a contact write nothing; one without its fields is not taken."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, payer = await _collecting(client, hd)
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    before = (await session.execute(select(LedgerEntry.id))).scalars().all()
    other = str(uuid.uuid4())
    assert await _attempt_outcome(other, doc["entity_id"], 1, "failed") is True
    assert await _attempt_outcome(cid, "doc:not-there", 1, "failed") is True
    assert await _attempt_outcome(cid, doc["entity_id"], 7, "failed") is True
    assert await _bind(cid, "contact:not-there") is True
    assert await _bind(other, payer) is True
    from celerp.services import collection
    assert await collection.receive_attempt({"company_id": cid, "entity_id": doc["entity_id"]}) is False
    assert await _bind(cid, payer, state="bogus") is False
    assert (await session.execute(select(LedgerEntry.id))).scalars().all() == before
    [c] = await _collection(session, eid)
    assert c["state"] == "submitted"


# ── page ─────────────────────────────────────────────────────────────────────

async def test_collection_ui_hidden_without_capability(owner_ui, session, monkeypatch, cloud):
    freeze_day(monkeypatch, "2026-06-15")
    api = owner_ui.api
    payer = await _payer(api, {})
    eid = await agreement(api, {}, start="2026-07-01", contact_id=payer)
    _flag(None)
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert "auto_invoice_collect" not in page and "/collection/setup-link" not in page
    _flag(1)
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert 'value="auto_invoice_collect"' in page and "/collection/setup-link" in page

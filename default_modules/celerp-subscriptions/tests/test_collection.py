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


def stand_in(monkeypatch, session) -> _Cloud:
    """Celerp Cloud replaced for one test: payments on, collection protocol 1, the app's own
    delivery sessions joining the test's transaction, each request answered by a _Cloud."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from celerp.gateway import state
    from celerp.services import collection

    monkeypatch.setattr(state, "_feature_flags",
                        {**state.get_feature_flags(), "payments_enabled": True, "collection_protocol": 1})
    monkeypatch.setattr("celerp.services.payments._own_session", lambda: AsyncSession(
        bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"))
    fake = _Cloud()
    monkeypatch.setattr(collection, "_ask", fake)
    return fake


@pytest.fixture
def cloud(monkeypatch, session):
    return stand_in(monkeypatch, session)


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
        "amount": amount, "method": "bank_transfer", "payment_date": "2026-07-02", "bank_account": "1111"})
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



async def test_collection_is_for_sales_agreements_only(client, session, monkeypatch, cloud):
    """A purchasing agreement bills what the company owes its supplier, so it cannot collect from a
    card: Bill and collect automatically and every card on file action are refused naming it, with
    nothing asked of Cloud, even when the supplier has a card on file as a customer."""
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    cid = await company_id(client, hd)
    r = await client.post("/crm/contacts", headers=hd, json={"name": "Supplier", "email": "supplier@example.test",
                                                             "contact_type": "both"})
    assert r.status_code in (200, 201), r.text
    supplier = r.json()["id"]
    assert await _bind(cid, supplier)
    eid = await agreement(client, hd, start="2026-07-01", doc_type="subscription_po", contact_id=supplier)
    r = await schedule(client, hd, eid, generation_mode="auto_invoice_collect")
    assert r.status_code == 409 and _says(r, "subscriptions.err_collection_sales_only"), r.text
    for path, body in (("setup-link", {"delivery": "copy"}), ("revoke", None)):
        r = await client.post(f"/subscriptions/{eid}/collection/{path}", headers=hd, json=body)
        assert r.status_code == 409 and _says(r, "subscriptions.err_collection_sales_only"), (path, r.text)
    r = await client.get(f"/subscriptions/{eid}/collection", headers=hd)
    assert r.status_code == 409 and _says(r, "subscriptions.err_collection_sales_only"), r.text
    assert cloud.calls == []


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


@pytest.mark.parametrize("route", ["generate-due", "bulk", "bill-early"])
async def test_collection_requested_when_a_period_is_billed_by_hand(client, session, monkeypatch, cloud, route):
    """A period billed from the page or the API is charged as soon as the bill commits, as an
    unattended bill is, not at the dispatcher's next pass."""
    freeze_day(monkeypatch, "2026-07-01" if route != "bill-early" else "2026-06-20")
    hd = await register(client)
    cid, eid, payer = await _collecting(client, hd)
    key = uuid.uuid4().hex
    if route == "bulk":
        r = await client.post("/subscriptions/generate-due", headers=hd,
                              json={"items": [{"entity_id": eid, "period_index": 0, "request_key": key}]})
    else:
        r = await client.post(f"/subscriptions/{eid}/{route}", headers=hd, json={"request_key": key})
    assert r.status_code == 200, r.text
    [doc] = await billed_docs(session, eid)
    [sent] = cloud.sent("attempt")
    assert (sent["entity_id"], sent["attempt_number"], sent["payer_contact_id"]) == (doc["entity_id"], 1, payer)
    assert sent["amount_minor"] == await _outstanding_minor(session, doc)
    assert doc["collection"]["state"] == "submitted"


async def test_every_route_that_bills_a_period_asks_for_its_collection():
    """Each route that claims a period by hand asks for the charge after it commits."""
    import ast
    import inspect
    from celerp_subscriptions import routes
    tree = ast.parse(inspect.getsource(routes))
    claimers = {fn.name: {f"{c.func.value.id}.{c.func.attr}" for c in ast.walk(fn) if isinstance(c, ast.Call)
                          and isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name)}
                for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef)}
    billing = sorted(name for name, called in claimers.items() if "billing.claim_period" in called)
    assert billing == ["bill_early", "catch_up_run", "generate_all_due", "generate_due"]
    assert [name for name in billing if "collect.ask_after_bill" not in claimers[name]] == []


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


async def test_succeeded_attempt_is_taken_and_settles_with_its_payment(client, session, monkeypatch, cloud):
    """Cloud reports a charge that went through before (or without) its payment reaching the
    invoice: the report is taken, nothing is charged or canceled again, the invoice says where
    the payment is when it could not take it, and the payment once recorded marks it paid."""
    from celerp.services import payments
    from ui.components.encode import to_markup
    from ui.routes.documents import _collection_status
    from ui.i18n import t
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    [sent] = cloud.sent("attempt")
    assert await _attempt_outcome(cid, doc["entity_id"], 1, "succeeded") is True
    [c] = await _collection(session, eid)
    assert (c["state"], c["definitive"]) == ("succeeded", True)
    assert t("collection.status_succeeded") in to_markup(_collection_status(c, "UTC"))
    await tick(session, cid, "2026-07-02")
    assert len(cloud.sent("attempt")) == 1 and cloud.sent("cancel") == []
    assert await _attempt_outcome(cid, doc["entity_id"], 1, "processing") is True  # a late, lesser report
    assert (await _collection(session, eid))[0]["state"] == "succeeded"
    assert await payments.receive_payment({
        "company_id": cid, "entity_id": doc["entity_id"], "reference": "pi_1", "amount_minor": sent["amount_minor"],
        "currency": sent["currency"], "paid_at": "2026-07-01T13:00:00+00:00", "managed": True,
        "context": sent["context"]})
    [doc] = await billed_docs(session, eid)
    assert doc["status"] == "paid"
    assert (await _collection(session, eid))[0]["state"] == "paid"


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


def _toast(r) -> dict:
    import json
    return json.loads(r.headers.get("HX-Trigger") or "{}").get("celerpToast") or {}


async def test_agreement_page_shows_the_card_on_file_and_its_actions(owner_ui, session, monkeypatch, cloud):
    """The agreement page names the payer's card on file. Copy setup link shows the link to copy;
    Email setup link says it went, or shows the link to copy when the email did not; Cloud out of
    reach is refused saying so; Remove card revokes the card at Cloud and the page reads No card on file."""
    from ui.i18n import t
    freeze_day(monkeypatch, "2026-06-15")
    api = owner_ui.api
    cid = await company_id(api, {})
    payer = await _payer(api, {})
    eid = await agreement(api, {}, start="2026-07-01", contact_id=payer)
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert t("subscriptions.no_card_on_file") in page and "/collection/revoke" not in page

    r = await owner_ui.post(f"/subscriptions/{eid}/collection/setup-link", data={"delivery": "copy"})
    assert r.status_code == 200 and LINK in r.text, r.text
    assert cloud.sent("setup-link")[-1]["delivery"] == "copy"
    r = await owner_ui.post(f"/subscriptions/{eid}/collection/setup-link", data={"delivery": "email"})
    assert _toast(r) == {"message": t("subscriptions.done_setup_link_emailed"), "type": "success"}, r.headers
    cloud.answers["setup-link"] = (200, {"link": LINK, "emailed": False})
    r = await owner_ui.post(f"/subscriptions/{eid}/collection/setup-link", data={"delivery": "email"})
    assert LINK in r.text and _toast(r) == {"message": t("subscriptions.setup_link_not_emailed"), "type": "warning"}
    cloud.answers["setup-link"] = None
    r = await owner_ui.post(f"/subscriptions/{eid}/collection/setup-link", data={"delivery": "email"})
    assert t("subscriptions.err_cloud_unreachable") in _toast(r).get("message", ""), r.headers

    assert await _bind(cid, payer, receipt_sent=False)
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert t("subscriptions.card_on_file", brand="Visa", last4="4242") in page
    assert t("subscriptions.receipt_not_sent") in page and "/collection/revoke" in page
    cloud.answers["setup-link"] = (200, {"receipt_sent": True})
    r = await owner_ui.post(f"/subscriptions/{eid}/collection/setup-link", data={"delivery": "email", "resend_receipt": "true"})
    assert _toast(r) == {"message": t("subscriptions.done_receipt_sent"), "type": "success"}, r.headers
    assert cloud.sent("setup-link")[-1]["resend_receipt"] is True

    r = await owner_ui.post(f"/subscriptions/{eid}/collection/revoke")
    assert _toast(r) == {"message": t("subscriptions.done_card_removed"), "type": "success"}, r.headers
    assert cloud.sent("revoke")[-1]["payer_contact_id"] == payer
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert t("subscriptions.no_card_on_file") in page and "/collection/revoke" not in page


async def test_agreement_on_collect_keeps_its_mode_shown_when_cloud_withdraws_collection(owner_ui, session, monkeypatch, cloud):
    """An agreement already on Bill and collect automatically still shows that mode (and its badge)
    after Cloud stops offering collection, so it can be set back to manual; the card section is gone."""
    from ui.i18n import t
    freeze_day(monkeypatch, "2026-06-15")
    cid, eid, _ = await _collecting(owner_ui.api, {})
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert t("subscriptions.bills_and_collects") in page and t("subscriptions.consent_collection", date="").strip() in page
    _flag(None)
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    assert 'value="auto_invoice_collect" selected' in page and t("subscriptions.bills_and_collects") in page
    assert "/collection/setup-link" not in page


async def test_invoice_page_says_how_its_collection_stands(owner_ui, session, monkeypatch, cloud):
    """The billed invoice's page reads its collection: pending while with Cloud, the payer's action
    link when Cloud needs it, Charge failed with the retry date, and why collection stopped."""
    from ui.i18n import sentence, t
    freeze_day(monkeypatch, "2026-07-01")
    cid, eid, payer = await _collecting(owner_ui.api, {})
    await tick(session, cid, "2026-07-01")
    [doc] = await billed_docs(session, eid)
    doc_id = doc["entity_id"]
    page = (await owner_ui.get(f"/docs/{doc_id}")).text
    assert t("collection.status_pending") in page
    assert await _attempt_outcome(cid, doc_id, 1, "requires_action", definitive=False,
                                  action_url="https://pay.test/act/1")
    page = (await owner_ui.get(f"/docs/{doc_id}")).text
    assert t("collection.status_requires_action") in page and 'href="https://pay.test/act/1"' in page
    eligible = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)
    assert await _attempt_outcome(cid, doc_id, 1, "failed", failure_code="card_declined",
                                  retry_eligible_at=eligible.isoformat())
    page = (await owner_ui.get(f"/docs/{doc_id}")).text
    assert sentence("collection.status_failed_retry", date="2026-07-04") in page
    assert await _bind(cid, payer, "revoked", card_brand=None, card_last4=None)
    _at(monkeypatch, eligible)
    await tick(session, cid, "2026-07-04")
    page = (await owner_ui.get(f"/docs/{doc_id}")).text
    assert t("collection.status_stopped", reason=t("collection.stopped_no_card_on_file")) in page


async def test_every_reason_collection_stops_for_is_one_the_invoice_page_names():
    """Each reason the app writes on a stopped collection is in STOP_REASONS, which the invoice
    page reads by name (anything else reads as Cloud's refusal), and each has its words."""
    import json
    import re
    from pathlib import Path
    from celerp.services import collection
    from celerp_subscriptions import collect
    root = Path(collection.__file__).resolve().parents[2]
    written = set()
    for module in (collection, collect):
        source = Path(module.__file__).read_text(encoding="utf-8")
        written |= set(re.findall(r'"state": "stopped", "reason": "(\w+)"', source))
        written |= set(re.findall(r'(?:return|reason =) "(\w+)"$', source, re.M))
    assert written == collection.STOP_REASONS
    words = json.loads((root / "ui" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert [r for r in collection.STOP_REASONS if f"collection.stopped_{r}" not in words] == []


# ── a request key names one request ──────────────────────────────────────────

async def _unanswered(client, session, monkeypatch, cloud) -> tuple[str, str]:
    """An agreement whose first attempt was asked for and never answered: (company id, agreement id)."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, _ = await _collecting(client, hd)
    cloud.answers["attempt"] = None
    await tick(session, cid, "2026-07-01")
    [c] = await _collection(session, eid)
    assert c["state"] == "pending" and c["asked"]
    return cid, eid


async def test_retry_after_a_lost_answer_sends_the_request_first_sent(client, session, monkeypatch, cloud):
    """An attempt whose answer was lost is asked for again with exactly the request first sent,
    whatever changed in the company's settings or payment generation since."""
    from celerp.services import payments
    from celerp.services.company_lock import locked_company
    cid, eid = await _unanswered(client, session, monkeypatch, cloud)
    company = await locked_company(session, uuid.UUID(cid))
    company.settings = {**(company.settings or {}), "timezone": "Asia/Bangkok"}
    await session.flush()
    generation = await payments.checkout_generation()

    async def _next_generation():
        return generation + 1
    monkeypatch.setattr(payments, "checkout_generation", _next_generation)
    cloud.answers["attempt"] = lambda p: (200, {"state": "submitted", "attempt_number": p["attempt_number"]})
    await tick(session, cid, "2026-07-02")
    first, again = cloud.sent("attempt")
    assert again == first
    [c] = await _collection(session, eid)
    assert c["state"] == "submitted"


async def test_a_refused_retry_of_an_unanswered_attempt_is_reconciled_before_it_ends(client, session, monkeypatch,
                                                                                   cloud):
    """Cloud refusing the retry of an attempt whose first answer was lost does not stop collection
    while that first outcome is unknown: the attempt is cancelled at Cloud, and ends only on its answer."""
    cid, eid = await _unanswered(client, session, monkeypatch, cloud)
    cloud.answers["attempt"] = (409, {"detail": "attempt_number_used"})
    cloud.answers["cancel"] = None
    await tick(session, cid, "2026-07-02")
    [c] = await _collection(session, eid)
    assert c["state"] == "pending" and c["cancel"] == "queued"
    assert len(cloud.sent("cancel")) == 1
    cloud.answers["cancel"] = (409, {"detail": "attempt_settled"})
    await tick(session, cid, "2026-07-03")
    [c] = await _collection(session, eid)
    assert c["state"] == "pending" and c["cancel"] == "refused"
    [doc] = await billed_docs(session, eid)
    assert await _attempt_outcome(cid, doc["entity_id"], 1, "succeeded")
    [c] = await _collection(session, eid)
    assert c["state"] == "succeeded"
    assert len(cloud.sent("attempt")) == 2


async def test_an_amendment_invoice_is_collected_as_a_period_invoice_is(client, session, monkeypatch, cloud):
    """An invoice an amendment issues on an agreement that collects automatically opens its own
    attempt and is charged after the amendment commits, as each period's invoice is."""
    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid, eid, payer = await _collecting(client, hd)
    await tick(session, cid, "2026-07-01")
    line = (await template(client, hd, eid))["line_items"][0]["line_id"]
    freeze_day(monkeypatch, "2026-07-15")
    r = await client.post(f"/subscriptions/{eid}/amend", headers=hd, json={
        "effective": "2026-07-15", "quantities": {line: 3}, "request_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    invoice = r.json()["invoice_id"]
    [doc] = [d for d in await billed_docs(session, eid) if d["entity_id"] == invoice]
    assert doc["collection"]["attempt_number"] == 1 and doc["collection"]["state"] == "submitted"
    assert [p["entity_id"] for p in cloud.sent("attempt")][-1] == invoice


async def test_payer_change_needs_the_collection_permission(client, session, monkeypatch, cloud):
    """Changing who pays an agreement that collects automatically, through the document's own
    edit, needs automate_collection: a member who may only edit documents is refused and the
    payer stays."""
    freeze_day(monkeypatch, "2026-06-15")
    hd = await register(client)
    cid, eid, payer = await _collecting(client, hd)
    other = await _payer(client, hd, "other@example.test")
    assert await _bind(cid, other)
    manager, _ = await member(client, session, hd, "manager")
    r = await client.patch(f"/docs/{eid}", headers=manager, json={"contact_id": other})
    assert r.status_code == 403, r.text
    assert (await template(client, hd, eid))["contact_id"] == payer

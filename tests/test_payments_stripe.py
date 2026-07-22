# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Online invoice payment via Stripe Connect, brokered by Celerp Connect (mocked).

The instance holds no Stripe credentials - checkout creation, session status and
Connect onboarding are all cloud calls, mocked here at the payments-service boundary.
The "payments enabled" gate is the gateway feature flag delivered on the handshake.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from celerp.services.money import to_minor_units


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "PayCo", "email": "pay@test.com", "name": "Admin", "password": "password123"})
    return r.json()["access_token"]


async def _payable_invoice(client: AsyncClient, tok: str) -> tuple[str, str]:
    """Create + finalize an invoice, return (entity_id, share_token)."""
    r = await client.post("/docs", json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 500.0}],
        "subtotal": 1000.0, "tax": 70.0, "total": 1070.0, "currency": "USD",
    }, headers=_h(tok))
    eid = r.json()["id"]
    assert (await client.post(f"/docs/{eid}/finalize", headers=_h(tok))).status_code == 200
    token = (await client.post(f"/docs/{eid}/share", headers=_h(tok))).json()["token"]
    return eid, token


def _company_id(token: str) -> str:
    from jose import jwt
    from celerp.config import settings
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["company_id"]


async def _doc_state(client, tok, eid):
    return (await client.get(f"/docs/{eid}", headers=_h(tok))).json()


@pytest.fixture
def payments_on(monkeypatch):
    """Merchant has a connected account: the cloud feature flag is on and the
    instance is cloud-connected (public URL set)."""
    from celerp.config import settings as cfg
    monkeypatch.setattr(cfg, "celerp_public_url", "https://acme.celerp.com")
    monkeypatch.setattr("celerp.services.payments.payments_enabled", lambda: True)


# ── minor units ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount,currency,expected", [
    (12.34, "USD", 1234), (1070.0, "THB", 107000), (1234, "JPY", 1234), (3.0, "KWD", 3000),
])
def test_to_minor_units(amount, currency, expected):
    assert to_minor_units(amount, currency) == expected


# ── checkout (the contract Cloud fulfils) ────────────────────────────────────

@pytest.mark.asyncio
async def test_checkout_sends_balance_due_and_reconcile_metadata(client, payments_on, monkeypatch):
    captured = {}
    async def _mk(**kw):
        captured.update(kw)
        return {"url": "https://stripe.test/cs_1"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", _mk)

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    r = await client.get(f"/pay/{token}", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "https://stripe.test/cs_1"
    assert captured["amount_minor"] == 107000  # 1070.00 USD in minor units
    assert captured["currency"] == "USD"
    # success_url must carry the placeholder Cloud/Stripe substitutes so /return can reconcile
    assert "{CHECKOUT_SESSION_ID}" in captured["success_url"]
    # metadata lets the backup gateway push route the confirmation back to this doc
    assert captured["metadata"]["entity_id"] == eid
    assert captured["metadata"]["company_id"] == _company_id(tok)
    assert captured["metadata"]["token"] == token


@pytest.mark.asyncio
async def test_checkout_unavailable_when_disabled(client):
    # No connected account → the flag is off → the pay route is closed.
    tok = await _register(client)
    _, token = await _payable_invoice(client, tok)
    r = await client.get(f"/pay/{token}", follow_redirects=False)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_checkout_502_when_cloud_unavailable(client, payments_on, monkeypatch):
    async def _mk(**kw):
        return None
    monkeypatch.setattr("celerp.services.payments.create_checkout", _mk)
    tok = await _register(client)
    _, token = await _payable_invoice(client, tok)
    r = await client.get(f"/pay/{token}", follow_redirects=False)
    assert r.status_code == 502


# ── reconcile on the customer's return (idempotent) ──────────────────────────

def _paid_status():
    return {"paid": True, "reference": "pi_123", "amount_minor": 107000, "currency": "usd"}


@pytest.mark.asyncio
async def test_return_reconciles_payment(client, payments_on, monkeypatch):
    monkeypatch.setattr("celerp.services.payments.checkout_status",
                        lambda _sid: _async(_paid_status()))
    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    r = await client.get(f"/pay/{token}/return?session_id=cs_1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/share/{token}"

    doc = await _doc_state(client, tok, eid)
    assert doc["status"] == "paid"
    assert doc["amount_paid"] == 1070.0
    assert any(p.get("reference") == "pi_123" and p.get("method") == "stripe"
               for p in doc.get("payments", []))


@pytest.mark.asyncio
async def test_return_is_idempotent(client, payments_on, monkeypatch):
    monkeypatch.setattr("celerp.services.payments.checkout_status",
                        lambda _sid: _async(_paid_status()))
    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    await client.get(f"/pay/{token}/return?session_id=cs_1", follow_redirects=False)
    await client.get(f"/pay/{token}/return?session_id=cs_1", follow_redirects=False)
    doc = await _doc_state(client, tok, eid)
    assert len([p for p in doc["payments"] if p.get("reference") == "pi_123"]) == 1


@pytest.mark.asyncio
async def test_return_ignores_unpaid_session(client, payments_on, monkeypatch):
    monkeypatch.setattr("celerp.services.payments.checkout_status",
                        lambda _sid: _async({"paid": False, "reference": None,
                                             "amount_minor": 0, "currency": "usd"}))
    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    await client.get(f"/pay/{token}/return?session_id=cs_1", follow_redirects=False)
    doc = await _doc_state(client, tok, eid)
    assert doc["status"] != "paid"
    assert not doc.get("payments")


def _async(value):
    async def _c(*a, **k):
        return value
    return _c()


# ── backup confirmation (the Cloud gateway push path) ────────────────────────

@pytest.mark.asyncio
async def test_backup_push_records_and_is_idempotent(client, session, payments_on):
    """record_stripe_payment is the shared recorder the invoice.payment gateway
    handler calls. A push that lands before any return records the payment; a
    replayed push (or a push after the return already recorded it) is a no-op."""
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    cid = _company_id(tok)

    row = await session.get(Projection, (cid, eid))
    await record_stripe_payment(session, cid, eid, dict(row.state),
                                reference="pi_push", amount_minor=107000, currency="usd")
    doc = await _doc_state(client, tok, eid)
    assert doc["status"] == "paid"
    assert len([p for p in doc["payments"] if p.get("reference") == "pi_push"]) == 1

    row2 = await session.get(Projection, (cid, eid))
    await record_stripe_payment(session, cid, eid, dict(row2.state),
                                reference="pi_push", amount_minor=107000, currency="usd")
    doc2 = await _doc_state(client, tok, eid)
    assert len([p for p in doc2["payments"] if p.get("reference") == "pi_push"]) == 1


@pytest.mark.asyncio
async def test_backup_push_uses_company_deposit_account(client, session, payments_on):
    """The deposit GL account is the company setting, defaulting to Cash."""
    from celerp.models.company import Company
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    company = await session.get(Company, cid)
    company.settings = {**(company.settings or {}), "stripe_deposit_account": "1055"}
    await session.commit()

    row = await session.get(Projection, (cid, eid))
    await record_stripe_payment(session, cid, eid, dict(row.state),
                                reference="pi_acct", amount_minor=107000, currency="usd")
    doc = await _doc_state(client, tok, eid)
    pay_entry = next(p for p in doc["payments"] if p.get("reference") == "pi_acct")
    assert pay_entry["bank_account"] == "1055"


# ── the whole journey, end to end ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_online_payment_journey(client, session, payments_on, monkeypatch):
    """Merchant sends an invoice; customer pays it from the email; money lands
    in the books; the link reverts to view-only; replays change nothing.

    Chains every hop of the real flow: send email (Pay leads with the amount
    due) -> share page carries the pay bar -> /pay creates the checkout with
    the exact balance in minor units and reconcile metadata -> the return leg
    records the payment -> status paid, journal entry posted, pay bar gone,
    /pay refuses further charges -> a replayed return records nothing."""
    import asyncio as _asyncio

    sent = {}
    email_done = _asyncio.Event()
    async def _fake_send(to, subject, body_html, body_text="", **kw):
        sent.update(html=body_html, text=body_text, subject=subject)
        email_done.set()
        return True
    monkeypatch.setattr("celerp.services.email.send_email", _fake_send)

    checkout = {}
    async def _fake_checkout(**kw):
        checkout.update(kw)
        return {"url": "https://stripe.test/cs_journey"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", _fake_checkout)

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)

    # 1. Merchant clicks Send: the email leads with a Pay button for the balance due.
    r = await client.post(f"/docs/{eid}/send", json={"sent_to": "cust@x.com"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    await _asyncio.wait_for(email_done.wait(), timeout=2)
    assert f"/pay/{token}" in sent["html"] and ">Pay USD 1,070.00<" in sent["html"]
    assert f"/pay/{token}" in sent["text"]

    # 2. Customer opens the invoice: print layout with the amount-due pay bar
    # (the on-page bar formats with the currency symbol; the email uses the code).
    html = (await client.get(f"/share/{token}")).text
    assert f"/pay/{token}" in html and "Pay $1,070.00 now" in html

    # 3. Customer clicks Pay: checkout for the exact balance, with reconcile metadata.
    r = await client.get(f"/pay/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://stripe.test/cs_journey"
    assert checkout["amount_minor"] == 107000 and checkout["currency"] == "USD"
    assert checkout["metadata"]["entity_id"] == eid and checkout["metadata"]["token"] == token
    assert "{CHECKOUT_SESSION_ID}" in checkout["success_url"]

    # 4. Stripe confirms; the customer returns; the payment reconciles.
    monkeypatch.setattr("celerp.services.payments.checkout_status",
                        lambda sid: _async({"paid": True, "reference": "pi_journey",
                                            "amount_minor": 107000, "currency": "usd"}))
    r = await client.get(f"/pay/{token}/return?session_id=cs_j", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/share/{token}"

    # 5. Money truth: invoice paid, exactly one payment, exactly one posted JE.
    doc = await _doc_state(client, tok, eid)
    assert doc["status"] == "paid" and doc["amount_outstanding"] == 0
    assert [p["reference"] for p in doc["payments"]] == ["pi_journey"]
    cid = _company_id(tok)
    assert await _pay_je_entities(session, cid, eid) == [f"je:auto:{eid}:pay:0"]

    # 6. The link reverts to view-only and cannot take more money.
    html = (await client.get(f"/share/{token}")).text
    assert f"/pay/{token}" not in html
    assert (await client.get(f"/pay/{token}", follow_redirects=False)).status_code == 409

    # 7. Replayed return (re-opened tab): nothing records twice.
    await client.get(f"/pay/{token}/return?session_id=cs_j", follow_redirects=False)
    doc = await _doc_state(client, tok, eid)
    assert len(doc["payments"]) == 1
    assert await _pay_je_entities(session, cid, eid) == [f"je:auto:{eid}:pay:0"]


# ── money truth: books post, races cannot double-book or drop a JE ───────────

async def _pay_je_entities(session, cid, eid) -> list[str]:
    """Ledger entities of the auto-posted payment JEs for this doc, sorted."""
    from sqlalchemy import select
    from celerp.models.ledger import LedgerEntry
    rows = (await session.execute(
        select(LedgerEntry.entity_id).where(
            LedgerEntry.company_id == cid,
            LedgerEntry.entity_id.like(f"je:auto:{eid}:pay:%"),
        ).distinct()
    )).scalars().all()
    return sorted(rows)


@pytest.mark.asyncio
async def test_online_payment_posts_exactly_one_journal_entry(client, session, payments_on):
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    row = await session.get(Projection, (cid, eid))
    await record_stripe_payment(session, cid, eid, dict(row.state),
                                reference="pi_je", amount_minor=107000, currency="usd")
    assert await _pay_je_entities(session, cid, eid) == [f"je:auto:{eid}:pay:0"]


@pytest.mark.asyncio
async def test_manual_payment_racing_online_confirm(client, session, payments_on):
    """The race that silently loses money from the books: a manual payment lands
    while the customer is at Stripe checkout. The online confirm then arrives
    holding a STALE snapshot (as both the return leg and the gateway push do).

    Required outcome: the online charge still records (clamped to the fresh
    outstanding, with the real charged amount kept on the payment record) and
    posts its own journal entry at the NEXT payment index - never colliding
    with the manual payment's JE and never silently deduping away."""
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    cid = _company_id(tok)

    # Snapshot BEFORE the manual payment: this is what the confirm leg holds.
    stale = dict((await session.get(Projection, (cid, eid))).state)

    r = await client.post(f"/docs/{eid}/payment", json={
        "amount": 500.0, "payment_date": "2026-07-13", "bank_account": "1110",
    }, headers=_h(tok))
    assert r.status_code == 200, r.text

    # Online confirm arrives with the stale snapshot and the full charge.
    await record_stripe_payment(session, cid, eid, stale,
                                reference="pi_race", amount_minor=107000, currency="usd")

    doc = await _doc_state(client, tok, eid)
    assert doc["status"] == "paid"
    stripe_pay = next(p for p in doc["payments"] if p.get("reference") == "pi_race")
    assert stripe_pay["amount"] == 570.0            # clamped to fresh outstanding
    assert stripe_pay["charged_amount"] == 1070.0   # the real charge is on record
    # Both payments posted their own JE - distinct indexes, nothing deduped away.
    assert await _pay_je_entities(session, cid, eid) == [
        f"je:auto:{eid}:pay:0", f"je:auto:{eid}:pay:1",
    ]


@pytest.mark.asyncio
async def test_stale_replay_after_return_recorded_is_noop(client, session, payments_on):
    """Gateway push arrives with a snapshot older than the return leg's record:
    the fresh re-read inside the lock sees the reference and quietly no-ops."""
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    stale = dict((await session.get(Projection, (cid, eid))).state)

    await record_stripe_payment(session, cid, eid, dict((await session.get(Projection, (cid, eid))).state),
                                reference="pi_dup", amount_minor=107000, currency="usd")
    # Replay with the PRE-payment snapshot (worst case: passes the caller's own
    # stale pre-check, must be stopped by the locked fresh read).
    assert await record_stripe_payment(session, cid, eid, stale,
                                       reference="pi_dup", amount_minor=107000, currency="usd") is None
    doc = await _doc_state(client, tok, eid)
    assert len([p for p in doc["payments"] if p.get("reference") == "pi_dup"]) == 1
    assert await _pay_je_entities(session, cid, eid) == [f"je:auto:{eid}:pay:0"]


# ── customer-facing surfaces gate on the flag ────────────────────────────────

@pytest.mark.asyncio
async def test_share_view_shows_pay_button_when_enabled(client, payments_on):
    tok = await _register(client)
    _, token = await _payable_invoice(client, tok)
    html = (await client.get(f"/share/{token}")).text
    assert f"/pay/{token}" in html
    assert "Pay" in html


@pytest.mark.asyncio
async def test_share_view_no_pay_button_when_disabled(client):
    tok = await _register(client)
    _, token = await _payable_invoice(client, tok)
    html = (await client.get(f"/share/{token}")).text
    assert f"/pay/{token}" not in html


@pytest.mark.asyncio
async def test_paid_invoice_share_view_drops_pay_bar(client, payments_on):
    """Once nothing is outstanding the share link reverts to a plain view."""
    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    r = await client.post(f"/docs/{eid}/payment", json={
        "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1110",
    }, headers=_h(tok))
    assert r.status_code == 200, r.text
    html = (await client.get(f"/share/{token}")).text
    assert f"/pay/{token}" not in html


@pytest.mark.asyncio
async def test_revoked_link_cannot_start_payment(client, payments_on):
    """A revoked share link must not take money: /pay 404s like /share does."""
    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    assert (await client.delete(f"/docs/{eid}/share", headers=_h(tok))).status_code == 200
    r = await client.get(f"/pay/{token}", follow_redirects=False)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_send_email_includes_pay_link(client, payments_on, monkeypatch):
    import asyncio
    captured = {}
    done = asyncio.Event()
    async def _fake(to, subject, body_html, body_text="", **kw):
        captured.update(html=body_html, text=body_text)
        done.set()
        return True
    monkeypatch.setattr("celerp.services.email.send_email", _fake)

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    r = await client.post(f"/docs/{eid}/send", json={"sent_to": "cust@x.com"}, headers=_h(tok))
    assert r.status_code == 200
    await asyncio.wait_for(done.wait(), timeout=2)
    assert f"/pay/{token}" in captured["html"]
    # The Pay button leads with the amount ("Pay USD 1,070.00"); the plain-text
    # body carries the same link as a "Pay online:" line.
    assert ">Pay USD" in captured["html"]
    assert f"Pay online: " in captured["text"] and f"/pay/{token}" in captured["text"]


# ── merchant Connect settings endpoints ──────────────────────────────────────

@pytest.mark.asyncio
async def test_status_endpoint_reports_connection(client, monkeypatch):
    monkeypatch.setattr("celerp.services.payments.connect_status",
                        lambda: _async({"enabled": True}))
    tok = await _register(client)
    r = await client.get("/payments/status", headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["enabled"] is True


@pytest.mark.asyncio
async def test_connect_endpoint_returns_oauth_url(client, monkeypatch):
    monkeypatch.setattr("celerp.services.payments.connect_start",
                        lambda: _async({"url": "https://connect.stripe.test/oauth"}))
    tok = await _register(client)
    r = await client.post("/payments/connect", headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["url"] == "https://connect.stripe.test/oauth"


@pytest.mark.asyncio
async def test_connect_endpoint_502_when_cloud_unavailable(client, monkeypatch):
    monkeypatch.setattr("celerp.services.payments.connect_start", lambda: _async(None))
    tok = await _register(client)
    r = await client.post("/payments/connect", headers=_h(tok))
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_disconnect_endpoint(client, monkeypatch):
    monkeypatch.setattr("celerp.services.payments.disconnect", lambda: _async(True))
    tok = await _register(client)
    r = await client.post("/payments/disconnect", headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["disconnected"] is True

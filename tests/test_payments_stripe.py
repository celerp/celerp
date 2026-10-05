# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Online invoice payment via Stripe Connect, brokered by Celerp Connect (mocked).

The instance holds no Stripe credentials - checkout creation and Connect onboarding
are cloud calls, mocked here at the payments-service boundary. A payment is recorded
only when Celerp Cloud delivers it, on the books its payment page opened with.
The "payments enabled" gate is the gateway feature flag delivered on the handshake.
"""

from __future__ import annotations

import datetime

import pytest
from httpx import AsyncClient

from celerp.services.company_lock import locked_company


# When Stripe reported a payment paid, and the books its page opened with.
PAID = datetime.datetime(2026, 7, 13, 9, 0, tzinfo=datetime.timezone.utc)
BOOKS = {"deposit_account": "1110", "timezone": "UTC", "base_currency": "USD", "rate": "1"}


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
def payments_on(monkeypatch, session):
    """Merchant has a connected account: the cloud feature flag is on and the
    instance is cloud-connected (public URL set). The payment intake's own sessions
    join the test's transaction, so it sees what the test wrote."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from celerp.config import settings as cfg
    monkeypatch.setattr("celerp.services.payments._own_session", lambda: AsyncSession(
        bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"))
    monkeypatch.setattr(cfg, "celerp_public_url", "https://acme.celerp.com")
    monkeypatch.setattr("celerp.services.payments.payments_enabled", lambda: True)


# ── Stripe amount units ──────────────────────────────────────────────────────
# Stripe's own rules (docs.stripe.com/currencies), not the accounting precision:
# ordinary currencies, IDR among them, are two-decimal; the zero-decimal list is
# charged as is; ISK and UGX are two-decimal in the API but whole units only; HUF and
# TWD charge two decimals; three-decimal amounts end in 0.

_STRIPE_ZERO_DECIMAL = ("BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF",
                        "VND", "VUV", "XAF", "XOF", "XPF")
_STRIPE_THREE_DECIMAL = ("BHD", "JOD", "KWD", "OMR", "TND")

_EXACT = [
    ("12.34", "USD", 1234), ("1070.00", "THB", 107000), ("100000", "IDR", 10000000),
    ("10.45", "HUF", 1045), ("800.45", "TWD", 80045),
    ("5", "ISK", 500), ("5", "UGX", 500),
    *[("1234", c, 1234) for c in _STRIPE_ZERO_DECIMAL],
    *[("5.120", c, 5120) for c in _STRIPE_THREE_DECIMAL],
]


@pytest.mark.parametrize("amount,currency,api", _EXACT, ids=[f"{c}-{a}" for a, c, _ in _EXACT])
def test_an_amount_converts_to_stripe_and_back_exactly(amount, currency, api):
    from decimal import Decimal
    from celerp.services.payments import from_stripe_amount, to_stripe_amount
    assert to_stripe_amount(Decimal(amount), currency) == api
    assert to_stripe_amount(Decimal(amount), currency.lower()) == api
    assert from_stripe_amount(api, currency) == Decimal(amount)


_NOT_CHARGEABLE = [
    ("12.345", "USD"), ("0.5", "IDR"), ("5.5", "ISK"), ("5.5", "UGX"),
    *[("1234.5", c) for c in _STRIPE_ZERO_DECIMAL],
    *[("5.124", c) for c in _STRIPE_THREE_DECIMAL],
    ("0", "USD"), ("-1", "USD"),
]


@pytest.mark.parametrize("amount,currency", _NOT_CHARGEABLE, ids=[f"{c}-{a}" for a, c in _NOT_CHARGEABLE])
def test_an_amount_stripe_cannot_charge_exactly_is_refused_not_rounded(amount, currency):
    from decimal import Decimal
    from celerp.services.payments import to_stripe_amount
    with pytest.raises(ValueError):
        to_stripe_amount(Decimal(amount), currency)


_NOT_RECORDABLE = [
    (550, "UGX"),        # 5.50 UGX: Stripe charges whole shillings only
    (550, "ISK"),        # 5.50 ISK: likewise
    (10000050, "IDR"),   # 100000.50 IDR: finer than the books keep IDR
    *[(5124, c) for c in _STRIPE_THREE_DECIMAL],  # a three-decimal amount not ending in 0
    (0, "USD"), (-100, "USD"),
]


@pytest.mark.parametrize("api,currency", _NOT_RECORDABLE, ids=[f"{c}-{a}" for a, c in _NOT_RECORDABLE])
def test_a_stripe_amount_that_does_not_convert_exactly_is_refused(api, currency):
    from celerp.services.payments import from_stripe_amount
    with pytest.raises(ValueError):
        from_stripe_amount(api, currency)


async def _idr_invoice(client: AsyncClient, tok: str, total: float = 100000.0) -> tuple[str, str]:
    """A finalized, shared invoice for *total* IDR in a company that keeps its books in IDR."""
    assert (await client.patch("/companies/me", json={"settings": {"currency": "IDR"}},
                               headers=_h(tok))).status_code == 200
    r = await client.post("/docs", json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": total}],
        "subtotal": total, "tax": 0.0, "total": total, "currency": "IDR",
    }, headers=_h(tok))
    eid = r.json()["id"]
    assert (await client.post(f"/docs/{eid}/finalize", headers=_h(tok))).status_code == 200
    token = (await client.post(f"/docs/{eid}/share", headers=_h(tok))).json()["token"]
    return eid, token


@pytest.mark.asyncio
async def test_a_100000_idr_invoice_opens_a_payment_for_100000_idr(client, payments_on, monkeypatch):
    """Stripe reads IDR as two-decimal: 100,000 IDR is the API amount 10000000."""
    captured = {}

    async def _mk(**kw):
        captured.update(kw)
        return {"url": "https://stripe.test/cs_idr"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", _mk)
    tok = await _register(client)
    _, token = await _idr_invoice(client, tok)

    r = await client.get(f"/pay/{token}", follow_redirects=False)

    assert r.status_code == 303
    assert captured["amount_minor"] == 10000000 and captured["currency"] == "IDR"


@pytest.mark.asyncio
async def test_a_payment_of_a_hundredth_of_an_idr_invoice_does_not_mark_it_paid(client, session, payments_on):
    """Cloud reports amount_total 100000 for an IDR payment: that is 1,000 IDR, not the
    invoice's 100,000."""
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _idr_invoice(client, tok)
    delivery = {"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_idr",
                "amount_minor": 100000, "currency": "idr", "paid_at": PAID.isoformat(),
                "context": {**BOOKS, "base_currency": "IDR"}, "managed": True}

    assert await receive_payment(delivery) is True

    doc = await _doc_state(client, tok, eid)
    assert doc["status"] != "paid"
    assert [p["amount"] for p in doc["payments"]] == [1000.0]
    assert doc["amount_outstanding"] == 99000


@pytest.mark.parametrize("change", [{"amount_minor": 107000.9}, {"amount_minor": True}, {"amount_minor": "107000"},
                                    {"amount_minor": -107000}, {"amount_minor": 0}, {"amount_minor": None},
                                    {"currency": None}, {"currency": ""}, {"currency": 840}],
                         ids=lambda c: repr(c))
@pytest.mark.asyncio
async def test_a_delivery_without_a_whole_amount_and_a_currency_records_nothing(client, session, payments_on,
                                                                               change):
    """Like a refund, a payment is recorded only from a whole positive amount in a named
    currency, as Stripe reports it: never read as some other amount or as dollars."""
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    delivery = {"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_malformed",
                "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                "context": dict(BOOKS), "managed": True, **change}

    assert await receive_payment(delivery) is False

    assert not (await _doc_state(client, tok, eid)).get("payments")
    assert (await client.get("/payments/unmatched", headers=_h(tok))).json()["items"] == []


@pytest.mark.asyncio
async def test_a_stripe_amount_the_books_cannot_hold_is_kept_among_the_unmatched(client, session, payments_on):
    """100,000.50 IDR cannot be recorded on books that keep IDR in whole rupiah: the
    whole charge is kept among the unmatched payments, never rounded onto the invoice."""
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _idr_invoice(client, tok)
    delivery = {"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_idr_frac",
                "amount_minor": 10000050, "currency": "idr", "paid_at": PAID.isoformat(),
                "context": {**BOOKS, "base_currency": "IDR"}, "managed": True}

    assert await receive_payment(delivery) is True

    doc = await _doc_state(client, tok, eid)
    assert not doc.get("payments") and doc["status"] != "paid"
    assert await _pay_je_entities(session, _company_id(tok), eid) == []
    r = await client.get("/payments/unmatched", headers=_h(tok))
    assert [(p["reference"], p["amount"], p["currency"]) for p in r.json()["items"]] == [
        ("pi_idr_frac", 100000.5, "IDR")]


@pytest.mark.asyncio
async def test_an_invoice_stripe_cannot_charge_exactly_does_not_open_a_payment(client, payments_on, monkeypatch):
    """A balance of 1,000.50 CLP: Stripe charges CLP in whole pesos, so the payment page
    is refused with a reason rather than charging a rounded amount."""
    opened = []

    async def _mk(**kw):
        opened.append(kw)
        return {"url": "https://stripe.test/cs_clp"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", _mk)
    tok = await _register(client)
    assert (await client.patch("/companies/me", json={"settings": {"currency": "CLP"}},
                               headers=_h(tok))).status_code == 200
    r = await client.post("/docs", json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 1000.5}],
        "subtotal": 1000.5, "tax": 0.0, "total": 1000.5, "currency": "CLP",
    }, headers=_h(tok))
    eid = r.json()["id"]
    assert (await client.post(f"/docs/{eid}/finalize", headers=_h(tok))).status_code == 200
    token = (await client.post(f"/docs/{eid}/share", headers=_h(tok))).json()["token"]

    r = await client.get(f"/pay/{token}", follow_redirects=False)

    assert r.status_code == 409
    assert "CLP" in r.json()["detail"] and "online" in r.json()["detail"]
    assert opened == []


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
    assert captured["entity_id"] == eid
    assert captured["company_id"] == _company_id(tok)
    assert captured["share_token"] == token
    assert captured["generation"] == 0  # the installation's payment generation
    assert captured["context"] == BOOKS  # the books the payment will be recorded on


@pytest.mark.asyncio
async def test_checkout_request_to_cloud_carries_generation_and_books(client, payments_on, monkeypatch):
    """Celerp Cloud opens a payment page only for a request that names the
    installation's payment generation and the books the payment will be recorded on."""
    import httpx
    sent = []
    async def _cloud(method, path, *, json=None, **kw):
        sent.append((method, path, json))
        return httpx.Response(200, json={"url": "https://stripe.test/cs_1"})
    monkeypatch.setattr("celerp.services.cloud_entitlement.authenticated_request", _cloud)

    tok = await _register(client)
    _, token = await _payable_invoice(client, tok)
    r = await client.get(f"/pay/{token}", follow_redirects=False)

    assert r.status_code == 303
    (body,) = [json for method, path, json in sent if path == "/billing/connect/checkout"]
    assert body["generation"] == 0
    assert body["context"] == BOOKS


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


# ── the customer's return from Stripe ────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_return_from_stripe_shows_the_invoice_and_records_nothing(client, payments_on, monkeypatch):
    received = []

    async def receive(payload):
        received.append(payload)
        return True
    monkeypatch.setattr("celerp.services.payments.receive_payment", receive)
    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)

    r = await client.get(f"/pay/{token}/return?session_id=cs_1", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == f"/share/{token}"
    assert received == []
    doc = await _doc_state(client, tok, eid)
    assert doc["status"] != "paid" and not doc.get("payments")


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
                                reference="pi_push", amount_minor=107000, currency="usd",
                                paid_at=PAID, context=BOOKS, managed=True)
    await session.commit()
    doc = await _doc_state(client, tok, eid)
    assert doc["status"] == "paid"
    assert len([p for p in doc["payments"] if p.get("reference") == "pi_push"]) == 1

    row2 = await session.get(Projection, (cid, eid))
    await record_stripe_payment(session, cid, eid, dict(row2.state),
                                reference="pi_push", amount_minor=107000, currency="usd",
                                paid_at=PAID, context=BOOKS, managed=True)
    await session.commit()
    doc2 = await _doc_state(client, tok, eid)
    assert len([p for p in doc2["payments"] if p.get("reference") == "pi_push"]) == 1


@pytest.mark.asyncio
async def test_a_payment_clears_to_the_deposit_account_its_page_opened_with(client, session, payments_on,
                                                                          monkeypatch):
    """The deposit GL account is the company setting when the payment page opens,
    defaulting to Cash; changing the setting later does not move the payment."""
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment
    opened = {}

    async def checkout(**kw):
        opened.update(kw["context"])
        return {"url": "https://stripe.test/cs_1"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", checkout)
    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    cid = _company_id(tok)

    async def deposit_to(code):
        company = await locked_company(session, cid)
        company.settings = {**(company.settings or {}), "stripe_deposit_account": code}
        await session.commit()
    await deposit_to("1111")
    assert (await client.get(f"/pay/{token}", follow_redirects=False)).status_code == 303
    await deposit_to("1110")

    row = await session.get(Projection, (cid, eid))
    await record_stripe_payment(session, cid, eid, dict(row.state), reference="pi_acct", amount_minor=107000,
                                currency="usd", paid_at=PAID, context=opened, managed=True)
    await session.commit()
    doc = await _doc_state(client, tok, eid)
    pay_entry = next(p for p in doc["payments"] if p.get("reference") == "pi_acct")
    assert opened["deposit_account"] == "1111" and pay_entry["bank_account"] == "1111"


# ── where online payments are deposited ──────────────────────────────────────

async def _bank(client: AsyncClient, tok: str, *, active: bool = True) -> str:
    """A bank account's chart code; deactivated when not *active*."""
    r = await client.post("/accounting/bank-accounts", json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
        "currency": "USD"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    bank = r.json()
    if not active:
        assert (await client.patch(f"/accounting/bank-accounts/{bank['id']}", json={"is_active": False},
                                   headers=_h(tok))).status_code == 200
    return bank["chart_account_code"]


async def _deposit_to(session, tok: str, code: str) -> None:
    """Store the online deposit account directly, as any writer of the company's settings could."""
    company = await locked_company(session, _company_id(tok))
    company.settings = {**(company.settings or {}), "stripe_deposit_account": code}
    await session.commit()


async def _chart_changed(session, tok: str, code: str, **values) -> None:
    """Change the chart account behind *code* directly, as books written before the
    chart refused it can hold: archived, or given another type."""
    import uuid
    from sqlalchemy import update
    from celerp_accounting.models import Account
    await session.execute(update(Account).where(
        Account.company_id == uuid.UUID(_company_id(tok)), Account.code == code).values(**values))
    await session.commit()


_REFUSED_DEPOSITS = ["revenue", "inactive-bank", "bank-on-archived-account", "bank-on-revenue-account",
                     "archived-cash", "cash-made-liability"]


async def _refused_deposit(client, session, tok, kind: str) -> str:
    if kind == "revenue":
        return "4100"
    if kind == "inactive-bank":
        return await _bank(client, tok, active=False)
    code = "1110" if "cash" in kind else await _bank(client, tok)
    if "archived" in kind:
        await _chart_changed(session, tok, code, is_active=False)
    else:
        await _chart_changed(session, tok, code, account_type="liability" if code == "1110" else "revenue")
    return code


@pytest.mark.parametrize("kind", _REFUSED_DEPOSITS)
@pytest.mark.asyncio
async def test_a_payment_page_is_refused_when_payments_would_deposit_outside_cash_or_an_active_bank(
        client, session, payments_on, monkeypatch, kind):
    opened = []

    async def _mk(**kw):
        opened.append(kw)
        return {"url": "https://stripe.test/cs_1"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", _mk)
    tok = await _register(client)
    _, token = await _payable_invoice(client, tok)
    await _deposit_to(session, tok, await _refused_deposit(client, session, tok, kind))

    r = await client.get(f"/pay/{token}", follow_redirects=False)

    assert r.status_code == 409
    assert r.json()["detail"] == "Online payment is not set up for this invoice. Please contact the business that sent it."
    assert opened == []


@pytest.mark.parametrize("kind", ["cash", "active-bank"])
@pytest.mark.asyncio
async def test_a_payment_page_opens_for_cash_or_an_active_bank(client, session, payments_on, monkeypatch, kind):
    opened = {}

    async def _mk(**kw):
        opened.update(kw["context"])
        return {"url": "https://stripe.test/cs_1"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", _mk)
    tok = await _register(client)
    _, token = await _payable_invoice(client, tok)
    code = "1110" if kind == "cash" else await _bank(client, tok)
    await _deposit_to(session, tok, code)

    assert (await client.get(f"/pay/{token}", follow_redirects=False)).status_code == 303
    assert opened["deposit_account"] == code


@pytest.mark.parametrize("kind", _REFUSED_DEPOSITS)
@pytest.mark.asyncio
async def test_a_payment_whose_deposit_account_is_not_cash_or_an_active_bank_is_kept_among_the_unmatched(
        client, session, payments_on, kind):
    """Delivered on books naming Revenue, or a bank account deactivated while the
    customer was paying: never posted there, kept whole among the unmatched, and
    Cloud is told it arrived."""
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    code = await _refused_deposit(client, session, tok, kind)
    delivery = {"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_dep",
                "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                "context": {**BOOKS, "deposit_account": code}, "managed": True}

    assert await receive_payment(delivery) is True

    doc = await _doc_state(client, tok, eid)
    assert not doc.get("payments") and doc["status"] != "paid"
    assert await _pay_je_entities(session, _company_id(tok), eid) == []
    r = await client.get("/payments/unmatched", headers=_h(tok))
    assert [p["reference"] for p in r.json()["items"]] == ["pi_dep"]


@pytest.mark.asyncio
async def test_a_payment_to_an_active_bank_is_recorded_there(client, session, payments_on):
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    code = await _bank(client, tok)
    delivery = {"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_bank",
                "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                "context": {**BOOKS, "deposit_account": code}, "managed": True}

    assert await receive_payment(delivery) is True

    doc = await _doc_state(client, tok, eid)
    assert doc["status"] == "paid" and [p["bank_account"] for p in doc["payments"]] == [code]


@pytest.mark.parametrize("key", ["stripe_deposit_account", "woocommerce_deposit_account"])
@pytest.mark.parametrize("value", ["4100", "9999"])
@pytest.mark.asyncio
async def test_the_online_deposit_setting_refuses_an_account_that_is_not_cash_or_an_active_bank(
        client, key, value):
    tok = await _register(client)
    r = await client.patch("/companies/me", json={"settings": {key: value}},
                           headers=_h(tok))
    assert r.status_code == 422
    assert "Cash (1110) or an active bank account" in r.json()["detail"]


@pytest.mark.parametrize("key", ["stripe_deposit_account", "woocommerce_deposit_account"])
@pytest.mark.parametrize("change", [{"is_active": False}, {"account_type": "revenue"}])
@pytest.mark.asyncio
async def test_the_online_deposit_setting_refuses_a_bank_whose_chart_account_cannot_hold_money(
        client, session, key, change):
    tok = await _register(client)
    code = await _bank(client, tok)
    await _chart_changed(session, tok, code, **change)
    r = await client.patch("/companies/me", json={"settings": {key: code}},
                           headers=_h(tok))
    assert r.status_code == 422, r.text
    assert "Cash (1110) or an active bank account" in r.json()["detail"]


@pytest.mark.parametrize("key", ["stripe_deposit_account", "woocommerce_deposit_account"])
@pytest.mark.asyncio
async def test_the_online_deposit_setting_takes_cash_an_active_bank_or_the_default(client, key):
    tok = await _register(client)
    code = await _bank(client, tok)
    inactive = await _bank(client, tok, active=False)
    for value in ("1110", code, ""):
        r = await client.patch("/companies/me", json={"settings": {key: value}},
                               headers=_h(tok))
        assert r.status_code == 200, (value, r.text)
    r = await client.patch("/companies/me", json={"settings": {key: inactive}},
                           headers=_h(tok))
    assert r.status_code == 422


@pytest.mark.parametrize("key", ["stripe_deposit_account", "woocommerce_deposit_account"])
@pytest.mark.parametrize("value", [1110, 0, False, True, [], ["1110"], {}, {"code": "1110"}, 1110.0])
@pytest.mark.asyncio
async def test_the_online_deposit_setting_refuses_a_value_that_is_not_an_account_code(client, key, value):
    tok = await _register(client)
    r = await client.patch("/companies/me", json={"settings": {key: value}}, headers=_h(tok))
    assert r.status_code == 422, (value, r.status_code, r.text)
    assert "account code" in r.json()["detail"]
    stored = (await client.get("/companies/me", headers=_h(tok))).json().get("settings", {})
    assert key not in stored


# ── the whole journey, end to end ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_online_payment_journey(client, session, payments_on, monkeypatch):
    """Merchant sends an invoice; customer pays it from the email; money lands
    in the books; the link reverts to view-only; replays change nothing.

    Chains every hop of the real flow: send email (Pay leads with the amount
    due) -> share page carries the pay bar -> /pay creates the checkout with
    the exact balance in minor units and the books it is recorded on -> the
    return from Stripe records nothing -> Celerp Cloud's delivery records the payment ->
    status paid, journal entry posted, pay bar gone, /pay refuses further
    charges -> a repeated return or delivery records nothing."""
    import asyncio as _asyncio

    sent = {}
    email_done = _asyncio.Event()
    async def _fake_send(to, subject, body_html, body_text="", **kw):
        sent.update(html=body_html, text=body_text, subject=subject)
        email_done.set()
        return True, None
    monkeypatch.setattr("celerp.services.email.send_email", _fake_send)

    checkout = {}
    async def _fake_checkout(**kw):
        checkout.update(kw)
        return {"url": "https://stripe.test/cs_journey"}
    monkeypatch.setattr("celerp.services.payments.create_checkout", _fake_checkout)

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)

    # 1. Merchant clicks Send: the email leads with a Pay button for the balance due.
    r = await client.post(f"/docs/{eid}/send", json={"sent_to": "cust@shop.example"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    await _asyncio.wait_for(email_done.wait(), timeout=2)
    assert f"/pay/{token}" in sent["html"] and ">Pay USD 1,070.00<" in sent["html"]
    assert f"/pay/{token}" in sent["text"]

    # 2. Customer opens the invoice: print layout with the amount-due pay bar
    # (the on-page bar formats with the currency symbol; the email uses the code).
    html = (await client.get(f"/share/{token}")).text
    assert f"/pay/{token}" in html and "Pay $1,070.00 now" in html

    # 3. Customer clicks Pay: checkout for the exact balance, with the books it records on.
    r = await client.get(f"/pay/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://stripe.test/cs_journey"
    assert checkout["amount_minor"] == 107000 and checkout["currency"] == "USD"
    assert checkout["entity_id"] == eid
    assert checkout["company_id"] == _company_id(tok)
    assert checkout["share_token"] == token and checkout["context"] == BOOKS

    # 4. The customer returns from Stripe: the invoice shows, nothing is recorded yet.
    r = await client.get(f"/pay/{token}/return?session_id=cs_j", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/share/{token}"
    assert not (await _doc_state(client, tok, eid)).get("payments")
    # Celerp Cloud delivers the payment Stripe reported paid.
    from celerp.services.payments import receive_payment
    delivery = {"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_journey",
                "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                "context": checkout["context"], "managed": True}
    assert await receive_payment(delivery) is True

    # 5. Money truth: invoice paid, exactly one payment, exactly one posted JE.
    doc = await _doc_state(client, tok, eid)
    assert doc["status"] == "paid" and doc["amount_outstanding"] == 0
    assert [(p["reference"], p["payment_date"]) for p in doc["payments"]] == [("pi_journey", "2026-07-13")]
    cid = _company_id(tok)
    assert await _pay_je_entities(session, cid, eid) == [f"je:auto:{eid}:pay:0"]

    # 6. The link reverts to view-only and cannot take more money.
    html = (await client.get(f"/share/{token}")).text
    assert f"/pay/{token}" not in html
    assert (await client.get(f"/pay/{token}", follow_redirects=False)).status_code == 409

    # 7. A re-opened return tab, or the same delivery again: nothing records twice.
    await client.get(f"/pay/{token}/return?session_id=cs_j", follow_redirects=False)
    assert await receive_payment(delivery) is True
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
                                reference="pi_je", amount_minor=107000, currency="usd",
                                paid_at=PAID, context=BOOKS, managed=True)
    await session.commit()
    assert await _pay_je_entities(session, cid, eid) == [f"je:auto:{eid}:pay:0"]


@pytest.mark.asyncio
async def test_manual_payment_racing_online_confirm(client, session, payments_on):
    """A manual payment lands while the customer is at Stripe checkout. The online
    delivery then arrives holding a STALE snapshot and a charge larger than what is
    still owed.

    The invoice refuses the charge whole, against what it owes under its row lock:
    nothing is clamped onto it and the manual payment stands alone (the intake then
    keeps the whole charge among the unmatched payments)."""
    from fastapi import HTTPException
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    cid = _company_id(tok)

    # Snapshot BEFORE the manual payment: this is what the delivery holds.
    stale = dict((await session.get(Projection, (cid, eid))).state)

    r = await client.post(f"/docs/{eid}/payment", json={
        "amount": 500.0, "payment_date": "2026-07-13", "bank_account": "1110",
    }, headers=_h(tok))
    assert r.status_code == 200, r.text

    with pytest.raises(HTTPException) as refused:
        await record_stripe_payment(session, cid, eid, stale,
                                    reference="pi_race", amount_minor=107000, currency="usd",
                                    paid_at=PAID, context=BOOKS, managed=True)
    assert refused.value.status_code == 409
    assert "exceeds amount outstanding" in refused.value.detail
    await session.rollback()

    doc = await _doc_state(client, tok, eid)
    assert [p["amount"] for p in doc["payments"] if p.get("status") != "deleted"] == [500.0]
    assert await _pay_je_entities(session, cid, eid) == [f"je:auto:{eid}:pay:0"]


@pytest.mark.asyncio
async def test_stale_repeated_delivery_is_noop(client, session, payments_on):
    """A repeated delivery arrives with a snapshot older than the first one's record:
    the fresh re-read inside the lock sees the reference and quietly no-ops."""
    from celerp.models.projections import Projection
    from celerp_docs.routes_payments import record_stripe_payment

    tok = await _register(client)
    eid, token = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    stale = dict((await session.get(Projection, (cid, eid))).state)

    await record_stripe_payment(session, cid, eid, dict((await session.get(Projection, (cid, eid))).state),
                                reference="pi_dup", amount_minor=107000, currency="usd",
                                paid_at=PAID, context=BOOKS, managed=True)
    # Replay with the PRE-payment snapshot (worst case: passes the caller's own
    # stale pre-check, must be stopped by the locked fresh read).
    assert await record_stripe_payment(session, cid, eid, stale,
                                       reference="pi_dup", amount_minor=107000, currency="usd",
                                       paid_at=PAID, context=BOOKS, managed=True) is None
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
    r = await client.post(f"/docs/{eid}/send", json={"sent_to": "cust@shop.example"}, headers=_h(tok))
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
@pytest.mark.parametrize("answer", [{"disconnected": False, "state": "disconnecting"},
                                    {"disconnected": False, "state": "revoked"},
                                    {"disconnected": True, "state": "disconnected"}])
async def test_disconnect_endpoint_answers_what_cloud_did(client, monkeypatch, answer):
    sent = []

    async def cloud_post(path, payload):
        sent.append(path)
        return dict(answer)
    monkeypatch.setattr("celerp.services.payments._cloud_post", cloud_post)
    tok = await _register(client)
    r = await client.post("/payments/disconnect", headers=_h(tok))
    assert r.status_code == 200
    assert r.json() == answer
    assert sent == ["/billing/connect/disconnect"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, {"disconnected": "yes"}, {"state": "connected"}])
async def test_disconnect_endpoint_502_when_cloud_does_not_answer(client, monkeypatch, answer):
    monkeypatch.setattr("celerp.services.payments._cloud_post", lambda path, payload: _async(answer))
    tok = await _register(client)
    r = await client.post("/payments/disconnect", headers=_h(tok))
    assert r.status_code == 502
    assert r.json()["detail"] == "Could not disconnect Stripe"


@pytest.mark.asyncio
async def test_unmatched_payments_are_listed_newest_first(client, session):
    import datetime
    from celerp.models.payment_closure import UnmatchedPayment
    tok = await _register(client)
    old = datetime.datetime(2026, 9, 28, 9, 0, tzinfo=datetime.timezone.utc)
    session.add_all([
        UnmatchedPayment(reference="pi_old", amount_minor=107000, currency="USD", former_company="c-old",
                         document="doc:1", received_at=old, paid_at=old - datetime.timedelta(days=3)),
        UnmatchedPayment(reference="pi_new", amount_minor=5000, currency="JPY", former_company="c-new",
                         document="doc:2", received_at=old + datetime.timedelta(days=1)),
    ])
    await session.commit()

    r = await client.get("/payments/unmatched", headers=_h(tok))

    assert r.status_code == 200
    assert r.json() == {"items": [
        {"reference": "pi_new", "amount": 5000, "currency": "JPY", "company_id": "c-new",
         "document_id": "doc:2", "received_at": "2026-09-29T09:00:00+00:00", "paid_at": None},
        {"reference": "pi_old", "amount": 1070.0, "currency": "USD", "company_id": "c-old",
         "document_id": "doc:1", "received_at": "2026-09-28T09:00:00+00:00",
         "paid_at": "2026-09-25T09:00:00+00:00"},
    ], "refunds": []}


@pytest.mark.asyncio
async def test_unmatched_payments_need_the_installation_owner(client):
    r = await client.get("/payments/unmatched")
    assert r.status_code in (401, 403)


# ── A Stripe payment is changed only in Stripe ──────────────────────────────

STRIPE_OWNED = "This payment was received through Stripe, so it can only be refunded or reversed in Stripe."


def _remove_payment(client, tok, eid, action):
    if action == "refund":
        return client.post(f"/docs/{eid}/refund", headers=_h(tok), json={
            "payment_index": 0, "amount": 100.0, "payment_date": "2026-07-14"})
    if action == "void":
        return client.post(f"/docs/{eid}/void-payment", headers=_h(tok), json={"payment_index": 0})
    return client.delete(f"/docs/{eid}/payments/0", headers=_h(tok))


async def _ledger(session, cid, eid) -> list[str]:
    from sqlalchemy import text
    return list((await session.scalars(text(
        "SELECT event_type FROM ledger WHERE company_id = CAST(:c AS uuid) "
        "AND (entity_id = :e OR entity_id LIKE :j) ORDER BY id"),
        {"c": cid, "e": eid, "j": f"je:auto:{eid}:%"})).all())


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["refund", "void", "delete"])
async def test_a_stripe_payment_cannot_be_refunded_voided_or_deleted_here(client, session, payments_on, action):
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    assert await receive_payment({"company_id": cid, "entity_id": eid, "reference": "pi_card",
                                  "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": BOOKS, "managed": True}) is True
    before, events = await _doc_state(client, tok, eid), await _ledger(session, cid, eid)
    assert before["status"] == "paid" and [p["method"] for p in before["payments"]] == ["stripe"]
    assert before["payments"][0]["held_by"] == "stripe"

    r = await _remove_payment(client, tok, eid, action)

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == STRIPE_OWNED
    assert await _doc_state(client, tok, eid) == before
    assert await _ledger(session, cid, eid) == events


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["refund", "void", "delete"])
@pytest.mark.parametrize("method", ["card", "stripe"])
async def test_a_payment_entered_by_hand_can_still_be_refunded_voided_or_deleted(client, session, method, action):
    # Typing "stripe" as the method does not hand the payment to Stripe: only the
    # payments Stripe itself reported are changed there.
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    r = await client.post(f"/docs/{eid}/payment", headers=_h(tok), json={
        "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1110", "method": method,
        "reference": "pi_card"})
    assert r.status_code == 200, r.text

    assert "held_by" not in (await _doc_state(client, tok, eid))["payments"][0]

    r = await _remove_payment(client, tok, eid, action)

    assert r.status_code == 200, r.text
    doc = await _doc_state(client, tok, eid)
    assert doc["status"] != "paid"


def _payment_history(payment: dict) -> str:
    from fasthtml.common import to_xml
    from ui.routes.documents import _payment_section
    return to_xml(_payment_section({
        "entity_id": "doc:inv", "doc_type": "invoice", "status": "paid", "currency": "USD",
        "total": 1070.0, "amount_paid": 1070.0, "amount_outstanding": 0.0,
        "payments": [{"index": 0, "amount": 1070.0, "method": "stripe", "reference": "pi_card",
                      "payment_date": "2026-07-13", "bank_account": "1119", "status": "active"} | payment],
    }, bank_accounts=[]))


def test_a_stripe_payment_offers_no_refund_or_void_and_says_where_to_do_it():
    from ui.i18n import t
    html = _payment_history({"held_by": "stripe"})
    assert "/docs/doc:inv/refund" not in html and "/docs/doc:inv/void-payment" not in html
    assert t("documents.refund_in_stripe") in html


def test_a_payment_no_longer_linked_to_stripe_says_so_and_is_refunded_here():
    from ui.i18n import t
    html = _payment_history({"stripe_released_at": "2026-09-10T09:00:00+00:00"})
    assert t("documents.stripe_released") in html
    assert "/docs/doc:inv/refund" in html and "/docs/doc:inv/void-payment" in html
    assert t("documents.refund_in_stripe") not in html
    assert t("documents.stripe_released") not in _payment_history({"held_by": "stripe"})
    assert t("documents.stripe_released") not in _payment_history({})


def test_a_payment_entered_by_hand_as_stripe_keeps_its_refund_and_void():
    from ui.i18n import t
    html = _payment_history({})
    assert "/docs/doc:inv/refund" in html and "/docs/doc:inv/void-payment" in html
    assert t("documents.refund_in_stripe") not in html


def test_the_online_payments_offer_names_no_payment_method_in_any_language():
    import json
    from pathlib import Path
    card = ("card", "karte", "tarjeta", "carte", "kartu", "carta", "カード", "cartão", "บัตร", "thẻ", "بطاقة", "ካርድ")
    locales = sorted((Path(__file__).parents[1] / "ui" / "locales").glob("*.json"))
    assert len(locales) == 12
    for path in locales:
        text = json.loads(path.read_text(encoding="utf-8"))["pay.upgrade_desc"].lower()
        assert not any(word in text for word in card), (path.name, text)


@pytest.mark.asyncio
async def test_a_payment_entered_by_hand_stays_the_users_when_stripe_later_reports_it(client, session, payments_on):
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    r = await client.post(f"/docs/{eid}/payment", headers=_h(tok), json={
        "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1110", "method": "stripe",
        "reference": "pi_card"})
    assert r.status_code == 200, r.text
    assert await receive_payment({"company_id": cid, "entity_id": eid, "reference": "pi_card",
                                  "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": BOOKS, "managed": True}) is True
    assert "held_by" not in (await _doc_state(client, tok, eid))["payments"][0]

    r = await _remove_payment(client, tok, eid, "void")

    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_stripe_payment_the_released_version_recorded_is_the_companys_to_manage(
        client, session, payments_on):
    """The released version recorded online payments without an index or a mark that
    Stripe manages them, and Stripe never sends their refunds: they are voided,
    deleted or refunded here like any other payment."""
    from sqlalchemy import text
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    assert await receive_payment({"company_id": cid, "entity_id": eid, "reference": "pi_card",
                                  "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": BOOKS, "managed": True}) is True
    await session.execute(text(
        "UPDATE ledger SET data = (data::jsonb - 'index' - 'stripe_managed')::json "
        "WHERE company_id = CAST(:c AS uuid) AND entity_id = :e AND source = 'stripe'"), {"c": cid, "e": eid})
    await session.commit()

    assert "held_by" not in (await _doc_state(client, tok, eid))["payments"][0]
    r = await _remove_payment(client, tok, eid, "void")
    assert r.status_code == 200, r.text

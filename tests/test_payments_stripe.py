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
from ui.i18n import t



# When Stripe reported a payment paid, and the books its page opened with.
PAID = datetime.datetime(2026, 7, 13, 9, 0, tzinfo=datetime.timezone.utc)
BOOKS = {"deposit_account": "1111", "timezone": "UTC", "base_currency": "USD", "rate": "1"}


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
    ("12.345", "USD"), ("0.505", "IDR"), ("5.5", "ISK"), ("5.5", "UGX"),
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
    *[(5124, c) for c in _STRIPE_THREE_DECIMAL],  # a three-decimal amount not ending in 0
    (0, "USD"), (-100, "USD"),
]


@pytest.mark.parametrize("api,currency", _NOT_RECORDABLE, ids=[f"{c}-{a}" for a, c in _NOT_RECORDABLE])
def test_a_stripe_amount_that_does_not_convert_exactly_is_refused(api, currency):
    from celerp.services.payments import from_stripe_amount
    with pytest.raises(ValueError):
        from_stripe_amount(api, currency)


async def _invoice(client: AsyncClient, tok: str, total: float = 100000.0,
                       currency: str = "IDR") -> tuple[str, str]:
    """A finalized, shared invoice for *total* in a company that keeps its books in *currency*."""
    assert (await client.patch("/companies/me/books", json={"currency": currency},
                               headers=_h(tok))).status_code == 200
    r = await client.post("/docs", json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": total}],
        "subtotal": total, "tax": 0.0, "total": total, "currency": currency,
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
    _, token = await _invoice(client, tok)

    r = await client.get(f"/pay/{token}", follow_redirects=False)

    assert r.status_code == 303
    assert captured["amount_minor"] == 10000000 and captured["currency"] == "IDR"


@pytest.mark.asyncio
async def test_a_payment_of_a_hundredth_of_an_idr_invoice_does_not_mark_it_paid(client, session, payments_on):
    """Cloud reports amount_total 100000 for an IDR payment: that is 1,000 IDR, not the
    invoice's 100,000."""
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _invoice(client, tok)
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
    """100,000.50 UGX cannot be recorded on books that keep UGX in whole shillings: the
    whole charge is kept among the unmatched payments, never rounded onto the invoice."""
    from celerp.services.payments import receive_payment
    tok = await _register(client)
    eid, _ = await _invoice(client, tok, currency="UGX")
    delivery = {"company_id": _company_id(tok), "entity_id": eid, "reference": "pi_ugx_frac",
                "amount_minor": 10000050, "currency": "ugx", "paid_at": PAID.isoformat(),
                "context": {**BOOKS, "base_currency": "UGX"}, "managed": True}

    assert await receive_payment(delivery) is True

    doc = await _doc_state(client, tok, eid)
    assert not doc.get("payments") and doc["status"] != "paid"
    assert await _pay_je_entities(session, _company_id(tok), eid) == []
    r = await client.get("/payments/unmatched", headers=_h(tok))
    assert [(p["reference"], p["amount"], p["currency"]) for p in r.json()["items"]] == [
        ("pi_ugx_frac", 100000.5, "UGX")]


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
    assert (await client.patch("/companies/me/books", json={"currency": "CLP"},
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
    defaulting to the default deposit account; changing the setting later does not move the payment."""
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
    code = "1111" if "cash" in kind else await _bank(client, tok)
    if "archived" in kind:
        await _chart_changed(session, tok, code, is_active=False)
    else:
        await _chart_changed(session, tok, code, account_type="liability" if code == "1111" else "revenue")
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
    code = "1111" if kind == "cash" else await _bank(client, tok)
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
    r = await client.patch("/companies/me/books", json={key: value},
                           headers=_h(tok))
    assert r.status_code == 422
    assert "the default deposit account (1111) or an active bank account" in r.json()["detail"]["message"]


@pytest.mark.parametrize("key", ["stripe_deposit_account", "woocommerce_deposit_account"])
@pytest.mark.parametrize("change", [{"is_active": False}, {"account_type": "revenue"}])
@pytest.mark.asyncio
async def test_the_online_deposit_setting_refuses_a_bank_whose_chart_account_cannot_hold_money(
        client, session, key, change):
    tok = await _register(client)
    code = await _bank(client, tok)
    await _chart_changed(session, tok, code, **change)
    r = await client.patch("/companies/me/books", json={key: code},
                           headers=_h(tok))
    assert r.status_code == 422, r.text
    expected_key = "posting.destination.inactive" if "is_active" in change else "posting.destination.not_money"
    assert r.json()["detail"]["message_key"] == expected_key


@pytest.mark.parametrize("key", ["stripe_deposit_account", "woocommerce_deposit_account"])
@pytest.mark.asyncio
async def test_the_online_deposit_setting_takes_cash_an_active_bank_or_the_default(client, key):
    tok = await _register(client)
    code = await _bank(client, tok)
    inactive = await _bank(client, tok, active=False)
    for value in ("1111", code, ""):
        r = await client.patch("/companies/me/books", json={key: value},
                               headers=_h(tok))
        assert r.status_code == 200, (value, r.text)
    r = await client.patch("/companies/me/books", json={key: inactive},
                           headers=_h(tok))
    assert r.status_code == 422


@pytest.mark.parametrize("key", ["stripe_deposit_account", "woocommerce_deposit_account"])
@pytest.mark.parametrize("value", [1110, 0, False, True, [], ["1110"], {}, {"code": "1110"}, 1110.0])
@pytest.mark.asyncio
async def test_the_online_deposit_setting_refuses_a_value_that_is_not_an_account_code(client, key, value):
    from ui.i18n import refusal_text

    tok = await _register(client)
    r = await client.patch("/companies/me/books", json={key: value}, headers=_h(tok))
    assert r.status_code == 422, (value, r.status_code, r.text)
    assert "account code" in refusal_text(r.json()["detail"])
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
        "amount": 500.0, "payment_date": "2026-07-13", "bank_account": "1111",
    }, headers=_h(tok))
    assert r.status_code == 200, r.text

    with pytest.raises(HTTPException) as refused:
        await record_stripe_payment(session, cid, eid, stale,
                                    reference="pi_race", amount_minor=107000, currency="usd",
                                    paid_at=PAID, context=BOOKS, managed=True)
    assert refused.value.status_code == 409
    assert refused.value.detail == t("documents.err_payment_too_much", "en", amount=1070.0, outstanding=570.0)
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
        "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1111",
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


_NOT_HTTPS = ["javascript:alert(1)", "http://stripe.test/cs_1", "//other.test/x", 7, ["https://stripe.test/cs_1"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _NOT_HTTPS)
async def test_a_checkout_address_that_is_not_https_is_never_followed(client, payments_on, monkeypatch, url):
    monkeypatch.setattr("celerp.services.payments.create_checkout", lambda **kw: _async({"url": url}))
    tok = await _register(client)
    _, token = await _invoice(client, tok)
    r = await client.get(f"/pay/{token}", follow_redirects=False)
    assert r.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _NOT_HTTPS)
async def test_a_connect_address_that_is_not_https_is_never_returned(client, monkeypatch, url):
    monkeypatch.setattr("celerp.services.payments.connect_start", lambda: _async({"url": url}))
    tok = await _register(client)
    r = await client.post("/payments/connect", headers=_h(tok))
    assert r.status_code == 502


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
    from ui.i18n import t
    assert r.json()["detail"] == t("error.stripe_disconnect_failed", "en")


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
        {"reference": "pi_new", "amount": 5000, "currency": "JPY", "company_id": "c-new", "company_name": None,
         "document_id": "doc:2", "document_ref": None, "received_on": "2026-09-29", "paid_on": None},
        {"reference": "pi_old", "amount": 1070.0, "currency": "USD", "company_id": "c-old", "company_name": None,
         "document_id": "doc:1", "document_ref": None, "received_on": "2026-09-28", "paid_on": "2026-09-25"},
    ], "refunds": []}


# 20:30 UTC on Oct 4 is 03:30 on Oct 5 in Bangkok (UTC+7).
_PAID_LATE = datetime.datetime(2026, 10, 4, 20, 30, tzinfo=datetime.timezone.utc)


@pytest.mark.asyncio
async def test_unmatched_dates_are_the_company_business_day(client, session, payments_on):
    """The unmatched tables date a payment and a refund on the company's calendar,
    the day the same payment is booked on, never the UTC day."""
    import re
    from fasthtml.common import to_xml
    from sqlalchemy import update
    from celerp.models.payment_closure import UnmatchedPayment, UnmatchedRefund
    from celerp.services.payments import receive_payment
    from ui.routes.settings_payments import _unmatched
    tok = await _register(client)
    assert (await client.patch("/companies/me", json={"settings": {"timezone": "Asia/Bangkok"}},
                               headers=_h(tok))).status_code == 200
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    bkk = dict(BOOKS, timezone="Asia/Bangkok")
    for entity, reference in ((eid, "pi_booked"), ("doc:gone", "pi_unmatched")):
        assert await receive_payment({"company_id": cid, "entity_id": entity, "reference": reference,
                                      "amount_minor": 100, "currency": "usd", "paid_at": _PAID_LATE.isoformat(),
                                      "context": bkk, "managed": True})
    # Arrival is stamped with the current time; pin it to the same late-evening instant.
    await session.execute(update(UnmatchedPayment).where(UnmatchedPayment.reference == "pi_unmatched")
                          .values(received_at=_PAID_LATE))
    session.add(UnmatchedRefund(refund_id="re_late", cycle=1, transition="applied", reference="pi_x",
                                amount_minor=100, currency="USD", former_company=cid, document="doc:gone",
                                received_at=_PAID_LATE, occurred_at=_PAID_LATE))
    await session.commit()
    booked_day = (await _doc_state(client, tok, eid))["payments"][0]["payment_date"]

    body = (await client.get("/payments/unmatched", headers=_h(tok))).json()

    assert booked_day == "2026-10-05"
    [payment] = body["items"]
    [refund] = body["refunds"]
    assert (payment["received_on"], payment["paid_on"]) == (booked_day, booked_day)
    assert (refund["received_on"], refund["refunded_on"]) == (booked_day, booked_day)
    cells = re.findall(r"<td[^>]*>([^<]*)</td>", to_xml(_unmatched(body)))
    assert cells.count(booked_day) == 4 and "2026-10-04" not in cells


# 02:00 UTC on Oct 5 is still Oct 4 in New York, and already Oct 5 in Bangkok.
_PAID_NY_EVENING = datetime.datetime(2026, 10, 5, 2, 0, tzinfo=datetime.timezone.utc)


@pytest.mark.asyncio
async def test_unmatched_rows_are_dated_in_their_own_company(client, session, payments_on):
    """The list shows every company's rows. Each is dated on its own company's
    calendar, the day it books on there, whichever company it is viewed from."""
    from celerp.models.payment_closure import UnmatchedRefund
    from celerp.services.payments import receive_payment
    tok_a = await _register(client)
    assert (await client.patch("/companies/me", json={"settings": {"timezone": "Asia/Bangkok"}},
                               headers=_h(tok_a))).status_code == 200
    r = await client.post("/companies", json={"name": "Second Co"}, headers=_h(tok_a))
    assert r.status_code == 200, r.text
    tok_b = r.json()["access_token"]
    assert (await client.patch("/companies/me", json={"settings": {"timezone": "America/New_York"}},
                               headers=_h(tok_b))).status_code == 200
    cid_b = _company_id(tok_b)
    assert await receive_payment({"company_id": cid_b, "entity_id": "doc:gone", "reference": "pi_ny",
                                  "amount_minor": 100, "currency": "usd",
                                  "paid_at": _PAID_NY_EVENING.isoformat(),
                                  "context": dict(BOOKS, timezone="America/New_York"), "managed": True})
    session.add(UnmatchedRefund(refund_id="re_ny", cycle=1, transition="applied", reference="pi_x",
                                amount_minor=100, currency="USD", former_company=cid_b, document="doc:gone",
                                received_at=_PAID_NY_EVENING, occurred_at=_PAID_NY_EVENING))
    await session.commit()
    eid, _ = await _payable_invoice(client, tok_b)
    viewed = {}
    for viewer, tok in (("own company", tok_b), ("other company", tok_a)):
        body = (await client.get("/payments/unmatched", headers=_h(tok))).json()
        [payment] = [p for p in body["items"] if p["reference"] == "pi_ny"]
        [refund] = [x for x in body["refunds"] if x["refund_id"] == "re_ny"]
        viewed[viewer] = (payment["paid_on"], refund["received_on"], refund["refunded_on"])

    rec = await client.post("/payments/unmatched/record", headers=_h(tok_b),
                            json={"reference": "pi_ny", "entity_id": eid})
    assert rec.status_code == 200, rec.text
    booked_day = (await _doc_state(client, tok_b, eid))["payments"][0]["payment_date"]

    assert booked_day == "2026-10-04"
    assert viewed == {"own company": (booked_day,) * 3, "other company": (booked_day,) * 3}


@pytest.mark.asyncio
async def test_unmatched_payments_name_their_company_and_invoice_while_they_exist(client, session):
    """The table shows names a person knows, not ids: the company's name and the
    invoice's reference while they exist here, and None once they are gone."""
    import datetime
    import uuid
    from celerp.models.payment_closure import UnmatchedPayment, UnmatchedRefund
    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    ref = (await _doc_state(client, tok, eid)).get("ref_id")
    name = (await client.get("/companies/me", headers=_h(tok))).json()["name"]
    gone = str(uuid.uuid4())
    at = datetime.datetime(2026, 9, 28, 9, 0, tzinfo=datetime.timezone.utc)
    session.add_all([
        UnmatchedPayment(reference="pi_here", amount_minor=107000, currency="USD", former_company=cid,
                         document=eid, received_at=at + datetime.timedelta(days=1)),
        UnmatchedPayment(reference="pi_gone", amount_minor=107000, currency="USD", former_company=gone,
                         document=eid, received_at=at),
        UnmatchedRefund(refund_id="re_here", cycle=1, transition="applied", reference="pi_here",
                        amount_minor=100, currency="USD", former_company=cid, document="doc:deleted",
                        received_at=at),
    ])
    await session.commit()

    body = (await client.get("/payments/unmatched", headers=_h(tok))).json()

    assert ref and name
    assert [(p["reference"], p["company_name"], p["document_ref"]) for p in body["items"]] == [
        ("pi_here", name, ref), ("pi_gone", None, None)]
    assert [(r["company_name"], r["document_ref"]) for r in body["refunds"]] == [(name, None)]


def test_the_unmatched_table_shows_names_and_marks_what_was_deleted():
    from fasthtml.common import to_xml
    from ui.routes.settings_payments import _unmatched
    row = {"received_on": "2026-10-04", "paid_on": None, "reference": "pi_1", "amount": 10.0,
           "currency": "USD", "company_id": "0b9c2e7a-1111-4c1e-9f00-aaaaaaaaaaaa", "company_name": "Acme Ltd",
           "document_id": "6f1d2c3b-2222-4d2e-8e11-bbbbbbbbbbbb", "document_ref": None}

    html = to_xml(_unmatched({"items": [row], "refunds": [{**row, "transition": "applied", "refunded_on": None}]}))

    assert row["company_id"] not in html and row["document_id"] not in html
    assert html.count("<td>Acme Ltd</td>") == 2
    # The payment's invoice cell is "--", click-to-edit to record it on an invoice; the
    # refund's stays marked deleted.
    assert html.count("(deleted)") == 1
    assert 'hx-get="/settings/payments/unmatched/pi_1/invoice/edit"' in html


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
        "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1111", "method": method,
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
        "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1111", "method": "stripe",
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


@pytest.mark.asyncio
async def test_actual_online_fees_post_once_and_refund_as_separate_cash_movements(
        client, session, payments_on):
    import hashlib
    import uuid
    from decimal import Decimal
    from celerp.models.projections import Projection
    from celerp.services.payments import receive_payment, receive_fees

    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    payment = {"company_id": cid, "entity_id": eid, "reference": "pi_fee_1",
               "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
               "context": dict(BOOKS), "managed": True}
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_fee_1",
            "total_minor": 520, "processor_minor": 270,
            "platform_minor": 250, "other_minor": 0}
    event = {"company_id": cid, "entity_id": eid, "reference": "pi_fee_1",
             "fees": fees, "occurred_at": PAID.isoformat()}
    assert await receive_fees(event) is False
    assert await receive_payment(payment)
    assert await receive_fees(event)
    assert await receive_fees(event)
    token = hashlib.sha256(b"txn_fee_1").hexdigest()[:24]
    journal = await session.get(
        Projection, (uuid.UUID(cid), f"je:auto:{eid}:online-fees:{token}"),
        populate_existing=True,
    )
    assert journal is not None and journal.state["status"] == "posted"
    lines = journal.state["entries"]
    debit = sum(Decimal(str(v.get("debit") or 0)) for v in lines)
    credit = sum(Decimal(str(v.get("credit") or 0)) for v in lines)
    assert debit == credit == Decimal("5.2")
    assert sum(Decimal(str(v.get("debit") or 0)) for v in lines
               if v["account"] != "1111") == Decimal("5.2")
    assert sum(Decimal(str(v.get("credit") or 0)) for v in lines
               if v["account"] == "1111") == Decimal("5.2")
    assert (await _doc_state(client, tok, eid))["amount_outstanding"] == 0

    reversed_fees = {**fees, "transaction_id": "txn_fee_refund",
                     "total_minor": -100, "processor_minor": 0,
                     "platform_minor": -100}
    assert await receive_fees({**event, "fees": reversed_fees})
    refund_token = hashlib.sha256(b"txn_fee_refund").hexdigest()[:24]
    refund_je = await session.get(
        Projection, (uuid.UUID(cid), f"je:auto:{eid}:online-fees:{refund_token}"),
        populate_existing=True,
    )
    assert refund_je is not None and refund_je.state["status"] == "posted"
    assert sum(Decimal(str(v.get("debit") or 0)) for v in refund_je.state["entries"]
               if v["account"] == "1111") == Decimal("1")
    with pytest.raises(ValueError, match="other details"):
        await receive_fees({**event, "fees": {**fees, "total_minor": 521, "other_minor": 1}})


@pytest.mark.asyncio
async def test_online_fee_contract_rejects_inconsistent_fees(
        client, session, payments_on):
    from celerp.services.payments import receive_payment, receive_fees
    from celerp.models.projections import Projection

    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    assert await receive_payment({
        "company_id": cid, "entity_id": eid, "reference": "pi_fee_guard",
        "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
        "context": dict(BOOKS), "managed": True})
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_guard",
            "total_minor": 10, "processor_minor": 10, "platform_minor": 0,
            "other_minor": 0}
    event = {"company_id": cid, "entity_id": eid, "reference": "pi_fee_guard",
             "fees": fees, "occurred_at": PAID.isoformat()}
    for changes in (
        {"fees": {**fees, "total_minor": 11}},
        {"fees": {**fees, "platform_minor": True}},
        {"fees": {**fees, "total_minor": 0.1}},
        {"reference": "pi_not_a_payment"},
        {"occurred_at": "2026-10-12"},
    ):
        assert await receive_fees({**event, **changes}) is False
    # A currency the payment's books cannot take is acknowledged with a notice, never posted.
    before = await _ledger_count(session, cid)
    assert await receive_fees({**event, "fees": {**fees, "currency": "eur"}}) is True
    assert await _ledger_count(session, cid) == before
    assert await receive_fees(event) is True



@pytest.mark.asyncio
async def test_delayed_actual_fees_post_in_open_period_without_backdating(client, session, payments_on):
    import hashlib
    import uuid
    from datetime import date
    from celerp.services.company_lock import locked_company
    from celerp.models.projections import Projection
    from celerp.services.payments import receive_payment, receive_fees

    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    assert await receive_payment({
        "company_id": cid, "entity_id": eid, "reference": "pi_fee_late",
        "amount_minor": 107000, "currency": "usd",
        "paid_at": PAID.isoformat(), "context": dict(BOOKS), "managed": True})
    company = await locked_company(session, uuid.UUID(cid))
    company.settings = {**company.settings, "lock_date": "2026-09-30"}
    await session.commit()
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_fee_late",
            "total_minor": 100, "processor_minor": 100,
            "platform_minor": 0, "other_minor": 0}
    assert await receive_fees({
        "company_id": cid, "entity_id": eid, "reference": "pi_fee_late",
        "fees": fees, "occurred_at": PAID.isoformat()})
    token = hashlib.sha256(b"txn_fee_late").hexdigest()[:24]
    row = await session.get(
        Projection, (uuid.UUID(cid), f"je:auto:{eid}:online-fees:{token}"),
        populate_existing=True)
    assert row is not None and row.state["status"] == "posted"
    assert date.fromisoformat(str(row.state["ts"])[:10]) > date(2026, 9, 30)
    assert await receive_fees({
        "company_id": cid, "entity_id": eid, "reference": "pi_fee_late",
        "fees": fees, "occurred_at": PAID.isoformat()})



@pytest.mark.asyncio
async def test_provider_base_currency_fees_are_not_converted_twice(client, session, payments_on):
    import uuid
    from decimal import Decimal
    from celerp.models.projections import Projection
    from celerp.services.payments import receive_payment, receive_fees

    tok = await _register(client)
    created = await client.post("/docs", json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100}],
        "total": 100, "currency": "EUR", "conversion_rate": 1.2,
    }, headers=_h(tok))
    assert created.status_code == 200, created.text
    eid = created.json()["id"]
    issued = await client.post(f"/docs/{eid}/finalize", headers=_h(tok))
    assert issued.status_code == 200, issued.text
    cid = _company_id(tok)
    assert await receive_payment({
        "company_id": cid, "entity_id": eid, "reference": "pi_fx_fee",
        "amount_minor": 10000, "currency": "eur", "paid_at": PAID.isoformat(),
        "context": {**BOOKS, "rate": "1.2"}, "managed": True,
    })
    fees = {"version": 1, "transaction_id": "txn_fee_usd_base", "currency": "usd",
            "total_minor": 320, "processor_minor": 120, "platform_minor": 200,
            "other_minor": 0}
    report = {"company_id": cid, "entity_id": eid, "reference": "pi_fx_fee",
              "fees": fees, "occurred_at": PAID.isoformat()}
    assert await receive_fees(report)
    assert await receive_fees(report)
    from hashlib import sha256
    token = sha256(b"txn_fee_usd_base").hexdigest()[:24]
    journal = await session.get(
        Projection, (uuid.UUID(cid), f"je:auto:{eid}:online-fees:{token}"),
        populate_existing=True)
    assert journal and journal.state["status"] == "posted"
    entries = journal.state["entries"]
    debit = sum(Decimal(str(e.get("debit") or 0)) for e in entries)
    credit = sum(Decimal(str(e.get("credit") or 0)) for e in entries)
    assert debit == credit == Decimal("3.2")
    # A third currency is never converted: acknowledged with a notice, nothing posted.
    before = await _ledger_count(session, cid)
    assert await receive_fees({**report, "fees": {**fees, "currency": "gbp", "transaction_id": "txn_gbp"}}) is True
    assert await _ledger_count(session, cid) == before


async def _fee_notices(session, cid: str, key: str) -> list:
    import uuid
    from sqlalchemy import select
    from celerp.models.notification import Notification
    rows = (await session.execute(select(Notification).where(
        Notification.company_id == uuid.UUID(cid)))).scalars().all()
    return [n for n in rows if (n.i18n or {}).get("title") == f"{key}.title"]


async def _paid_with_fees(client, tok, reference: str, fees: dict):
    from celerp.services.payments import receive_payment
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    assert await receive_payment({"company_id": cid, "entity_id": eid, "reference": reference,
                                  "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": dict(BOOKS), "managed": True})
    return eid, {"company_id": cid, "entity_id": eid, "reference": reference,
                 "fees": fees, "occurred_at": PAID.isoformat()}


@pytest.mark.asyncio
async def test_online_fees_post_to_the_chosen_account(client, session, payments_on):
    """The fee lands on the account the company chose for online payment fees, which a
    new company starts with as its own seeded account, never on General expenses."""
    import hashlib
    import uuid
    from celerp.models.projections import Projection
    from celerp.services.payments import receive_fees

    tok = await _register(client)
    roles = {r["role"]: r for r in (await client.get(
        "/accounting/posting-accounts", headers=_h(tok))).json()["roles"]}
    assert roles["online_payment_fees"]["code"] == "6940"
    r = await client.post("/accounting/accounts", headers=_h(tok), json={
        "code": "6945", "name": "Card fees", "account_type": "expense", "parent_code": "6000"})
    assert r.status_code == 200, r.text
    r = await client.put("/accounting/posting-accounts/online_payment_fees", headers=_h(tok),
                         json={"code": "6945"})
    assert r.status_code == 200, r.text

    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_chosen",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    eid, event = await _paid_with_fees(client, tok, "pi_chosen", fees)
    assert await receive_fees(event)
    token = hashlib.sha256(b"txn_chosen").hexdigest()[:24]
    journal = await session.get(Projection, (uuid.UUID(_company_id(tok)), f"je:auto:{eid}:online-fees:{token}"),
                                populate_existing=True)
    fee_lines = [e for e in journal.state["entries"] if e["account"] != "1111"]
    assert {e["account"] for e in fee_lines} == {"6945"}
    assert all(e["account_roles"] == ["online_payment_fees"] for e in fee_lines)


@pytest.mark.asyncio
async def test_fee_notice_and_failure_notice(client, session, payments_on):
    """Each fee booked on the company's behalf tells it so, once, with a link to the
    invoice; a fee that cannot be recorded tells it why instead of only being logged.
    One whose cause can be fixed is recorded when delivered again after the fix; one
    whose cause is the payment's own recorded books is acknowledged once told, since
    no fix can reach them."""
    from celerp.services.payments import receive_fees

    tok = await _register(client)
    cid = _company_id(tok)
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_notice",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    eid, event = await _paid_with_fees(client, tok, "pi_notice", fees)
    assert await receive_fees(event)
    assert await receive_fees(event)
    recorded = await _fee_notices(session, cid, "notice.online_fee_recorded")
    assert len(recorded) == 1
    assert recorded[0].action_url == f"/docs/{eid}"
    assert "5.20 USD" in recorded[0].body and "6940" in recorded[0].body

    refund = {**fees, "transaction_id": "txn_notice_back", "total_minor": -100,
              "processor_minor": 0, "platform_minor": -100}
    assert await receive_fees({**event, "fees": refund})
    returned = await _fee_notices(session, cid, "notice.online_fee_returned")
    assert len(returned) == 1 and "1.00 USD" in returned[0].body

    # A fee in a currency the payment's books cannot convert is never guessed at; the
    # books were fixed when the payment was recorded, so it is acknowledged once told.
    odd = {**fees, "transaction_id": "txn_notice_gbp", "currency": "gbp"}
    assert await receive_fees({**event, "fees": odd}) is True
    assert await receive_fees({**event, "fees": odd}) is True
    failed = await _fee_notices(session, cid, "notice.online_fee_failed")
    assert len(failed) == 1
    assert failed[0].priority == "high" and failed[0].action_url == f"/docs/{eid}"
    assert failed[0].i18n["params"]["reason"]["message_key"] == "notice.online_fee_failed.currency"

    # The company's fee account cannot take postings: the fee is refused with a notice,
    # and recorded once the account is fixed and the fee delivered again.
    r = await client.post("/accounting/accounts", headers=_h(tok), json={
        "code": "6946", "name": "Old fees", "account_type": "expense", "parent_code": "6000"})
    assert r.status_code == 200, r.text
    assert (await client.put("/accounting/posting-accounts/online_payment_fees", headers=_h(tok),
                             json={"code": "6946"})).status_code == 200
    from sqlalchemy import update
    from celerp_accounting.models import Account
    import uuid
    await session.execute(update(Account).where(Account.company_id == uuid.UUID(cid), Account.code == "6946")
                          .values(is_active=False))
    await session.commit()
    later = {**fees, "transaction_id": "txn_notice_later"}
    assert await receive_fees({**event, "fees": later}) is False
    failed = await _fee_notices(session, cid, "notice.online_fee_failed")
    assert any(n.i18n["params"]["reason"]["message_key"] == "notice.online_fee_failed.account" for n in failed)
    assert await _fee_notices(session, cid, "notice.posting_unmapped")
    assert (await client.put("/accounting/posting-accounts/online_payment_fees", headers=_h(tok),
                             json={"code": "6940"})).status_code == 200
    assert await receive_fees({**event, "fees": later}) is True


async def _ledger_count(session, cid: str) -> int:
    import uuid
    from sqlalchemy import func, select
    from celerp.models.ledger import LedgerEntry
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == uuid.UUID(cid)))


@pytest.mark.asyncio
async def test_fee_for_a_deleted_document_is_acknowledged_with_one_notice(client, session, payments_on):
    """A fee delivered for a document that no longer exists can never be recorded, so it
    is acknowledged (the relay stops redelivering) and the company is told once, with the
    amount and the document, to record it by hand. Nothing is posted."""
    import uuid
    from sqlalchemy import delete
    from celerp.models.projections import Projection
    from celerp.services.payments import receive_fees

    tok = await _register(client)
    cid = _company_id(tok)
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_gone_doc",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    eid, event = await _paid_with_fees(client, tok, "pi_gone_doc", fees)
    await session.execute(delete(Projection).where(
        Projection.company_id == uuid.UUID(cid), Projection.entity_id == eid))
    await session.commit()
    before = await _ledger_count(session, cid)

    assert await receive_fees({**event, "delivery_id": "d1"}) is True
    assert await receive_fees({**event, "delivery_id": "d1"}) is True
    failed = await _fee_notices(session, cid, "notice.online_fee_failed")
    assert len(failed) == 1
    assert failed[0].priority == "high" and failed[0].action_url is None
    assert failed[0].i18n["params"]["reason"]["message_key"] == "notice.online_fee_failed.deleted"
    assert "5.20 USD" in failed[0].body and eid in failed[0].body
    assert await _ledger_count(session, cid) == before


@pytest.mark.asyncio
async def test_fee_for_a_deleted_payment_is_acknowledged_with_one_notice(client, session, payments_on):
    """A fee for a payment entry that was deleted from its invoice is acknowledged with
    one notice and nothing posted; a fee for a payment not yet delivered stays pending."""
    import uuid
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from celerp.models.projections import Projection
    from celerp.services.payments import receive_fees

    tok = await _register(client)
    cid = _company_id(tok)
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_gone_pay",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    eid, event = await _paid_with_fees(client, tok, "pi_gone_pay", fees)
    assert await receive_fees({**event, "reference": "pi_not_yet"}) is False
    row = await session.scalar(select(Projection).where(
        Projection.company_id == uuid.UUID(cid), Projection.entity_id == eid))
    for p in row.state["payments"]:
        if p.get("reference") == "pi_gone_pay":
            p["status"] = "deleted"
    flag_modified(row, "state")
    await session.commit()
    before = await _ledger_count(session, cid)

    assert await receive_fees(event) is True
    assert await receive_fees(event) is True
    failed = await _fee_notices(session, cid, "notice.online_fee_failed")
    assert len(failed) == 1
    assert failed[0].action_url == f"/docs/{eid}"
    assert failed[0].i18n["params"]["reason"]["message_key"] == "notice.online_fee_failed.deleted"
    assert "5.20 USD" in failed[0].body
    assert await _ledger_count(session, cid) == before


@pytest.mark.asyncio
async def test_fee_for_a_payment_kept_among_the_unmatched_is_acknowledged_with_one_notice(
        client, session, payments_on):
    """An invoice already paid by hand parks the online payment among the unmatched
    payments; its fee can never be recorded on the invoice, so it is acknowledged and the
    company is told once, with the amount and the payment reference, to record it by hand.
    Nothing is posted."""
    from celerp.services.payments import receive_payment, receive_fees

    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    r = await client.post(f"/docs/{eid}/payment", headers=_h(tok), json={
        "amount": 1070.0, "payment_date": "2026-07-13", "bank_account": "1111", "method": "bank_transfer",
        "reference": "wire1"})
    assert r.status_code == 200, r.text
    assert await receive_payment({"company_id": cid, "entity_id": eid, "reference": "pi_parked",
                                  "amount_minor": 107000, "currency": "usd", "paid_at": PAID.isoformat(),
                                  "context": dict(BOOKS), "managed": True}) is True
    items = (await client.get("/payments/unmatched", headers=_h(tok))).json()["items"]
    assert [p["reference"] for p in items] == ["pi_parked"]
    before = await _ledger_count(session, cid)
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_parked",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    event = {"company_id": cid, "entity_id": eid, "reference": "pi_parked", "fees": fees,
             "occurred_at": PAID.isoformat()}

    assert [await receive_fees(event) for _ in range(3)] == [True, True, True]
    failed = await _fee_notices(session, cid, "notice.online_fee_failed")
    assert len(failed) == 1
    reason = failed[0].i18n["params"]["reason"]
    assert reason["message_key"] == "notice.online_fee_failed.unmatched"
    assert reason["params"] == {"reference": "pi_parked"}
    assert "5.20 USD" in failed[0].body and "pi_parked" in failed[0].body
    assert await _ledger_count(session, cid) == before


@pytest.mark.asyncio
async def test_fee_for_a_payment_recorded_without_books_is_acknowledged_with_one_notice(
        client, session, payments_on):
    """A payment recorded without the bank account or rate its fees need keeps them so;
    no fix can reach them, so the fee is acknowledged and the company told once."""
    import uuid
    from sqlalchemy import select
    from celerp.models.projections import Projection
    from celerp.services.payments import receive_fees

    tok = await _register(client)
    cid = _company_id(tok)
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_no_books",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    eid, event = await _paid_with_fees(client, tok, "pi_no_books", fees)
    row = await session.scalar(select(Projection).where(
        Projection.company_id == uuid.UUID(cid), Projection.entity_id == eid))
    row.state = {**row.state, "payments": [{**p, "bank_account": None} for p in row.state["payments"]]}
    await session.commit()
    before = await _ledger_count(session, cid)

    assert [await receive_fees(event) for _ in range(2)] == [True, True]
    failed = await _fee_notices(session, cid, "notice.online_fee_failed")
    assert len(failed) == 1
    assert failed[0].i18n["params"]["reason"]["message_key"] == "notice.online_fee_failed.books"
    assert await _ledger_count(session, cid) == before


@pytest.mark.asyncio
async def test_fee_before_its_payment_stays_pending_without_a_notice(client, session, payments_on):
    """A fee for a document with no payment for the reference and nothing unmatched under
    it can still be recorded once the payment arrives, so it is not acknowledged."""
    from celerp.services.payments import receive_fees

    tok = await _register(client)
    eid, _ = await _payable_invoice(client, tok)
    cid = _company_id(tok)
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_early",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    event = {"company_id": cid, "entity_id": eid, "reference": "pi_early", "fees": fees,
             "occurred_at": PAID.isoformat()}
    assert await receive_fees(event) is False
    assert await _fee_notices(session, cid, "notice.online_fee_failed") == []


@pytest.mark.asyncio
async def test_fee_for_a_company_that_no_longer_exists_is_acknowledged_silently(client, session, payments_on):
    """A company row that is gone never comes back, so its fee is acknowledged (the relay
    stops redelivering) with nothing written and nobody to tell."""
    import uuid
    from sqlalchemy import func, select
    from celerp.models.ledger import LedgerEntry
    from celerp.models.notification import Notification
    from celerp.services.payments import receive_fees

    gone = str(uuid.uuid4())
    fees = {"version": 1, "currency": "usd", "transaction_id": "txn_no_company",
            "total_minor": 520, "processor_minor": 270, "platform_minor": 250, "other_minor": 0}
    event = {"company_id": gone, "entity_id": "doc:x", "reference": "pi_x",
             "fees": fees, "occurred_at": PAID.isoformat()}
    assert await receive_fees(event) is True
    assert await receive_fees(event) is True
    assert await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == uuid.UUID(gone))) == 0
    assert await session.scalar(select(func.count()).select_from(Notification).where(
        Notification.company_id == uuid.UUID(gone))) == 0

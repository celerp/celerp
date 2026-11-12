# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A members' club from its first renewal to the donor statement, through the API the pages use.

Initial conditions: a fresh company with the seeded chart and the Nonprofit preset applied (its
Donation category counts as gifts), funds turned on with a restricted fund (Library gift), two
board-designated funds and the restricted net assets account (test_fund_movements._setup). The
business day is frozen per step. Opening balances: 1000.00 in the bank against opening equity,
400.00 of it assigned to the restricted fund.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from celerp.services.journal_accounts import fund_account_balances
from test_credit_note_settlement_owned import _post
from test_fund_movements import _setup
from test_income_accounts import _issued, _item, _line
from test_revenue_recognition import _freeze

pytestmark = pytest.mark.asyncio

BANK = "1111"
DATE = "2026-03-02"


async def _held(session, auth, fund, account=BANK) -> Decimal:
    return (await fund_account_balances(session, auth["company_id"])).get(fund, {}).get(account, Decimal(0))


async def _club(client, auth) -> dict:
    """The preset, funds on, the opening balances by fund, and the items the club sells."""
    h = auth["headers"]
    r = await client.post("/companies/me/apply-preset", headers=h, params={"vertical": "nonprofit"})
    assert r.status_code == 200, r.text
    f = await _setup(client, auth)
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-01-01", "memo": "Opening balances", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": BANK, "debit": 1000.0, "credit": 0.0},
                    {"account": "3200", "debit": 0.0, "credit": 1000.0}]})
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/funds/opening", headers=h, json={
        "request_key": uuid.uuid4().hex, "date": "2026-01-01", "reason": "Opening balances by fund",
        "assignments": [{"account": BANK, "fund": f["R"], "amount": "400"}]})
    assert r.status_code == 200, r.text
    f["gift"] = await _item(client, auth, "Library gift", category="donation")
    f["ticket"] = await _item(client, auth, "Gala ticket", category="event_ticket")
    f["dues"] = await _item(client, auth, "Annual membership", category="membership")
    return f


async def _pay(client, auth, doc: str, amount: float, day: str = DATE):
    r = await client.post(f"/docs/{doc}/payment", headers=auth["headers"], json={
        "amount": amount, "payment_date": day, "bank_account": BANK, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


async def _member(client, auth, name: str, email: str) -> str:
    r = await client.post("/crm/contacts", headers=auth["headers"], json={"name": name, "email": email})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_e2e1_renewal_receipts_mixed_invoice_and_refund(client, session, auth, monkeypatch):
    """Stage 1: opening balances sit on their funds; a member's annual renewal falls due and
    Generate due bills it; a partial bank receipt leaves the rest open and a second receipt
    settles it; a mixed invoice of a restricted gift and a gala ticket posts each line to its own
    fund; a second gift refunded in full gives its money back from the restricted fund, and the
    donor's history counts the kept gift only."""
    h = auth["headers"]
    _freeze(monkeypatch, "2026-01-02")
    f = await _club(client, auth)
    assert await _held(session, auth, f["R"]) == 400 and await _held(session, auth, None) == 600

    member = await _member(client, auth, "Ada Member", "ada@club.test")
    r = await client.post("/docs", headers=h, json={
        "doc_type": "subscription_invoice", "kind": "membership", "contact_id": member,
        "frequency": "annually", "billing_timing": "advance", "start_date": "2026-03-01",
        "price_list": "Retail", "line_items": [_line(f["dues"], "Annual membership", 120.0)]})
    assert r.status_code in (200, 201), r.text
    agreement = r.json()["id"]
    assert (await _post(client, auth, f"/subscriptions/{agreement}/activate")).status_code == 200

    _freeze(monkeypatch, DATE)
    r = await _post(client, auth, f"/subscriptions/{agreement}/generate-due", {"request_key": str(uuid.uuid4())})
    assert r.status_code == 200, r.text
    renewal = r.json()["doc_id"]
    _freeze(monkeypatch, "2026-03-03")
    await _pay(client, auth, renewal, 50.0, "2026-03-03")
    doc = (await client.get(f"/docs/{renewal}", headers=h)).json()
    assert (doc["status"], float(doc["amount_outstanding"])) == ("partial", 70.0), doc
    await _pay(client, auth, renewal, 70.0, "2026-03-03")
    doc = (await client.get(f"/docs/{renewal}", headers=h)).json()
    assert (doc["status"], float(doc["amount_outstanding"])) == ("paid", 0.0), doc
    assert await _held(session, auth, None) == 720

    donor = await _member(client, auth, "Bea Donor", "bea@club.test")
    mixed = await _issued(client, auth, "invoice", [
        _line(f["gift"], "Library gift", 300.0, fund_id=f["R"]), _line(f["ticket"], "Gala ticket", 80.0)],
        contact_id=donor, issue_date="2026-03-03", price_list="Retail", subtotal=380.0, total=380.0)
    await _pay(client, auth, mixed, 380.0, "2026-03-03")
    assert await _held(session, auth, f["R"]) == 700 and await _held(session, auth, None) == 800

    gift = await _issued(client, auth, "invoice", [_line(f["gift"], "Library gift", 50.0, fund_id=f["R"])],
                         contact_id=donor, issue_date="2026-03-03", price_list="Retail", subtotal=50.0, total=50.0)
    await _pay(client, auth, gift, 50.0, "2026-03-03")
    assert await _held(session, auth, f["R"]) == 750
    r = await client.post(f"/docs/{gift}/refund", headers=h, json={
        "payment_index": 0, "amount": 50.0, "payment_date": "2026-03-03", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"]) == 700 and await _held(session, auth, None) == 800
    r = await client.get(f"/donors/{donor}/history", headers=h, params={"from": "2026-03-01", "to": "2026-03-31"})
    assert r.status_code == 200, r.text
    assert float(r.json()["total"]) == 300.0

"""Review probe, CLASS: status-code-overloaded (the journal form reads every 409 as an
idempotency replay, so the new reconciled-history refusal reaches the user as "already posted
with different amounts"). Expected: the form shows the refusal naming the account and the
statement date."""
from __future__ import annotations

import html
import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def test_probe_backdated_journal_form_shows_the_reconciliation_refusal(owner_ui):
    api = owner_ui.api
    r = await api.post("/accounting/bank-accounts", json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
        "currency": "USD", "opening_balance": 1000.0})
    assert r.status_code == 200, r.text
    bank = r.json()
    r = await api.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank["id"], "statement_date": "2026-09-30", "statement_balance": 1000.0})
    sid = r.json()["id"]
    assert (await api.post(f"/accounting/reconciliation/{sid}/complete")).status_code == 200
    page = await owner_ui.post("/accounting/journal/new", data={
        "ts": "2026-09-15", "memo": "Backdated", "idempotency_token": uuid.uuid4().hex,
        "account_0": bank["chart_account_code"], "debit_0": "10", "credit_0": "",
        "account_1": "4100", "debit_1": "", "credit_1": "10"})
    body = html.unescape(page.text)
    assert "already posted with different amounts" not in body, "the refusal reads as an idempotency replay"
    assert "Harbor Bank" in body and "2026-09-30" in body

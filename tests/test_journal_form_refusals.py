# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The journal form tells two refusals apart by their code, never by the status they
share: a posting inside a completed reconciliation shows that refusal, naming the
account and the statement date, and only a replayed token with different amounts reads
as already posted (with a fresh token, so a deliberate resubmit goes through)."""
from __future__ import annotations

import html
import re
import uuid

import pytest

pytestmark = pytest.mark.asyncio


def _form(ts: str, code: str, amount: str, token: str) -> dict:
    return {"ts": ts, "memo": "Manual", "idempotency_token": token,
            "account_0": code, "debit_0": amount, "credit_0": "",
            "account_1": "4100", "debit_1": "", "credit_1": amount}


async def _bank(api) -> dict:
    r = await api.post("/accounting/bank-accounts", json={
        "bank_name": "Harbor Bank", "account_number": "0001", "bank_type": "checking",
        "currency": "USD", "opening_balance": 1000.0})
    assert r.status_code == 200, r.text
    return r.json()


async def test_backdated_journal_form_shows_the_reconciliation_refusal(owner_ui):
    api = owner_ui.api
    bank = await _bank(api)
    r = await api.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank["id"], "statement_date": "2026-09-30", "statement_balance": 1000.0})
    sid = r.json()["id"]
    assert (await api.post(f"/accounting/reconciliation/{sid}/complete")).status_code == 200
    page = await owner_ui.post("/accounting/journal/new",
                               data=_form("2026-09-15", bank["chart_account_code"], "10", uuid.uuid4().hex))
    body = html.unescape(page.text)
    assert "already posted with different amounts" not in body
    assert "Harbor Bank" in body and "2026-09-30" in body


async def test_replayed_token_with_different_amounts_reads_as_already_posted(owner_ui):
    bank = await _bank(owner_ui.api)
    token = uuid.uuid4().hex
    first = await owner_ui.post("/accounting/journal/new",
                                data=_form("2026-10-05", bank["chart_account_code"], "10", token))
    assert first.status_code in (200, 303), first.text
    page = await owner_ui.post("/accounting/journal/new",
                               data=_form("2026-10-05", bank["chart_account_code"], "20", token))
    body = html.unescape(page.text)
    assert "already posted with different amounts" in body
    fresh = re.search(r'name="idempotency_token"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="idempotency_token"', page.text)
    assert fresh and token not in fresh.groups()

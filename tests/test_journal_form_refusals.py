# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The journal form tells two refusals apart by their code, never by the status they
share: a posting inside a completed reconciliation shows that refusal, naming the
account and the statement date, and only a replayed token with different amounts reads
as already made with different details (with a fresh token, so a deliberate resubmit goes
through)."""
from __future__ import annotations

import html
import re
import uuid

import pytest

from ui.i18n import t

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
    assert t("request.key_reused") not in body
    assert "Harbor Bank" in body and "30 Sep 2026" in body


async def test_replayed_token_with_different_amounts_reads_as_already_made(owner_ui):
    bank = await _bank(owner_ui.api)
    token = uuid.uuid4().hex
    first = await owner_ui.post("/accounting/journal/new",
                                data=_form("2026-10-05", bank["chart_account_code"], "10", token))
    assert first.status_code in (200, 303), first.text
    page = await owner_ui.post("/accounting/journal/new",
                               data=_form("2026-10-05", bank["chart_account_code"], "20", token))
    body = html.unescape(page.text)
    assert t("request.key_reused") in body
    fresh = re.search(r'name="idempotency_token"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="idempotency_token"', page.text)
    assert fresh and token not in fresh.groups()


async def test_with_funds_on_each_journal_line_names_its_fund(owner_ui):
    """With funds in use the journal form has a Fund column, each line Unrestricted until a fund
    is picked; a picked fund goes with the line, Unrestricted sends none."""
    from unittest.mock import AsyncMock, patch

    import ui.api_client as ui_api

    api = owner_ui.api
    assert (await api.put("/accounting/funds/setting", json={"enabled": True})).status_code == 200
    r = await api.post("/accounting/funds", json={"request_key": uuid.uuid4().hex, "name": "Board reserve",
                                                  "restricted": False, "purpose": None})
    assert r.status_code == 200, r.text
    fund = r.json()["id"]
    page = (await owner_ui.get("/accounting/journal/new")).text
    assert re.search(r"<th[^>]*>Fund</th>", page), "Fund header missing"
    picks = re.findall(r'<input[^>]*name="fund_(\d+|__IDX__)"[^>]*value="([^"]*)"', page)
    assert sorted(picks) == [("0", "unrestricted"), ("1", "unrestricted"), ("__IDX__", "unrestricted")], picks
    assert "Board reserve" in page

    bank = await _bank(api)
    for picked, sent in ((fund, fund), ("unrestricted", None)):
        create = AsyncMock(side_effect=ui_api.create_journal_entry)
        with patch("ui.api_client.create_journal_entry", new=create):
            posted = await owner_ui.post("/accounting/journal/new", data={
                **_form("2026-10-05", bank["chart_account_code"], "10", uuid.uuid4().hex),
                "fund_0": picked, "fund_1": picked})
        assert posted.status_code == 303, posted.text
        assert [e.get("fund_id") for e in create.await_args.args[1]["entries"]] == [sent, sent]


async def test_without_funds_the_journal_form_has_no_fund_column(owner_ui):
    page = (await owner_ui.get("/accounting/journal/new")).text
    assert 'name="fund_0"' not in page and not re.search(r"<th[^>]*>Fund</th>", page)

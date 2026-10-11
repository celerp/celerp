# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Accounting messages say what went wrong and what to do, in the user's language."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from ui.i18n import t

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"
_LANGS = ("en", "th", "de", "fr", "es", "it", "pt", "id", "vi", "ja", "ar", "am")


async def _headers(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "AcctMsgCo", "email": f"acct-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _je(entries, ts="2026-01-15"):
    return {"ts": ts, "memo": "", "entries": entries, "idempotency_token": uuid.uuid4().hex}


@pytest.mark.asyncio
async def test_one_line_entry_asks_for_a_second_line(client):
    r = await client.post("/accounting/journal-entries", headers=await _headers(client),
                          json=_je([{"account": "1110", "debit": 10, "credit": 0}]))
    assert r.status_code == 422
    assert r.json()["detail"] == t("acct.err_je_two_lines", "en")


@pytest.mark.asyncio
async def test_bad_entry_date_names_the_field_and_the_format(client):
    r = await client.post("/accounting/journal-entries", headers=await _headers(client),
                          json=_je([{"account": "1110", "debit": 10, "credit": 0},
                                    {"account": "4100", "debit": 0, "credit": 10}], ts="15/01/2026"))
    assert r.status_code == 422
    assert r.json()["detail"] == t("acct.err_date_invalid", "en",
                                   field=t("acct.field_entry_date", "en"))


@pytest.mark.asyncio
async def test_backwards_report_range_says_which_date_to_move(client):
    r = await client.get("/accounting/trial-balance", headers=await _headers(client),
                         params={"date_from": "2026-02-01", "date_to": "2026-01-01"})
    assert r.status_code == 422
    assert r.json()["detail"] == t("acct.err_date_range", "en",
                                   start="2026-02-01", end="2026-01-01")


@pytest.mark.asyncio
async def test_missing_bank_account_says_refresh(client):
    r = await client.get(f"/accounting/bank-accounts/{uuid.uuid4()}", headers=await _headers(client))
    assert r.status_code == 404
    assert r.json()["detail"] == t("acct.err_bank_account_not_found", "en")


@pytest.mark.asyncio
async def test_missing_journal_entry_void_says_refresh(client):
    r = await client.post("/accounting/journal-entries/je:nope/void", headers=await _headers(client))
    assert r.status_code == 404
    assert r.json()["detail"] == t("acct.err_je_not_found", "en")


@pytest.mark.asyncio
async def test_empty_bulk_void_says_tick_entries(client):
    r = await client.post("/accounting/journal-entries/bulk-void", headers=await _headers(client),
                          json={"je_ids": []})
    assert r.status_code == 422
    assert r.json()["detail"] == t("acct.err_void_none_selected", "en")


@pytest.mark.asyncio
async def test_missing_reconciliation_says_open_it_again(client):
    r = await client.get(f"/accounting/reconciliation/{uuid.uuid4()}", headers=await _headers(client))
    assert r.status_code == 404
    assert r.json()["detail"] == t("acct.err_recon_not_found", "en")


@pytest.mark.asyncio
async def test_unknown_account_in_entry_names_the_code(client):
    r = await client.post("/accounting/journal-entries", headers=await _headers(client),
                          json=_je([{"account": "9999", "debit": 10, "credit": 0},
                                    {"account": "4100", "debit": 0, "credit": 10}]))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert (detail["message_key"], detail["params"]) == ("posting.destination.not_in_chart", {"code": "9999"})


@pytest.mark.parametrize("key,needle", [
    ("acct.err_je_two_lines", "Add another line"),
    ("acct.err_je_unbalanced_debit", "debit side"),
    ("acct.err_je_unbalanced_credit", "credit side"),
    ("acct.err_je_not_manual", "Open that document"),
    ("acct.err_recon_unbalanced", "write off the difference"),
    ("acct.err_currency_unknown", "three-letter currency code"),
    ("settings_accounting.code_too_long", "{max}"),
    ("settings_accounting.account_not_found", "Refresh the chart of accounts"),
    ("acct.error_loading_data", "Refresh the page"),
    ("recon.err_no_lines", "bank statement export"),
])
def test_message_says_what_to_do(key, needle):
    raw = json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))
    assert needle in raw[key]


@pytest.mark.parametrize("key", ["acct.error_loading_entries", "acct.account_code_required",
                                 "contacts.err_record_not_found"])
def test_superseded_keys_are_gone(key):
    for lang in _LANGS:
        raw = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        assert key not in raw, (lang, key)


def test_record_not_found_is_shared_and_translated():
    th = json.loads((_LOCALES / "th.json").read_text(encoding="utf-8"))
    en = json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))
    assert en["error.record_not_found"] != th["error.record_not_found"]


def test_thai_reader_gets_thai_entry_message():
    assert t("acct.err_je_two_lines", "th") != t("acct.err_je_two_lines", "en")


def test_ui_account_code_limit_matches_the_api():
    from celerp_accounting.chart_rules import ACCOUNT_CODE_MAX as api_max
    from ui.routes.settings_accounting import _ACCOUNT_CODE_MAX as ui_max
    assert ui_max == api_max

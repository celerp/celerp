# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Each bank reconciliation write, from the route, on a statement with real book entries.

Suggest, accept, match, attach, remove and complete each change the statement or its
lines; every one is driven here against posted journal entries on the bank's own account,
and the statement's state is read back after each.
"""
from __future__ import annotations

import uuid

import pytest

import bank_statement_helpers as B


async def _company(client) -> dict:
    r = await client.post("/auth/register", json={
        "email": f"recon_{uuid.uuid4().hex[:8]}@example.com", "password": "pass1234",
        "name": "Recon", "company_name": f"Recon {uuid.uuid4().hex[:6]}",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _post(client, h, bank_code: str, ts: str, amount: float, other: str) -> str:
    debit, credit = (amount, 0.0) if amount > 0 else (0.0, -amount)
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": ts, "memo": f"Bank {ts}",
        "entries": [{"account": bank_code, "debit": debit, "credit": credit},
                    {"account": other, "debit": credit, "credit": debit}],
        "idempotency_token": uuid.uuid4().hex,
    })
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _session(client):
    h = await _company(client)
    bank = (await client.post("/accounting/bank-accounts", headers=h, json={
        "bank_name": "Main", "account_number": "1", "bank_type": "checking",
        "currency": "USD", "opening_balance": 1000.0,
    })).json()
    deposit = await _post(client, h, bank["chart_account_code"], "2026-03-05", 500.0, "4100")
    rent = await _post(client, h, bank["chart_account_code"], "2026-03-10", -200.0, "6200")
    r = await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": 1300.0,
    })
    assert r.status_code == 200, r.text
    got = await B.import_into(client, h, r.json()["id"],
                              b"Date,Description,Amount\n2026-03-05,Deposit,500\n2026-03-14,Rent,-200\n")
    return h, got["session_id"], deposit, rent


async def _lines(client, h, sid) -> dict:
    r = await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)
    return {l["description"]: l for l in r.json()["items"]}


async def _state(client, h, sid) -> dict:
    return (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()


@pytest.mark.asyncio
async def test_suggest_then_accept_reconcile_both_book_entries(client):
    h, sid, deposit, rent = await _session(client)
    co, bank_id = await B.co_of(client, h), await B.bank_of(client, h, sid)

    r = await B.bank_post(client, h, sid, "suggest")
    assert r.status_code == 200 and r.json()["suggested"] == 2, r.text
    table = await B.groups(client, co, bank_id)
    by_text = {l["description"]: l for l in B.all_lines(table)}
    assert by_text["Deposit"]["suggestion"]["je_ids"] == [deposit]
    assert by_text["Rent"]["suggestion"]["je_ids"] == [rent]
    assert {l["status"] for l in (await _lines(client, h, sid)).values()} == {"unmatched"}, "a suggestion posts nothing"

    r = await B.resolve(client, co, bank_id, accept={"group_keys": [g["key"] for g in table["groups"]]})
    assert r.status_code == 200 and r.json()["refused"] == 0, r.text
    lines = await _lines(client, h, sid)
    assert (lines["Deposit"]["status"], lines["Deposit"]["matched_je_ids"]) == ("matched", [deposit])
    assert (lines["Rent"]["status"], lines["Rent"]["matched_je_ids"]) == ("matched", [rent])
    assert (await _state(client, h, sid))["difference"] == 0

    assert (await B.complete(client, h, sid))["status"] == "completed"
    r = await client.post(f"/accounting/reconciliation/{sid}/match", headers=h, json={"je_ids": [deposit]})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_matching_book_entries_by_hand_counts_each_once(client):
    h, sid, deposit, rent = await _session(client)

    assert B.stopped_reason(await B.bank_complete(client, h, sid)), "nothing is matched yet, so a difference remains"

    for _ in range(2):
        r = await client.post(f"/accounting/reconciliation/{sid}/match", headers=h,
                              json={"je_ids": [deposit, rent]})
        assert r.status_code == 200, r.text
    assert sorted(r.json()["reconciled_je_ids"]) == sorted([deposit, rent])
    assert (await _state(client, h, sid))["difference"] == 0


@pytest.mark.asyncio
async def test_a_statement_line_attachment_is_added_once_and_removed(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    h, sid, _deposit, _rent = await _session(client)
    line = (await _lines(client, h, sid))["Deposit"]

    for _ in range(2):
        r = await client.post(f"/accounting/reconciliation/{sid}/lines/{line['id']}/attach", headers=h,
                              files={"file": ("slip.pdf", b"%PDF-1.4 slip", "application/pdf")})
        assert r.status_code == 200, r.text
    att = r.json()["attachment_id"]
    assert (await _lines(client, h, sid))["Deposit"]["attachments"] == [{"id": att, "url": r.json()["url"]}]

    r = await client.delete(f"/accounting/reconciliation/{sid}/lines/{line['id']}/attach/{att}", headers=h)
    assert r.json() == {"removed": att}
    assert (await _lines(client, h, sid))["Deposit"]["attachments"] == []
    assert list(tmp_path.iterdir()) == [], "nothing is written relative to the process directory"

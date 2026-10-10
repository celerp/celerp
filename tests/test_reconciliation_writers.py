# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Each bank reconciliation write, from the route, on a statement with real book entries.

Auto-match, match, bulk confirm, attach, remove and complete each change the session or
its statement lines; every one is driven here against posted journal entries on the
bank's own account, and the session's state is read back after each.
"""
from __future__ import annotations

import uuid

import pytest


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
        "currency": "THB", "opening_balance": 1000.0,
    })).json()
    deposit = await _post(client, h, bank["chart_account_code"], "2026-03-05", 500.0, "4100")
    rent = await _post(client, h, bank["chart_account_code"], "2026-03-10", -200.0, "6200")
    r = await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": 1300.0,
    })
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await client.post(
        f"/accounting/reconciliation/{sid}/import-csv", headers=h,
        files={"file": ("s.csv", b"Date,Description,Amount\n2026-03-05,Deposit,500\n2026-03-14,Rent,-200\n", "text/csv")},
    )
    assert r.status_code == 200, r.text
    return h, sid, deposit, rent


async def _lines(client, h, sid) -> dict:
    r = await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)
    return {l["description"]: l for l in r.json()["items"]}


async def _state(client, h, sid) -> dict:
    return (await client.get(f"/accounting/reconciliation/{sid}", headers=h)).json()


@pytest.mark.asyncio
async def test_auto_match_then_bulk_confirm_reconcile_both_book_entries(client):
    h, sid, deposit, rent = await _session(client)

    r = await client.post(f"/accounting/reconciliation/{sid}/auto-match", headers=h)
    assert r.json() == {"matched": 1, "suggested": 1, "total_processed": 2}
    lines = await _lines(client, h, sid)
    assert (lines["Deposit"]["status"], lines["Deposit"]["matched_je_id"]) == ("matched", deposit)
    assert (lines["Rent"]["status"], lines["Rent"]["matched_je_id"]) == ("suggested", rent)
    assert (await _state(client, h, sid))["reconciled_je_ids"] == [deposit]

    r = await client.post(f"/accounting/reconciliation/{sid}/bulk-confirm", headers=h)
    assert r.json() == {"confirmed": 1}
    assert (await _lines(client, h, sid))["Rent"]["status"] == "matched"
    state = await _state(client, h, sid)
    assert sorted(state["reconciled_je_ids"]) == sorted([deposit, rent])
    assert state["difference"] == 0

    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    r = await client.post(f"/accounting/reconciliation/{sid}/match", headers=h, json={"je_ids": [deposit]})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_matching_book_entries_by_hand_counts_each_once(client):
    h, sid, deposit, rent = await _session(client)

    r = await client.post(f"/accounting/reconciliation/{sid}/complete", headers=h)
    assert r.status_code == 422, "nothing is matched yet, so a difference remains"

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
    assert (await _lines(client, h, sid))["Deposit"]["attachment_ids"] == [att]

    r = await client.delete(f"/accounting/reconciliation/{sid}/lines/{line['id']}/attach/{att}", headers=h)
    assert r.json() == {"removed": att}
    assert (await _lines(client, h, sid))["Deposit"]["attachment_ids"] == []

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The opening balance enters a bank reconciliation exactly once.

Creating a bank account with an opening balance posts it as a journal entry on the
bank's own account, and the bank list reads its balance from the ledger alone. A
reconciliation reads the same ledger: the opening entry is the balance brought
forward, so it counts as cleared from the start, and the bank account's
opening_balance column is never added on top of it.

Every route that reports or gates on the remaining difference is driven here: the
session view, the workbench, complete and write-off.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import bank_statement_helpers as B


async def _company(client) -> dict:
    r = await client.post("/auth/register", json={
        "email": f"rob_{uuid.uuid4().hex[:8]}@example.com", "password": "pass1234",
        "name": "Recon", "company_name": f"Recon {uuid.uuid4().hex[:6]}",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _bank(client, h, opening: float) -> dict:
    r = await client.post("/accounting/bank-accounts", headers=h, json={
        "bank_name": "Main", "account_number": "1", "bank_type": "checking",
        "currency": "USD", "opening_balance": opening,
    })
    assert r.status_code == 200, r.text
    return r.json()


async def _deposit(client, h, code: str, amount: float, ts: str = "2026-03-05") -> str:
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": ts, "memo": "Deposit",
        "entries": [{"account": code, "debit": amount, "credit": 0.0},
                    {"account": "4100", "debit": 0.0, "credit": amount}],
        "idempotency_token": uuid.uuid4().hex,
    })
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _start(client, h, bank_id: str, statement: float) -> str:
    r = await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": bank_id, "statement_date": "2026-03-31", "statement_balance": statement,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _listed(client, h, bank_id: str) -> dict:
    items = (await client.get("/accounting/bank-accounts", headers=h)).json()["items"]
    return next(b for b in items if b["id"] == bank_id)


async def _view(client, h, sid: str) -> dict:
    r = await client.get(f"/accounting/reconciliation/{sid}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _workbench(client, h, sid: str) -> dict:
    r = await client.get(f"/accounting/reconciliation/{sid}/workbench", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _complete(client, h, sid: str) -> dict:
    """Complete the bank's open statements: the answer, ``stopped_at`` set when one is off."""
    r = await B.bank_post(client, h, sid, "complete")
    assert r.status_code == 200, r.text
    return r.json()


def _opening(bank: dict) -> str:
    return f"je:opening:{bank['id']}"


@pytest.mark.asyncio
async def test_book_balance_matches_the_bank_list_and_the_opening_entry_is_already_cleared(client):
    h = await _company(client)
    bank = await _bank(client, h, 1000.0)
    deposit = await _deposit(client, h, bank["chart_account_code"], 500.0)
    sid = await _start(client, h, bank["id"], 1500.0)

    listed = await _listed(client, h, bank["id"])
    view = await _view(client, h, sid)
    assert listed["balance"] == view["book_balance"] == 1500.0
    assert [e["je_id"] for e in view["unreconciled_entries"]] == [deposit]
    assert [e["je_id"] for e in view["reconciled_entries"]] == [_opening(bank)]
    assert view["matched_balance"] == 1000.0
    assert view["difference"] == 500.0

    wb = await _workbench(client, h, sid)
    assert [e["je_id"] for e in wb["book_entries"]] == [deposit]
    assert wb["difference"] == 500.0


@pytest.mark.asyncio
async def test_matching_every_offered_entry_reconciles_a_statement_that_agrees_with_the_books(client):
    h = await _company(client)
    bank = await _bank(client, h, 1000.0)
    await _deposit(client, h, bank["chart_account_code"], 500.0)
    sid = await _start(client, h, bank["id"], 1500.0)

    for e in (await _view(client, h, sid))["unreconciled_entries"]:
        r = await client.post(f"/accounting/reconciliation/{sid}/match", headers=h, json={"je_ids": [e["je_id"]]})
        assert r.status_code == 200, r.text
    after = await _view(client, h, sid)
    assert (after["matched_balance"], after["difference"]) == (1500.0, 0.0)
    assert (await _workbench(client, h, sid))["difference"] == 0.0
    assert (await _complete(client, h, sid))["stopped_at"] is None
    assert (await _view(client, h, sid))["status"] == "completed"


@pytest.mark.asyncio
async def test_an_opening_entry_held_by_hand_still_counts_once(client, session):
    from celerp_accounting.models import ReconciliationSession

    h = await _company(client)
    bank = await _bank(client, h, 1000.0)
    deposit = await _deposit(client, h, bank["chart_account_code"], 500.0)
    sid = await _start(client, h, bank["id"], 1500.0)

    r = await client.post(f"/accounting/reconciliation/{sid}/match", headers=h,
                          json={"je_ids": [_opening(bank)]})
    assert r.status_code == 400, r.text
    # A session from before matching was checked may hold the opening entry by id.
    recon = await session.get(ReconciliationSession, uuid.UUID(sid))
    recon.reconciled_je_ids = [_opening(bank), deposit]
    await session.flush()
    view = await _view(client, h, sid)
    assert (view["book_balance"], view["matched_balance"], view["difference"]) == (1500.0, 1500.0, 0.0)
    assert view["unreconciled_entries"] == []
    wb = await _workbench(client, h, sid)
    assert (wb["book_entries"], wb["difference"]) == ([], 0.0)
    assert (await _complete(client, h, sid))["stopped_at"] is None


@pytest.mark.asyncio
async def test_write_off_takes_only_the_true_difference(client):
    h = await _company(client)
    bank = await _bank(client, h, 1000.0)
    deposit = await _deposit(client, h, bank["chart_account_code"], 500.0)
    sid = await _start(client, h, bank["id"], 1500.5)

    r = await client.post(f"/accounting/reconciliation/{sid}/match", headers=h,
                          json={"je_ids": [deposit]})
    assert r.status_code == 200, r.text
    r = await client.post(f"/accounting/reconciliation/{sid}/write-off", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == pytest.approx(0.5)
    view = await _view(client, h, sid)
    assert view["difference"] == pytest.approx(0.0)
    assert (await _complete(client, h, sid))["stopped_at"] is None


@pytest.mark.asyncio
async def test_auto_match_never_pairs_a_statement_line_with_the_opening_entry(client):
    h = await _company(client)
    bank = await _bank(client, h, 1000.0)
    sid = await _start(client, h, bank["id"], 2000.0)
    opening_ts = next(e["ts"] for e in (await _view(client, h, sid))["all_entries"] if e["je_id"] == _opening(bank))
    # A statement that ends on the opening entry's date, so its line can sit beside it.
    r = await client.request("DELETE", f"{B.API}/{sid}", json={"idempotency_key": B.key()}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.post("/accounting/reconciliation/start", headers=h, json={
        "bank_account_id": bank["id"], "statement_date": opening_ts[:10], "statement_balance": 2000.0})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    csv = f"Date,Description,Amount\n{opening_ts[:10]},Customer receipt,1000\n".encode()
    sid = (await B.import_into(client, h, sid, csv, "s.csv"))["session_id"]
    r = await B.bank_post(client, h, sid, "suggest")
    assert r.status_code == 200, r.text
    (line,) = B.all_lines(await B.groups(client, await B.co_of(client, h), bank["id"], view="all"))
    assert (line["status"], line["matched_je_ids"]) == ("unmatched", [])
    assert _opening(bank) not in str(line.get("suggestion"))
    assert (await _view(client, h, sid))["difference"] == 1000.0


# Neighbour guards.

@pytest.mark.asyncio
async def test_a_bank_with_no_opening_balance_reconciles_from_its_entries_alone(client):
    h = await _company(client)
    bank = await _bank(client, h, 0.0)
    deposit = await _deposit(client, h, bank["chart_account_code"], 500.0)
    sid = await _start(client, h, bank["id"], 500.0)

    view = await _view(client, h, sid)
    assert (await _listed(client, h, bank["id"]))["balance"] == view["book_balance"] == 500.0
    assert [e["je_id"] for e in view["unreconciled_entries"]] == [deposit]
    assert view["reconciled_entries"] == []
    assert (view["matched_balance"], view["difference"]) == (0.0, 500.0)
    await client.post(f"/accounting/reconciliation/{sid}/match", headers=h, json={"je_ids": [deposit]})
    assert (await _view(client, h, sid))["difference"] == 0.0
    assert (await _complete(client, h, sid))["stopped_at"] is None


@pytest.mark.asyncio
async def test_an_opening_balance_with_no_posted_entry_is_not_counted(client, session):
    from celerp_accounting.models import BankAccount

    h = await _company(client)
    bank = await _bank(client, h, 0.0)
    deposit = await _deposit(client, h, bank["chart_account_code"], 500.0)
    row = (await session.execute(select(BankAccount).where(BankAccount.id == uuid.UUID(bank["id"])))).scalar_one()
    row.opening_balance = 1000.0
    await session.commit()
    sid = await _start(client, h, bank["id"], 500.0)

    listed = await _listed(client, h, bank["id"])
    assert (listed["balance"], listed["opening_unbacked"]) == (500.0, True)
    view = await _view(client, h, sid)
    assert view["book_balance"] == listed["balance"]
    assert (view["matched_balance"], view["difference"]) == (0.0, 500.0)
    await client.post(f"/accounting/reconciliation/{sid}/match", headers=h, json={"je_ids": [deposit]})
    assert (await _view(client, h, sid))["difference"] == 0.0


@pytest.mark.asyncio
async def test_matching_then_unmatching_a_line_restores_the_difference(client):
    h = await _company(client)
    bank = await _bank(client, h, 1000.0)
    deposit = await _deposit(client, h, bank["chart_account_code"], 500.0)
    sid = await _start(client, h, bank["id"], 1500.0)
    data = await B.import_into(client, h, sid, b"Date,Description,Amount\n2026-03-20,Deposit,500\n", "s.csv")
    sid, (line,) = data["session_id"], data["lines"]
    r = await B.decide(client, h, sid, line["id"], "match", je_ids=[deposit])
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    assert (await _view(client, h, sid))["difference"] == 0.0
    assert (await _workbench(client, h, sid))["difference"] == 0.0

    r = await B.undo(client, h, sid, [line["id"]])
    assert r.status_code == 200, r.text
    view = await _view(client, h, sid)
    assert (view["matched_balance"], view["difference"]) == (1000.0, 500.0)
    assert [e["je_id"] for e in view["unreconciled_entries"]] == [deposit]
    assert (await _complete(client, h, sid))["stopped_at"] is not None

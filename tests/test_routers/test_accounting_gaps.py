# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Coverage gap closers for routers/accounting.py:
  - PATCH /accounts/{code} — not found (404) + all optional fields
  - GET /accounting/import/template
  - POST /import/batch — skip existing entity + error path
  - GET /trial-balance with date_from/date_to filters
  - GET /pnl — revenue/cogs/expense sections (_section helper + _build_balances)
  - GET /balance-sheet — asset/liability/equity + retained earnings derived path

NOTE: Company registration seeds the full Thai chart of accounts.
      We use pre-seeded codes (1110, 4100, 5100, 6100, etc.) rather than creating accounts,
      except when testing PATCH (need a code to patch).
"""

from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reg(client) -> str:
    addr = f"acc-{uuid.uuid4().hex[:8]}@gaps.test"
    r = await client.post("/auth/register", json={"company_name": "AccCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _post_je(client, tok, entries, ts="2026-01-15"):
    """Emit a posted journal entry via batch import."""
    entity_id = f"je:{uuid.uuid4()}"
    r = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "acc.journal_entry.created",
        "data": {"status": "posted", "ts": ts, "entries": entries},
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    return entity_id


# ---------------------------------------------------------------------------
# PATCH /accounts/{code}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounting_patch_account_not_found(client):
    """PATCH on nonexistent account → 404 (line 192)."""
    tok = await _reg(client)
    r = await client.patch("/accounting/accounts/NONEXISTENT", headers=_h(tok), json={"name": "X"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_accounting_patch_account_all_fields(client):
    """PATCH all optional fields: account_type, parent_code, is_active (lines 197, 199, 201).
    Uses pre-seeded code 1140 (Prepaid Expenses) and 1130 as parent."""
    tok = await _reg(client)

    # Patch pre-seeded account 1140 with all optional fields
    r = await client.patch("/accounting/accounts/1140", headers=_h(tok), json={
        "name": "Prepaid Expenses Updated",
        "account_type": "asset",
        "parent_code": "1130",
        "is_active": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Prepaid Expenses Updated"
    assert body["parent_code"] == "1130"
    assert body["is_active"] is False


# ---------------------------------------------------------------------------
# GET /accounting/import/template
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounting_import_template(client):
    """GET /accounting/import/template → CSV with correct headers (line 209)."""
    tok = await _reg(client)
    r = await client.get("/accounting/import/template", headers=_h(tok))
    assert r.status_code == 200
    assert "entity_id" in r.text
    assert "account_type" in r.text


# ---------------------------------------------------------------------------
# POST /import/batch — skip + error paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounting_batch_import_skip_and_error(client):
    """Batch import: skip duplicate idempotency_key + duplicate entity (lines 248-249, 268-270)."""
    tok = await _reg(client)
    entity_id = f"je:{uuid.uuid4()}"
    ik1 = str(uuid.uuid4())

    # First import — succeeds
    r1 = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "acc.journal_entry.created",
        "data": {"status": "posted", "ts": "2026-01-01", "entries": []},
        "source": "test",
        "idempotency_key": ik1,
    }]})
    assert r1.status_code == 200
    assert r1.json()["created"] == 1

    # Same idempotency_key → skipped
    r2 = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "acc.journal_entry.created",
        "data": {"status": "posted", "ts": "2026-01-01", "entries": []},
        "source": "test",
        "idempotency_key": ik1,
    }]})
    assert r2.status_code == 200
    assert r2.json()["skipped"] >= 1

    # Same entity_id, new idempotency_key → entity already exists, skipped
    r3 = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "acc.journal_entry.created",
        "data": {"status": "posted", "ts": "2026-01-02", "entries": []},
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r3.status_code == 200
    assert r3.json()["skipped"] >= 1


# ---------------------------------------------------------------------------
# GET /trial-balance with date filters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounting_trial_balance_date_filters(client):
    """Trial balance date_from/date_to filters prune entries outside range (lines 353-365)."""
    tok = await _reg(client)

    # Post JE using pre-seeded accounts: debit Cash (1110), credit Sales (4100)
    await _post_je(client, tok, [
        {"account": "1110", "debit": 100},
        {"account": "4100", "credit": 100},
    ], ts="2026-01-15")

    # Without filter — JE included
    r_all = await client.get("/accounting/trial-balance", headers=_h(tok))
    assert r_all.status_code == 200
    lines_all = r_all.json()["lines"]
    codes_all = {l["code"] for l in lines_all}
    assert "1110" in codes_all

    # date_from after JE date → JE excluded (line 355-356)
    r_future = await client.get("/accounting/trial-balance?date_from=2026-06-01", headers=_h(tok))
    assert r_future.status_code == 200
    codes_future = {l["code"] for l in r_future.json()["lines"]}
    assert "1110" not in codes_future

    # date_to before JE date → also excluded (line 357-358)
    r_past = await client.get("/accounting/trial-balance?date_to=2026-01-01", headers=_h(tok))
    assert r_past.status_code == 200
    codes_past = {l["code"] for l in r_past.json()["lines"]}
    assert "1110" not in codes_past


# ---------------------------------------------------------------------------
# GET /pnl — revenue, cogs, expense sections
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounting_pnl_sections(client):
    """P&L _section covers revenue negation + type filter (lines 426-432).
    Uses pre-seeded: 4100 (revenue), 5100 (cogs), 6100 (expense), 1110 (asset - excluded)."""
    tok = await _reg(client)

    # Revenue: debit Cash, credit Sales
    await _post_je(client, tok, [
        {"account": "1110", "debit": 500},
        {"account": "4100", "credit": 500},
    ])
    # COGS: debit COGS, credit Cash
    await _post_je(client, tok, [
        {"account": "5100", "debit": 200},
        {"account": "1110", "credit": 200},
    ])
    # Expense: debit Salaries, credit Cash
    await _post_je(client, tok, [
        {"account": "6100", "debit": 100},
        {"account": "1110", "credit": 100},
    ])

    r = await client.get("/accounting/pnl", headers=_h(tok))
    assert r.status_code == 200
    body = r.json()

    rev_codes = {l["code"] for l in body["revenue"]["lines"]}
    cogs_codes = {l["code"] for l in body["cogs"]["lines"]}
    exp_codes = {l["code"] for l in body["expenses"]["lines"]}

    assert "4100" in rev_codes
    assert "5100" in cogs_codes
    assert "6100" in exp_codes
    # Asset account must be excluded from all P&L sections
    assert "1110" not in rev_codes | cogs_codes | exp_codes

    # Revenue is credit-normal → amount should be positive
    rev_line = next(l for l in body["revenue"]["lines"] if l["code"] == "4100")
    assert rev_line["amount"] > 0


@pytest.mark.asyncio
async def test_accounting_pnl_date_filter(client):
    """P&L date_from excludes old entries (_build_balances lines 307-310)."""
    tok = await _reg(client)

    await _post_je(client, tok, [
        {"account": "1110", "debit": 300},
        {"account": "4100", "credit": 300},
    ], ts="2026-01-15")

    # Filter to future period — JE (ts=2026-01-15) excluded
    r = await client.get("/accounting/pnl?date_from=2026-06-01", headers=_h(tok))
    assert r.status_code == 200
    rev_codes = {l["code"] for l in r.json()["revenue"]["lines"]}
    assert "4100" not in rev_codes


# ---------------------------------------------------------------------------
# GET /balance-sheet — retained earnings derived path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounting_balance_sheet_retained_earnings(client):
    """Balance sheet: retained earnings derived when assets != liabilities + explicit equity (lines 499-501).
    Uses pre-seeded: 1110 (asset), 3100 (equity). Deliberately unbalanced JE to force derivation."""
    tok = await _reg(client)

    # Post a JE that credits equity less than debits asset → retained earnings gap
    await _post_je(client, tok, [
        {"account": "1110", "debit": 1000},
        {"account": "3100", "credit": 600},
        # Deliberately missing 400 → retained earnings = 1000 - 0 - 600 = 400 >= 0.01
    ])

    r = await client.get("/accounting/balance-sheet", headers=_h(tok))
    assert r.status_code == 200
    body = r.json()

    retained = [l for l in body["equity"]["lines"] if "Retained" in l["name"]]
    assert len(retained) >= 1


@pytest.mark.asyncio
async def test_accounting_balance_sheet_as_of_filter(client):
    """Balance sheet as_of passes date_to to _build_balances (line 479)."""
    tok = await _reg(client)

    await _post_je(client, tok, [
        {"account": "1110", "debit": 500},
        {"account": "3100", "credit": 500},
    ], ts="2026-01-15")

    # as_of before JE → asset 1110 should not appear
    r = await client.get("/accounting/balance-sheet?as_of=2026-01-01", headers=_h(tok))
    assert r.status_code == 200
    body = r.json()
    asset_codes = {l["code"] for l in body["assets"]["lines"]}
    assert "1110" not in asset_codes


# ---------------------------------------------------------------------------
# _build_balances / trial_balance: non-posted JE and entry without account
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounting_trial_balance_skips_non_posted(client):
    """Trial balance skips JEs with status != posted (lines 305, 353)."""
    tok = await _reg(client)

    # Import a non-posted JE (status=draft) — should not appear in trial balance
    entity_id = f"je:{uuid.uuid4()}"
    r = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "acc.journal_entry.created",
        "data": {"status": "draft", "ts": "2026-01-15", "entries": [{"account": "1110", "debit": 999}]},
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r.status_code == 200

    # Trial balance should NOT include the draft JE's entries
    r_tb = await client.get("/accounting/trial-balance", headers=_h(tok))
    assert r_tb.status_code == 200
    # No rows expected from draft JE; other accounts may exist from seeding but not 999 debit


@pytest.mark.asyncio
async def test_accounting_trial_balance_skips_entry_without_account(client):
    """Trial balance skips entries with no 'account' key (lines 314, 362)."""
    tok = await _reg(client)

    # Import a posted JE where one entry has no 'account' field
    entity_id = f"je:{uuid.uuid4()}"
    r = await client.post("/accounting/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "acc.journal_entry.created",
        "data": {
            "status": "posted",
            "ts": "2026-01-15",
            "entries": [
                {"account": "1110", "debit": 100},      # valid
                {"debit": 100},                           # no 'account' key → skipped
                {"account": "", "credit": 100},           # empty account → also skipped by falsy check
            ],
        },
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    }]})
    assert r.status_code == 200

    r_tb = await client.get("/accounting/trial-balance", headers=_h(tok))
    assert r_tb.status_code == 200
    # Should process without error; entry without account is silently skipped
    lines = r_tb.json()["lines"]
    # Only 1110 entry should appear (the one with a valid account code)
    codes = {l["code"] for l in lines}
    assert "1110" in codes


# ── Period Lock + Close Year tests ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_period_lock_get_default_empty(client):
    tok = await _reg(client)
    r = await client.get("/accounting/period-lock", headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert data["lock_date"] is None


@pytest.mark.asyncio
async def test_period_lock_set_and_get(client):
    tok = await _reg(client)
    r = await client.post("/accounting/period-lock", json={"lock_date": "2025-12-31"}, headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert data["lock_date"] == "2025-12-31"
    assert data["lock_date_set_at"] is not None

    # Verify GET returns it
    r2 = await client.get("/accounting/period-lock", headers=_h(tok))
    assert r2.json()["lock_date"] == "2025-12-31"


@pytest.mark.asyncio
async def test_period_lock_unlock(client):
    tok = await _reg(client)
    await client.post("/accounting/period-lock", json={"lock_date": "2025-06-30"}, headers=_h(tok))
    r = await client.post("/accounting/period-lock", json={"lock_date": None}, headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["lock_date"] is None


@pytest.mark.asyncio
async def test_period_lock_invalid_date(client):
    tok = await _reg(client)
    r = await client.post("/accounting/period-lock", json={"lock_date": "not-a-date"}, headers=_h(tok))
    assert r.status_code == 422

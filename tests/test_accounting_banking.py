# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for bank accounts, transfers, and reconciliation endpoints."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


# ── helpers ──────────────────────────────────────────────────────────────────

import uuid as _uuid

async def _headers(client: AsyncClient, suffix: str = "") -> dict:
    uid = suffix or _uuid.uuid4().hex[:8]
    r = await client.post(
        "/auth/register",
        json={
            "email": f"banker_{uid}@example.com",
            "password": "pass1234",
            "name": "Bank Tester",
            "company_name": f"BankerCo_{uid}",
        },
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── Bank Accounts CRUD ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bank_account_create_and_list(client):
    headers = await _headers(client)

    r = await client.post(
        "/accounting/bank-accounts",
        json={
            "bank_name": "Bangkok Bank",
            "account_number": "1234567890",
            "currency": "THB",
            "bank_type": "checking",
        },
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["bank_name"] == "Bangkok Bank"
    bank_id = data["id"]

    r2 = await client.get("/accounting/bank-accounts", headers=headers)
    assert r2.status_code == 200
    items = r2.json()["items"]
    assert any(b["id"] == bank_id for b in items)


@pytest.mark.asyncio
async def test_bank_account_patch(client):
    headers = await _headers(client)

    r = await client.post(
        "/accounting/bank-accounts",
        json={"bank_name": "SCB", "account_number": "9876543210",
              "currency": "THB", "bank_type": "savings"},
        headers=headers,
    )
    assert r.status_code == 200
    bank_id = r.json()["id"]

    r2 = await client.patch(
        f"/accounting/bank-accounts/{bank_id}",
        json={"bank_name": "Emergency Savings SCB"},
        headers=headers,
    )
    assert r2.status_code == 200
    assert r2.json()["bank_name"] == "Emergency Savings SCB"


@pytest.mark.asyncio
async def test_bank_account_get_single(client):
    headers = await _headers(client)

    r = await client.post(
        "/accounting/bank-accounts",
        json={"bank_name": "Petty Cash", "account_number": "0000",
              "currency": "THB", "bank_type": "savings"},
        headers=headers,
    )
    assert r.status_code == 200
    bank_id = r.json()["id"]

    r2 = await client.get(f"/accounting/bank-accounts/{bank_id}", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["id"] == bank_id


# ── Transfers ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transfer_between_accounts(client):
    headers = await _headers(client)

    r1 = await client.post(
        "/accounting/bank-accounts",
        json={"bank_name": "KBank From", "account_number": "111",
              "currency": "THB", "bank_type": "checking"},
        headers=headers,
    )
    r2 = await client.post(
        "/accounting/bank-accounts",
        json={"bank_name": "KBank To", "account_number": "222",
              "currency": "THB", "bank_type": "savings"},
        headers=headers,
    )
    from_id = r1.json()["id"]
    to_id = r2.json()["id"]

    r3 = await client.post(
        "/accounting/transfers",
        json={
            "from_bank_id": from_id,
            "to_bank_id": to_id,
            "amount": 5000.0,
            "date": "2026-03-20",
            "description": "Monthly transfer",
        },
        headers=headers,
    )
    assert r3.status_code == 200
    result = r3.json()
    assert "je_id" in result or "id" in result or "event_id" in result


# ── Reconciliation ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconciliation_lifecycle(client):
    headers = await _headers(client)

    r = await client.post(
        "/accounting/bank-accounts",
        json={"bank_name": "Test Bank", "account_number": "999",
              "currency": "THB", "bank_type": "checking"},
        headers=headers,
    )
    assert r.status_code == 200
    bank_id = r.json()["id"]

    r2 = await client.post(
        "/accounting/reconciliation/start",
        json={
            "bank_account_id": bank_id,
            "statement_date": "2026-03-31",
            "statement_balance": 10000.0,
        },
        headers=headers,
    )
    assert r2.status_code == 200
    session_id = r2.json().get("session_id") or r2.json().get("id")
    assert session_id

    r3 = await client.get(f"/accounting/reconciliation/{session_id}", headers=headers)
    assert r3.status_code == 200
    recon = r3.json()
    assert recon.get("status") in ("open", "pending", "active")


@pytest.mark.asyncio
async def test_bank_account_list_response_shape(client):
    """List endpoint returns 'items' key with chart_account_code and bank_name.

    Regression: UI dropdown previously used 'accounts', 'account_code', 'name'
    which are all wrong keys. This test locks in the correct shape so the bug
    cannot silently re-appear.
    """
    headers = await _headers(client)
    r = await client.get("/accounting/bank-accounts", headers=headers)
    assert r.status_code == 200
    body = r.json()
    # Envelope shape — must be 'items', not 'accounts' or 'data'
    assert "items" in body, "response must use 'items' key, not 'accounts' or 'data'"
    assert "total" in body
    # Every item must have the fields the payment dropdown depends on
    for item in body["items"]:
        assert "chart_account_code" in item, "missing chart_account_code (UI dropdown uses this as option value)"
        assert "bank_name" in item, "missing bank_name (UI dropdown uses this as option label)"
        assert "id" in item


@pytest.mark.asyncio
async def test_multiple_bank_accounts_all_in_list(client):
    """Creating two bank accounts — both appear in the list response.

    Regression: payment dropdown only showed 'Default' because the UI read
    the wrong key ('accounts' instead of 'items') and fell back to a hardcoded
    option. Verify all created accounts are present.
    """
    headers = await _headers(client)
    names = ["Kasikorn Bank", "SCB Savings"]
    ids = []
    for name in names:
        r = await client.post(
            "/accounting/bank-accounts",
            json={"bank_name": name, "account_number": "0000000001",
                  "currency": "THB", "bank_type": "savings"},
            headers=headers,
        )
        assert r.status_code == 200
        ids.append(r.json()["id"])

    r2 = await client.get("/accounting/bank-accounts", headers=headers)
    assert r2.status_code == 200
    listed_ids = {b["id"] for b in r2.json()["items"]}
    for bank_id in ids:
        assert bank_id in listed_ids, f"bank account {bank_id} missing from list"

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""Tests for bank account ISO 4217 currency validation (Issue 2)."""

from __future__ import annotations

import uuid

import pytest


async def _register(client, email: str | None = None) -> str:
    addr = email or f"bank-{uuid.uuid4().hex[:8]}@test.local"
    r = await client.post("/auth/register", json={"company_name": "BankCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bank_payload(**kwargs) -> dict:
    base = {"bank_name": "Test Bank", "account_number": "9999", "bank_type": "checking", "currency": "USD", "opening_balance": 0}
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_create_bank_account_invalid_currency(client):
    """Creating a bank account with an invalid currency code returns 422."""
    token = await _register(client)
    r = await client.post("/accounting/bank-accounts", headers=_h(token),
                          json=_bank_payload(currency="FFF"))
    assert r.status_code == 422
    assert "FFF" in r.json().get("detail", "")


@pytest.mark.asyncio
async def test_create_bank_account_currency_normalized_uppercase(client):
    """Currency code is stored uppercase even if submitted lowercase."""
    token = await _register(client)
    r = await client.post("/accounting/bank-accounts", headers=_h(token),
                          json=_bank_payload(bank_name="EUR Bank", account_number="1234", currency="eur"))
    assert r.status_code == 200
    assert r.json()["currency"] == "EUR"


@pytest.mark.asyncio
async def test_create_bank_account_common_currencies(client):
    """Spot-check that common ISO 4217 codes are all accepted."""
    token = await _register(client)
    for code in ["USD", "EUR", "THB", "GBP", "JPY", "SGD"]:
        r = await client.post("/accounting/bank-accounts", headers=_h(token),
                              json=_bank_payload(bank_name=f"{code} Bank", account_number=code, currency=code))
        assert r.status_code == 200, f"Expected 200 for {code}, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_patch_bank_account_invalid_currency(client):
    """Patching a bank account with an invalid currency code returns 422."""
    token = await _register(client)
    r = await client.post("/accounting/bank-accounts", headers=_h(token),
                          json=_bank_payload(bank_name="My Bank"))
    assert r.status_code == 200
    bank_id = r.json()["id"]
    r2 = await client.patch(f"/accounting/bank-accounts/{bank_id}", headers=_h(token),
                            json={"currency": "XYZ"})
    assert r2.status_code == 422
    assert "XYZ" in r2.json().get("detail", "")


@pytest.mark.asyncio
async def test_patch_bank_account_currency_normalized(client):
    """Patching currency with lowercase normalizes to uppercase."""
    token = await _register(client)
    r = await client.post("/accounting/bank-accounts", headers=_h(token),
                          json=_bank_payload(bank_name="Patch Bank"))
    bank_id = r.json()["id"]
    r2 = await client.patch(f"/accounting/bank-accounts/{bank_id}", headers=_h(token),
                            json={"currency": "gbp"})
    assert r2.status_code == 200
    assert r2.json()["currency"] == "GBP"

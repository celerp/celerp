# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Integration tests for monetary precision: round_money applied at every computation boundary."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest


async def _register(client) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": f"Prec-{uuid.uuid4().hex[:6]}", "email": f"prec-{uuid.uuid4().hex[:8]}@test.test", "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_and_fetch(client, token: str, payload: dict) -> dict:
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice", "contact_id": "contact:1", **payload})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r2 = await client.get(f"/docs/{doc_id}", headers=_h(token))
    assert r2.status_code == 200
    return r2.json()


@pytest.mark.asyncio
async def test_line_total_rounded_on_create(client):
    """38.6 × 38.86 = 1499.996 raw float → must store 1500.00."""
    token = await _register(client)
    doc = await _create_and_fetch(client, token, {
        "line_items": [{"name": "A", "quantity": 38.6, "unit_price": 38.86}],
        "tax": 0,
    })
    li = doc["line_items"][0]
    assert Decimal(str(li["line_total"])) == Decimal("1500.00")
    assert Decimal(str(doc["subtotal"])) == Decimal("1500.00")


@pytest.mark.asyncio
async def test_amount_outstanding_equals_total(client):
    """amount_outstanding must equal total exactly on creation."""
    token = await _register(client)
    doc = await _create_and_fetch(client, token, {
        "line_items": [{"name": "A", "quantity": 38.6, "unit_price": 38.86}],
        "tax": 105.00,
        "total": 1605.00,
    })
    assert Decimal(str(doc["amount_outstanding"])) == Decimal(str(doc["total"]))


@pytest.mark.asyncio
async def test_payment_of_displayed_amount_accepted(client):
    """Paying the displayed (rounded) total must succeed - no 409 conflict."""
    token = await _register(client)
    doc = await _create_and_fetch(client, token, {
        "line_items": [{"name": "A", "quantity": 1, "unit_price": 1605.00}],
        "tax": 0,
        "total": 1605.00,
    })
    doc_id = doc["id"]
    r = await client.post(f"/docs/{doc_id}/send", headers=_h(token), json={})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{doc_id}/payment", headers=_h(token), json={
        "amount": doc["total"], "payment_date": "2026-05-04", "bank_account": "1111",
    })
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_partial_payment_outstanding_exact(client):
    """After partial payment, outstanding = total - paid with no float noise."""
    token = await _register(client)
    total = 2782.17
    doc = await _create_and_fetch(client, token, {
        "line_items": [{"name": "A", "quantity": 1, "unit_price": total}],
        "tax": 0,
        "total": total,
    })
    doc_id = doc["id"]
    r = await client.post(f"/docs/{doc_id}/send", headers=_h(token), json={})
    assert r.status_code == 200

    r = await client.post(f"/docs/{doc_id}/payment", headers=_h(token), json={
        "amount": 1000.00, "payment_date": "2026-05-04", "bank_account": "1111",
    })
    assert r.status_code == 200

    r = await client.get(f"/docs/{doc_id}", headers=_h(token))
    updated = r.json()
    outstanding = Decimal(str(updated["amount_outstanding"]))
    assert outstanding == Decimal("1782.17")


@pytest.mark.asyncio
async def test_kwd_3dp_precision(client):
    """KWD: line_total must round to 3 decimal places."""
    token = await _register(client)
    doc = await _create_and_fetch(client, token, {
        "currency": "KWD",
        "line_items": [{"name": "A", "quantity": 1, "unit_price": 10.1234}],
        "tax": 0,
    })
    li = doc["line_items"][0]
    assert Decimal(str(li["line_total"])) == Decimal("10.123")


@pytest.mark.asyncio
async def test_jpy_0dp_precision(client):
    """JPY: line_total must round to 0 decimal places (integer)."""
    token = await _register(client)
    doc = await _create_and_fetch(client, token, {
        "currency": "JPY",
        "line_items": [{"name": "A", "quantity": 1.5, "unit_price": 100}],
        "tax": 0,
    })
    li = doc["line_items"][0]
    # 1.5 × 100 = 150.0 → rounded to 0dp = 150
    assert Decimal(str(li["line_total"])) == Decimal("150")


@pytest.mark.asyncio
async def test_subtotal_is_sum_of_rounded_line_totals(client):
    """subtotal must equal sum of per-line rounded totals (not recomputed from raw)."""
    token = await _register(client)
    # Two lines: 38.6×38.86=1499.996→1500.00, 1×99.99=99.99
    doc = await _create_and_fetch(client, token, {
        "line_items": [
            {"name": "A", "quantity": 38.6, "unit_price": 38.86},
            {"name": "B", "quantity": 1, "unit_price": 99.99},
        ],
        "tax": 0,
    })
    li_totals = [Decimal(str(li["line_total"])) for li in doc["line_items"]]
    expected_subtotal = sum(li_totals)
    assert Decimal(str(doc["subtotal"])) == expected_subtotal

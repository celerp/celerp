# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Integration tests for multi-currency document support."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client) -> str:
    addr = f"fx-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post("/auth/register", json={"company_name": "FX Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


async def _make_invoice(client, h, currency="USD", conversion_rate=35.0):
    payload = {
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": currency,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0,
        "tax": 0.0,
        "total": 100.0,
    }
    if currency != "USD" or conversion_rate != 1.0:
        payload["conversion_rate"] = conversion_rate
    r = await client.post("/docs", headers=h, json=payload)
    return r


@pytest.mark.asyncio
async def test_create_foreign_currency_invoice(client):
    token = await _register(client)
    h = _auth(token)

    r = await _make_invoice(client, h, "USD", 35.0)
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    assert doc.get("currency") == "USD"
    assert float(doc.get("conversion_rate", 0)) == 35.0


@pytest.mark.asyncio
async def test_invalid_currency_code_rejected(client):
    token = await _register(client)
    h = _auth(token)

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "XYZ",
        "conversion_rate": 1.0,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0}],
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_finalize_base_currency_doc_succeeds(client):
    """Regression: base-currency docs must finalize unchanged (no rate required)."""
    token = await _register(client)
    h = _auth(token)

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 200


@pytest.mark.asyncio
async def test_finalize_foreign_currency_without_rate_fails(client):
    token = await _register(client)
    h = _auth(token)

    # Set base currency to THB so USD docs are foreign
    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        # No conversion_rate
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 422
    assert "conversion rate" in finalize.json()["detail"].lower()


@pytest.mark.asyncio
async def test_finalize_with_rate_creates_base_currency_jes(client, session):
    """JE debit on AR account must equal total_usd * rate in base currency."""
    token = await _register(client)
    h = _auth(token)

    # Set base currency to THB
    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        "conversion_rate": 35.0,
        "line_items": [{"name": "Widget", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc = r.json()
    doc_id = doc.get("id")

    finalize = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert finalize.status_code == 200, finalize.text

    # Read the auto-JE ledger entries for this doc
    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()

    created_row = next(
        (row for row in rows if row.event_type == "acc.journal_entry.created"),
        None,
    )
    assert created_row is not None, "JE created event not found"
    entries = created_row.data.get("entries", [])
    ar_entry = next((e for e in entries if e.get("account") == "1120" and e.get("debit", 0) > 0), None)
    assert ar_entry is not None, "AR debit entry not found"
    # 100 USD * 35.0 = 3500 THB
    assert abs(float(ar_entry["debit"]) - 3500.0) < 0.01, f"Expected 3500, got {ar_entry['debit']}"


@pytest.mark.asyncio
async def test_finalize_with_rate_revenue_entry_in_base(client, session):
    """Revenue credit entry must also be in base currency."""
    token = await _register(client)
    h = _auth(token)

    await client.patch("/companies/me", headers=h, json={"settings": {"currency": "THB"}})

    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_id": "contact:1",
        "currency": "USD",
        "conversion_rate": 40.0,
        "line_items": [{"name": "Item", "quantity": 2, "unit_price": 50.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json().get("id")

    await client.post(f"/docs/{doc_id}/finalize", headers=h)

    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"))
    )).scalars().all()
    created_row = next((row for row in rows if row.event_type == "acc.journal_entry.created"), None)
    assert created_row is not None
    entries = created_row.data.get("entries", [])
    rev_entry = next((e for e in entries if e.get("account") == "4100" and e.get("credit", 0) > 0), None)
    assert rev_entry is not None
    # 100 USD * 40 = 4000 THB
    assert abs(float(rev_entry["credit"]) - 4000.0) < 0.01, f"Expected 4000, got {rev_entry['credit']}"

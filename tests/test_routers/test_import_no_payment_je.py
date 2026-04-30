# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for Issue 4: snapshot import must not synthesize payment JEs."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest



async def _register(client, email: str | None = None) -> str:
    addr = email or f"imp-{uuid.uuid4().hex[:8]}@test.local"
    r = await client.post("/auth/register", json={"company_name": "ImportCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_snapshot_import_paid_invoice_no_payment_je(client):
    """Importing a paid invoice snapshot creates finalization JE but no payment JE."""
    token = await _register(client)
    entity_id = f"doc:snap-{uuid.uuid4().hex[:8]}"

    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {
            "doc_type": "invoice", "total": 500.0, "subtotal": 500.0, "tax": 0.0,
            "status": "paid", "amount_paid": 500.0, "amount_outstanding": 0.0,
            "issue_date": "2026-01-10",
        },
        "source": "import:test",
        "idempotency_key": f"idem-snap-{uuid.uuid4().hex}",
    })
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{entity_id}", headers=_h(token))).json()
    assert doc["status"] == "paid"
    assert doc["amount_outstanding"] == 0.0
    assert doc["amount_paid"] == 500.0

    # No payments list should exist (no explicit payment event was sent)
    assert doc.get("payments", []) == []


@pytest.mark.asyncio
async def test_snapshot_import_partial_invoice_shows_outstanding(client):
    """Partial-paid imported invoice shows correct outstanding balance from snapshot."""
    token = await _register(client)
    entity_id = f"doc:partial-{uuid.uuid4().hex[:8]}"

    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {
            "doc_type": "invoice", "total": 1000.0, "subtotal": 1000.0, "tax": 0.0,
            "status": "partial", "amount_paid": 400.0, "amount_outstanding": 600.0,
            "issue_date": "2026-01-10",
        },
        "source": "import:test",
        "idempotency_key": f"idem-partial-{uuid.uuid4().hex}",
    })
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{entity_id}", headers=_h(token))).json()
    assert doc["status"] == "partial"
    assert doc["amount_outstanding"] == 600.0
    assert doc["amount_paid"] == 400.0
    assert doc.get("payments", []) == []


@pytest.mark.asyncio
async def test_import_with_explicit_payment_event_works(client):
    """Option A: explicit doc.payment.received after import creates payment + JE."""
    token = await _register(client)
    entity_id = f"doc:optA-{uuid.uuid4().hex[:8]}"

    # Import as 'final' (revenue JE created, no payment)
    await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id,
        "event_type": "doc.created",
        "data": {
            "doc_type": "invoice", "total": 200.0, "subtotal": 200.0, "tax": 0.0,
            "status": "final", "amount_paid": 0.0, "amount_outstanding": 200.0,
            "issue_date": "2026-01-10",
        },
        "source": "import:test",
        "idempotency_key": f"idem-optA-create-{uuid.uuid4().hex}",
    })

    # Explicit payment event
    r = await client.post(f"/docs/{entity_id}/payment", headers=_h(token),
                          json={"payment_date": "2026-01-15", "amount": 200.0, "bank_account": "1111"})
    assert r.status_code == 200

    doc = (await client.get(f"/docs/{entity_id}", headers=_h(token))).json()
    assert doc["status"] == "paid"
    assert len(doc.get("payments", [])) == 1
    assert doc["payments"][0]["bank_account"] == "1111"
    assert doc["payments"][0]["payment_date"] == "2026-01-15"


@pytest.mark.asyncio
async def test_import_auto_je_no_payment_synthesis(client):
    """Unit: _import_auto_je must never call create_for_doc_payment - verified via HTTP import."""
    token = await _register(client)
    entity_id = f"doc:unit-{uuid.uuid4().hex[:8]}"

    with patch("celerp_docs.routes.auto_je.create_for_doc_payment", new_callable=AsyncMock) as mock_pay:
        r = await client.post("/docs/import", headers=_h(token), json={
            "entity_id": entity_id,
            "event_type": "doc.created",
            "data": {
                "doc_type": "invoice", "total": 100.0, "subtotal": 100.0,
                "status": "paid", "amount_paid": 100.0, "issue_date": "2026-01-01",
            },
            "source": "import:test",
            "idempotency_key": f"idem-unit-{uuid.uuid4().hex}",
        })
        assert r.status_code == 200
        mock_pay.assert_not_called()

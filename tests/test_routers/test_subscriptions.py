# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_subscription_generate_creates_invoice_with_lines_and_total(client):
    reg = await client.post(
        "/auth/register",
        json={"company_name": "Acme Inc", "email": "a@b.com", "name": "Admin", "password": "pw"},
    )
    token = reg.json()["access_token"]

    # Token embeds company_id; fetch it by listing my-companies.
    companies = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token}"})
    assert companies.status_code == 200, companies.text
    company_id = companies.json()["items"][0]["company_id"]
    headers = {"Authorization": f"Bearer {token}", "X-Company-Id": company_id}

    # Create subscription with a single line item.
    sub = await client.post(
        "/subscriptions",
        headers=headers,
        json={
            "name": "Monthly retainer",
            "contact_id": "contact:test",
            "doc_type": "invoice",
            "frequency": "monthly",
            "start_date": "2026-01-01",
            "line_items": [{"description": "Service", "quantity": 2, "unit_price": 1000}],
            "shipping": 0,
            "discount": 0,
            "tax": 0,
        },
    )
    assert sub.status_code == 200, sub.text
    sub_id = sub.json()["id"]

    gen = await client.post(f"/subscriptions/{sub_id}/generate", headers=headers)
    assert gen.status_code == 200, gen.text
    doc_id = gen.json()["doc_id"]

    doc = await client.get(f"/docs/{doc_id}", headers=headers)
    assert doc.status_code == 200, doc.text
    body = doc.json()

    assert body.get("doc_type") == "invoice"
    assert body.get("status") == "draft"

    assert body.get("line_items")
    assert body["line_items"][0]["description"] == "Service"
    assert body["line_items"][0]["quantity"] == 2
    assert body["line_items"][0]["unit_price"] == 1000

    assert body.get("total") == 2000
    assert body.get("amount_outstanding") == 2000


# ---------------------------------------------------------------------------
# Additional Phase 1 tests
# ---------------------------------------------------------------------------

async def _reg_sub(client, company="SubTestCo"):
    r = await client.post("/auth/register", json={"company_name": company, "email": f"{company.lower()}@test.com", "name": "Admin", "password": "pw"})
    tok = r.json()["access_token"]
    cid = (await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {tok}"})).json()["items"][0]["company_id"]
    return tok, {"Authorization": f"Bearer {tok}", "X-Company-Id": cid}


@pytest.mark.asyncio
async def test_contact_id_required(client):
    """POST /subscriptions without contact_id must return 422."""
    _, h = await _reg_sub(client, "ContactReqCo")
    r = await client.post("/subscriptions", headers=h, json={
        "name": "No Contact Sub", "doc_type": "invoice", "frequency": "monthly", "start_date": "2026-01-01",
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_contact_id_empty_string_rejected(client):
    """POST /subscriptions with contact_id='' must return 422."""
    _, h = await _reg_sub(client, "EmptyContactCo")
    r = await client.post("/subscriptions", headers=h, json={
        "name": "Empty Contact Sub", "doc_type": "invoice", "frequency": "monthly",
        "start_date": "2026-01-01", "contact_id": "",
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_cancel_subscription(client):
    """POST /cancel sets status to cancelled."""
    tok, h = await _reg_sub(client, "CancelCo")
    sid = (await client.post("/subscriptions", headers=h, json={
        "name": "To Cancel", "doc_type": "invoice", "frequency": "monthly",
        "start_date": "2026-01-01", "contact_id": "contact:c1",
    })).json()["id"]
    r = await client.post(f"/subscriptions/{sid}/cancel", headers=h)
    assert r.status_code == 200
    sub = (await client.get(f"/subscriptions/{sid}", headers=h)).json()
    assert sub["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_cancelled_is_409(client):
    """POST /cancel on already-cancelled subscription must return 409."""
    tok, h = await _reg_sub(client, "CancelTwiceCo")
    sid = (await client.post("/subscriptions", headers=h, json={
        "name": "Cancel Twice", "doc_type": "invoice", "frequency": "monthly",
        "start_date": "2026-01-01", "contact_id": "contact:c1",
    })).json()["id"]
    await client.post(f"/subscriptions/{sid}/cancel", headers=h)
    r = await client.post(f"/subscriptions/{sid}/cancel", headers=h)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_generated_doc_ids_is_list(client):
    """Generating twice accumulates doc IDs in generated_doc_ids list."""
    tok, h = await _reg_sub(client, "GenListCo")
    sid = (await client.post("/subscriptions", headers=h, json={
        "name": "Multi Gen", "doc_type": "invoice", "frequency": "monthly",
        "start_date": "2026-01-01", "contact_id": "contact:c1",
        "line_items": [{"description": "Fee", "quantity": 1, "unit_price": 50}],
    })).json()["id"]
    doc1 = (await client.post(f"/subscriptions/{sid}/generate", headers=h)).json()["doc_id"]
    doc2 = (await client.post(f"/subscriptions/{sid}/generate", headers=h)).json()["doc_id"]
    sub = (await client.get(f"/subscriptions/{sid}", headers=h)).json()
    doc_ids = sub.get("generated_doc_ids", [])
    assert doc1 in doc_ids
    assert doc2 in doc_ids
    assert len(doc_ids) == 2
    assert "last_generated_doc_id" not in sub


@pytest.mark.asyncio
async def test_generate_total_from_line_items(client):
    """generate_now must compute total from line items, not stored flat floats."""
    tok, h = await _reg_sub(client, "GenTotalCo")
    sid = (await client.post("/subscriptions", headers=h, json={
        "name": "Computed Total", "doc_type": "invoice", "frequency": "monthly",
        "start_date": "2026-01-01", "contact_id": "contact:c1",
        "line_items": [{"description": "Widget", "quantity": 3, "unit_price": 50}],
        "shipping": 10, "discount": 5, "tax": 0,
    })).json()["id"]
    doc_id = (await client.post(f"/subscriptions/{sid}/generate", headers=h)).json()["doc_id"]
    doc = (await client.get(f"/docs/{doc_id}", headers=h)).json()
    # subtotal = 150, discount = 5, shipping = 10 => total = 155
    assert doc["total"] == 155.0
    assert doc["amount_outstanding"] == 155.0

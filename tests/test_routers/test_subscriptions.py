# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp-subscriptions module rebuild.

Subscription templates are docs with doc_type "subscription_invoice" or "subscription_po".
Lifecycle actions (pause/resume/cancel/generate) are at /subscriptions/<id>/<action>.
"""
from __future__ import annotations

import uuid

import pytest


async def _register(client, name: str | None = None) -> str:
    addr = f"sub-{uuid.uuid4().hex[:8]}@test.test"
    co = name or f"SubCo-{uuid.uuid4().hex[:6]}"
    r = await client.post("/auth/register", json={"company_name": co, "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_sub_template(client, headers, doc_type="subscription_invoice", **kwargs):
    data = {
        "doc_type": doc_type,
        "contact_id": "contact:test-001",
        "frequency": "monthly",
        "start_date": "2026-01-01",
        "status": "active",
        "next_run_date": "2026-02-01",
        "line_items": [{"description": "Monthly Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        **kwargs,
    }
    r = await client.post("/docs", json=data, headers=headers)
    assert r.status_code in {200, 201}, f"Failed to create template: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# 1. Create subscription template
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_subscription_template(client):
    tok = await _register(client)
    h = _h(tok)
    data = {
        "doc_type": "subscription_invoice",
        "contact_id": "contact:test-001",
        "frequency": "monthly",
        "start_date": "2026-01-01",
        "status": "active",
        "next_run_date": "2026-02-01",
        "line_items": [{"description": "Monthly Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
    }
    r = await client.post("/docs", json=data, headers=h)
    assert r.status_code in {200, 201}, r.text
    body = r.json()
    eid = body.get("entity_id") or body.get("id") or ""
    assert eid


# ---------------------------------------------------------------------------
# 2. Subscription template stored as doc
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_template_stored_as_doc(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""
    r = await client.get(f"/docs/{eid}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body.get("doc_type") == "subscription_invoice"
    assert body.get("frequency") == "monthly"
    assert body.get("status") == "active"


# ---------------------------------------------------------------------------
# 3. List subscription templates - sales direction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_subscription_templates(client):
    tok = await _register(client)
    h = _h(tok)
    # Create one sales template
    await _create_sub_template(client, h, doc_type="subscription_invoice")
    # Create one purchasing template
    await _create_sub_template(client, h, doc_type="subscription_po")
    r = await client.get("/subscriptions?direction=sales", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(i.get("doc_type") == "subscription_invoice" for i in items)


# ---------------------------------------------------------------------------
# 4. List subscription templates - purchasing direction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_subscription_templates_purchasing(client):
    tok = await _register(client)
    h = _h(tok)
    await _create_sub_template(client, h, doc_type="subscription_invoice")
    await _create_sub_template(client, h, doc_type="subscription_po")
    r = await client.get("/subscriptions?direction=purchasing", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(i.get("doc_type") == "subscription_po" for i in items)


# ---------------------------------------------------------------------------
# 5. Status filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_status_filter(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    # Should appear in active filter
    r_active = await client.get("/subscriptions?status=active", headers=h)
    assert r_active.status_code == 200
    ids_active = [i.get("id") for i in r_active.json()["items"]]
    assert eid in ids_active

    # Pause it
    await client.post(f"/subscriptions/{eid}/pause", headers=h)

    r_paused = await client.get("/subscriptions?status=paused", headers=h)
    assert r_paused.status_code == 200
    ids_paused = [i.get("id") for i in r_paused.json()["items"]]
    assert eid in ids_paused


# ---------------------------------------------------------------------------
# 6. Generate now creates invoice
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_now_creates_invoice(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h, name="My Sub Template")
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    doc_id = body.get("doc_id", "")
    assert doc_id.startswith("doc:")

    # Verify generated doc
    dr = await client.get(f"/docs/{doc_id}", headers=h)
    assert dr.status_code == 200
    doc = dr.json()
    assert doc.get("doc_type") == "invoice"
    assert doc.get("subscription_id") == eid
    assert "My Sub Template" in doc.get("notes", "")


# ---------------------------------------------------------------------------
# 7. Generate now computes totals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_now_computes_totals(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(
        client, h,
        line_items=[
            {"description": "Item A", "quantity": 2, "unit_price": 50.0, "line_total": 100.0},
            {"description": "Item B", "quantity": 1, "unit_price": 30.0, "line_total": 30.0},
        ],
        discount=10.0,
        shipping=5.0,
    )
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200, r.text
    doc_id = r.json()["doc_id"]

    dr = await client.get(f"/docs/{doc_id}", headers=h)
    assert dr.status_code == 200
    doc = dr.json()
    # subtotal = 130, discount = 10, shipping = 5, tax = 0 -> total = 125
    assert float(doc.get("subtotal", 0)) == pytest.approx(130.0)
    assert float(doc.get("total", 0)) == pytest.approx(125.0)


# ---------------------------------------------------------------------------
# 8. Pause subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_subscription(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/pause", headers=h)
    assert r.status_code == 200

    dr = await client.get(f"/docs/{eid}", headers=h)
    assert dr.json().get("status") == "paused"


# ---------------------------------------------------------------------------
# 9. Resume subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_subscription(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    await client.post(f"/subscriptions/{eid}/pause", headers=h)
    r = await client.post(f"/subscriptions/{eid}/resume", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("next_run_date")

    dr = await client.get(f"/docs/{eid}", headers=h)
    assert dr.json().get("status") == "active"


# ---------------------------------------------------------------------------
# 10. Cancel subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_subscription(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/cancel", headers=h)
    assert r.status_code == 200

    dr = await client.get(f"/docs/{eid}", headers=h)
    assert dr.json().get("status") == "cancelled"


# ---------------------------------------------------------------------------
# 11. Cancel already-cancelled is 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_already_cancelled_is_409(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    await client.post(f"/subscriptions/{eid}/cancel", headers=h)
    r2 = await client.post(f"/subscriptions/{eid}/cancel", headers=h)
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# 12. Pause already-paused or cancelled is 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_already_paused_is_409(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    await client.post(f"/subscriptions/{eid}/pause", headers=h)
    # Double-pause
    r2 = await client.post(f"/subscriptions/{eid}/pause", headers=h)
    assert r2.status_code == 409

    # Cancel then pause
    await client.post(f"/subscriptions/{eid}/cancel", headers=h)
    r3 = await client.post(f"/subscriptions/{eid}/pause", headers=h)
    assert r3.status_code == 409


# ---------------------------------------------------------------------------
# 13. Contact ID is preserved when set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_contact_id_preserved(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h, contact_id="contact:my-specific-contact")
    eid = sub.get("entity_id") or sub.get("id") or ""

    dr = await client.get(f"/docs/{eid}", headers=h)
    assert dr.status_code == 200
    assert dr.json().get("contact_id") == "contact:my-specific-contact"


# ---------------------------------------------------------------------------
# 14. Custom frequency uses 30-day default when no interval given
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_custom_frequency_default_interval(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h, frequency="custom")
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200
    # 30-day default from today - just verify next_run_date is returned and not empty
    assert r.json().get("next_run_date")


# ---------------------------------------------------------------------------
# 15. Generated invoice appears in normal invoice list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generated_invoice_appears_in_normal_invoice_list(client):
    tok = await _register(client)
    h = _h(tok)
    sub = await _create_sub_template(client, h)
    eid = sub.get("entity_id") or sub.get("id") or ""

    r = await client.post(f"/subscriptions/{eid}/generate", headers=h)
    assert r.status_code == 200
    doc_id = r.json()["doc_id"]

    # Check via GET /docs?doc_type=invoice
    r2 = await client.get("/docs?doc_type=invoice", headers=h)
    assert r2.status_code == 200
    ids = [d.get("entity_id") or d.get("id") for d in r2.json().get("items", [])]
    assert doc_id in ids

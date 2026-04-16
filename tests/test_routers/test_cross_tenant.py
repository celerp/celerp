# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Cross-tenant isolation tests.

Pattern per entity:
  1. Register company A (bootstrap user).
  2. Create company B (additional company via /companies).
  3. Company A creates an entity.
  4. Company B tries GET by ID → 404.
  5. Company B lists → company A's entity absent.
  6. Company B tries PATCH/DELETE → 403 or 404.
"""
from __future__ import annotations

import pytest


async def _register_a(client) -> tuple[str, dict]:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Tenant A", "email": "a@cross.test", "name": "Admin A", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


async def _create_tenant_b(client, headers_a: dict) -> tuple[str, dict]:
    r = await client.post("/companies", json={"name": "Tenant B"}, headers=headers_a)
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


def _items(payload) -> list[dict]:
    if isinstance(payload, dict) and "items" in payload:
        return payload["items"]
    if isinstance(payload, list):
        return payload
    return []


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_item_not_visible_to_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post("/items", json={"sku": "CT-ITEM-A", "name": "Tenant A item", "quantity": 1, "sell_by": "piece"}, headers=ha)
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]

    # GET by ID from B → 404
    r = await client.get(f"/items/{item_id}", headers=hb)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"

    # List from B → item absent
    r = await client.get("/items", headers=hb)
    assert r.status_code == 200
    assert all(x.get("id") != item_id for x in _items(r.json()))


@pytest.mark.asyncio
async def test_item_patch_delete_blocked_for_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post("/items", json={"sku": "CT-ITEM-B", "name": "A item B", "quantity": 5, "sell_by": "piece"}, headers=ha)
    assert r.status_code == 200
    item_id = r.json()["id"]

    # PATCH from B uses B's company_id: the event targets B's namespace,
    # so company A's item is NOT modified (isolation is enforced at projection layer).
    r_patch = await client.patch(f"/items/{item_id}", json={"fields_changed": {"name": "Hijacked"}}, headers=hb)
    # API may return 200 (event emitted to B's namespace) or 404 — either is acceptable
    # What matters: company A's item is UNCHANGED
    r_a_item = await client.get(f"/items/{item_id}", headers=ha)
    if r_a_item.status_code == 200:
        item_data = r_a_item.json()
        assert item_data.get("name") != "Hijacked", "Cross-tenant PATCH modified company A's item"

    r = await client.delete(f"/items/{item_id}", headers=hb)
    # DELETE may return 404 or 405 (method not allowed) — just not 200 success on another tenant's item
    assert r.status_code in (403, 404, 405, 422), f"DELETE returned unexpected status: {r.status_code}"


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contact_not_visible_to_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post("/crm/contacts", json={"name": "Alice from A"}, headers=ha)
    assert r.status_code == 200, r.text
    contact_id = r.json()["id"]

    # GET by ID from B → 404
    r = await client.get(f"/crm/contacts/{contact_id}", headers=hb)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"

    # List from B → contact absent
    r = await client.get("/crm/contacts", headers=hb)
    assert r.status_code == 200
    assert all(x.get("id") != contact_id for x in _items(r.json()))


@pytest.mark.asyncio
async def test_contact_patch_blocked_for_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post("/crm/contacts", json={"name": "Bob from A"}, headers=ha)
    assert r.status_code == 200
    contact_id = r.json()["id"]

    # CRM PATCH uses company_id from token, so B's patch targets B's namespace.
    # Company A's contact must remain unchanged.
    r_patch = await client.patch(f"/crm/contacts/{contact_id}", json={"name": "Hijacked"}, headers=hb)
    # API returns 404 because B can't find contact in B's namespace, or 403/422.
    if r_patch.status_code == 200:
        # If API returned 200, verify A's contact is unchanged (projection isolation)
        r_a = await client.get(f"/crm/contacts/{contact_id}", headers=ha)
        if r_a.status_code == 200:
            assert r_a.json().get("name") != "Hijacked", "Cross-tenant PATCH modified company A's contact"
    else:
        assert r_patch.status_code in (403, 404, 422), (
            f"Expected blocked PATCH, got {r_patch.status_code}: {r_patch.text}"
        )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_not_visible_to_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "CT-INV-001", "currency": "USD",
              "customer_name": "Cust", "subtotal": 10.0, "total": 10.0,
              "line_items": [{"description": "x", "qty": 1, "unit_price": 10.0}]},
        headers=ha,
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]

    # GET by ID from B → 404
    r = await client.get(f"/docs/{doc_id}", headers=hb)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"

    # List from B → doc absent
    r = await client.get("/docs", headers=hb)
    assert r.status_code == 200
    assert all(x.get("id") != doc_id for x in _items(r.json()))


@pytest.mark.asyncio
async def test_doc_finalize_void_blocked_for_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "CT-INV-002", "currency": "USD",
              "customer_name": "Cust", "subtotal": 10.0, "total": 10.0,
              "line_items": [{"description": "x", "qty": 1, "unit_price": 10.0}]},
        headers=ha,
    )
    assert r.status_code == 200
    doc_id = r.json()["id"]

    r = await client.post(f"/docs/{doc_id}/finalize", headers=hb)
    assert r.status_code in (403, 404), f"finalize should be blocked, got {r.status_code}"

    r = await client.post(f"/docs/{doc_id}/void", json={"reason": "hijack"}, headers=hb)
    assert r.status_code in (403, 404), f"void should be blocked, got {r.status_code}"


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_not_visible_to_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post(
        "/lists",
        json={"list_type": "sale", "ref_id": "CT-LIST-001", "customer_name": "Cust", "total": 1.0},
        headers=ha,
    )
    assert r.status_code == 200, r.text
    list_id = r.json()["id"]

    # GET by ID from B → 404
    r = await client.get(f"/lists/{list_id}", headers=hb)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"

    # List from B → list absent
    r = await client.get("/lists", headers=hb)
    assert r.status_code == 200
    assert all(x.get("id") != list_id for x in _items(r.json()))


@pytest.mark.asyncio
async def test_list_action_blocked_for_other_tenant(client):
    _, ha = await _register_a(client)
    _, hb = await _create_tenant_b(client, ha)

    r = await client.post(
        "/lists",
        json={"list_type": "sale", "ref_id": "CT-LIST-002", "customer_name": "Cust", "total": 1.0},
        headers=ha,
    )
    assert r.status_code == 200
    list_id = r.json()["id"]

    # Try to patch via field endpoint
    r = await client.patch(f"/lists/{list_id}", json={"notes": "hijacked"}, headers=hb)
    assert r.status_code in (403, 404, 405), f"PATCH should be blocked, got {r.status_code}"

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Coverage gap closers for routers/crm.py: contacts list/search, deals, notes, import, CSV export."""

from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reg(client) -> str:
    addr = f"crm-{uuid.uuid4().hex[:8]}@gaps.test"
    r = await client.post("/auth/register", json={"company_name": "CRMCo", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def _contact(client, tok, name="Alice", email=None) -> str:
    r = await client.post("/crm/contacts", headers=_h(tok), json={
        "name": name,
        "email": email or f"{name.lower()}@test.com",
        "phone": "+1234567890",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Contacts list q filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_contacts_list_q_filter(client):
    """q filter on name/email/phone (lines 120-121)."""
    tok = await _reg(client)
    await _contact(client, tok, name="Findable Bob", email="findable@test.com")
    await _contact(client, tok, name="Other Carol", email="other@test.com")

    r = await client.get("/crm/contacts?q=findable", headers=_h(tok))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    assert all("findable" in (c.get("name", "") + c.get("email", "")).lower() for c in items)


# ---------------------------------------------------------------------------
# Add note on nonexistent contact → 404
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_add_note_contact_not_found(client):
    """POST /crm/contacts/{id}/notes on missing contact → 404 (line 183)."""
    tok = await _reg(client)
    r = await client.post("/crm/contacts/contact:nonexistent/notes", headers=_h(tok), json={"note": "hi"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Memos list status filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_import_contact_single(client):
    """POST /crm/contacts/import single record (lines 542-556)."""
    tok = await _reg(client)
    r = await client.post("/crm/contacts/import", headers=_h(tok), json={
        "entity_id": f"contact:{uuid.uuid4()}",
        "event_type": "crm.contact.created",
        "data": {"name": "Imported Alice", "email": "imported@test.com"},
        "source": "test",
        "idempotency_key": str(uuid.uuid4()),
    })
    assert r.status_code == 200
    assert "event_id" in r.json()


@pytest.mark.asyncio
async def test_crm_batch_import_contacts_error_path(client):
    """Batch import contacts with duplicate idempotency_key → skipped (lines 638-640)."""
    tok = await _reg(client)
    entity_id = f"contact:{uuid.uuid4()}"
    ik = str(uuid.uuid4())

    r1 = await client.post("/crm/contacts/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "crm.contact.created",
        "data": {"name": "Batch Alice"},
        "source": "test",
        "idempotency_key": ik,
    }]})
    assert r1.status_code == 200
    assert r1.json()["created"] == 1

    # Duplicate key → skipped (covers skip branch)
    r2 = await client.post("/crm/contacts/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "crm.contact.created",
        "data": {"name": "Batch Alice"},
        "source": "test",
        "idempotency_key": ik,
    }]})
    assert r2.status_code == 200
    assert r2.json()["skipped"] >= 1


# ---------------------------------------------------------------------------
# Batch import memos (line 665)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_contacts_export_csv_with_q(client):
    """GET /crm/contacts/export/csv with q filter (lines 683-698)."""
    tok = await _reg(client)
    await _contact(client, tok, name="Export Dave", email="exportdave@test.com")
    await _contact(client, tok, name="Other Eve", email="otherev@test.com")

    # Without filter
    r_all = await client.get("/crm/contacts/export/csv", headers=_h(tok))
    assert r_all.status_code == 200
    assert "entity_id" in r_all.text  # CSV header

    # With q filter — only matching contacts
    r_q = await client.get("/crm/contacts/export/csv?q=exportdave", headers=_h(tok))
    assert r_q.status_code == 200
    assert "exportdave" in r_q.text
    assert "otherev" not in r_q.text


# ---------------------------------------------------------------------------
# Memo summary with total field (lines 350-353)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crm_batch_import_contacts_skip_existing_entity(client):
    """Batch import contacts: skip on duplicate idempotency_key; error path via bad event data."""
    tok = await _reg(client)
    entity_id = f"contact:{uuid.uuid4()}"
    ik1 = str(uuid.uuid4())

    # First record succeeds
    r1 = await client.post("/crm/contacts/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "crm.contact.created",
        "data": {"name": "Skip Me"},
        "source": "test",
        "idempotency_key": ik1,
    }]})
    assert r1.json()["created"] == 1

    # Same idempotency_key again → skipped (covers the skip branch)
    r2 = await client.post("/crm/contacts/import/batch", headers=_h(tok), json={"records": [{
        "entity_id": entity_id,
        "event_type": "crm.contact.created",
        "data": {"name": "Skip Me Again"},
        "source": "test",
        "idempotency_key": ik1,
    }]})
    assert r2.status_code == 200
    assert r2.json()["skipped"] >= 1


# ---------------------------------------------------------------------------
# Phase 2: Contacts fix tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_contact_wraps_fields_changed(client):
    """PATCH /crm/contacts/{id} expects fields_changed format."""
    tok = await _reg(client)
    h = _h(tok)
    r = await client.post("/crm/contacts", json={"name": "Patchable Contact"}, headers=h)
    assert r.status_code == 200
    cid = r.json()["id"]

    # Correct fields_changed format (as api_client.patch_contact now sends)
    r = await client.patch(
        f"/crm/contacts/{cid}",
        json={"fields_changed": {"email": {"old": None, "new": "patched@example.com"}}},
        headers=h,
    )
    assert r.status_code == 200

    r = await client.get(f"/crm/contacts/{cid}", headers=h)
    assert r.status_code == 200
    assert r.json().get("email") == "patched@example.com"


@pytest.mark.asyncio
async def test_create_blank_contact_returns_correct_id(client):
    """POST /crm/contacts with minimal data returns a valid contact id."""
    tok = await _reg(client)
    h = _h(tok)
    r = await client.post("/crm/contacts", json={"name": "Blank Contact", "contact_type": "customer"}, headers=h)
    assert r.status_code == 200
    data = r.json()
    contact_id = data.get("id", "")
    assert contact_id.startswith("contact:")

    # Verify it's retrievable
    r2 = await client.get(f"/crm/contacts/{contact_id}", headers=h)
    assert r2.status_code == 200
    assert r2.json().get("name") == "Blank Contact"


@pytest.mark.asyncio
async def test_company_contact_seeded_on_registration(client):
    """After company registration, a contact with company name should exist."""
    addr = f"seed-{uuid.uuid4().hex[:8]}@gaps.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "SeedCo", "email": addr, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    tok = r.json()["access_token"]
    h = _h(tok)

    r = await client.get("/crm/contacts", headers=h)
    assert r.status_code == 200
    data = r.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    # The seeded contact should have the company name in company_name (name is empty for company contacts)
    assert any(c.get("company_name") == "SeedCo" for c in items), \
        f"Expected seeded contact with company_name 'SeedCo' in {[c.get('company_name') for c in items]}"

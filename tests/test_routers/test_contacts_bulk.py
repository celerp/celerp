# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for contacts bulk actions:
  - POST /crm/contacts/bulk/delete
  - GET  /crm/contacts/export/csv  (with selected= filter)
  - POST /crm/contacts/merge

Covers: happy path, validation guards, edge cases (already-merged source,
deleted target, open-doc blocker, currency mismatch, deduplication).
"""

from __future__ import annotations

import uuid
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client, name=None) -> str:
    name = name or f"BulkCo-{uuid.uuid4().hex[:6]}"
    r = await client.post("/auth/register", json={
        "company_name": name,
        "email": f"bulk-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin",
        "password": "pw",
    })
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _contact(client, tok, **kw) -> str:
    r = await client.post("/crm/contacts", headers=_h(tok), json={
        "name": f"Contact {uuid.uuid4().hex[:6]}",
        "contact_type": "customer",
        **kw,
    })
    assert r.status_code == 200
    return r.json()["id"]


async def _invoice(client, tok, contact_id: str) -> str:
    """Create a finalized invoice linked to a contact so it blocks deletion."""
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice",
        "contact_id": contact_id,
        "contact_name": "Test",
        "total": 100,
        "issue_date": "2026-01-01",
        "due_date": "2026-02-01",
        "currency": "USD",
    })
    assert r.status_code == 200
    doc_id = r.json()["id"]
    await client.post(f"/docs/{doc_id}/send", headers=_h(tok), json={})
    r2 = await client.post(f"/docs/{doc_id}/finalize", headers=_h(tok))
    assert r2.status_code == 200
    return doc_id


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bulk_delete_contacts_happy(client):
    tok = await _register(client, "BulkDel1")
    c1 = await _contact(client, tok)
    c2 = await _contact(client, tok)

    r = await client.post("/crm/contacts/bulk/delete", headers=_h(tok), json={"contact_ids": [c1, c2]})
    assert r.status_code == 200
    data = r.json()
    assert data["deleted"] == 2

    # Both contacts should be marked deleted in their state
    r_c1 = await client.get(f"/crm/contacts/{c1}", headers=_h(tok))
    r_c2 = await client.get(f"/crm/contacts/{c2}", headers=_h(tok))
    assert r_c1.json().get("deleted") is True
    assert r_c2.json().get("deleted") is True


@pytest.mark.asyncio
async def test_bulk_delete_contacts_empty_list(client):
    tok = await _register(client, "BulkDel2")
    r = await client.post("/crm/contacts/bulk/delete", headers=_h(tok), json={"contact_ids": []})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_bulk_delete_contacts_not_found(client):
    tok = await _register(client, "BulkDel3")
    r = await client.post("/crm/contacts/bulk/delete", headers=_h(tok), json={"contact_ids": ["nonexistent-id"]})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bulk_delete_contacts_blocked_by_open_doc(client):
    tok = await _register(client, "BulkDel4")
    c1 = await _contact(client, tok)
    await _invoice(client, tok, c1)  # creates a finalized invoice - blocks deletion

    r = await client.post("/crm/contacts/bulk/delete", headers=_h(tok), json={"contact_ids": [c1]})
    assert r.status_code == 422
    assert "open document" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_bulk_delete_contacts_draft_doc_does_not_block(client):
    tok = await _register(client, "BulkDel5")
    c1 = await _contact(client, tok)
    # Create a draft invoice (not finalized) - should NOT block deletion
    r = await client.post("/docs", headers=_h(tok), json={
        "doc_type": "invoice",
        "contact_id": c1,
        "contact_name": "Test",
        "total": 50,
        "issue_date": "2026-01-01",
        "currency": "USD",
    })
    assert r.status_code == 200

    r2 = await client.post("/crm/contacts/bulk/delete", headers=_h(tok), json={"contact_ids": [c1]})
    assert r2.status_code == 200
    assert r2.json()["deleted"] == 1


# ---------------------------------------------------------------------------
# Export CSV with selected= filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_contacts_csv_all(client):
    tok = await _register(client, "Export1")
    c1 = await _contact(client, tok, name="Alice Export")
    c2 = await _contact(client, tok, name="Bob Export")

    r = await client.get("/crm/contacts/export/csv", headers=_h(tok))
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    body = r.text
    assert "Alice Export" in body
    assert "Bob Export" in body


@pytest.mark.asyncio
async def test_export_contacts_csv_selected_filter(client):
    tok = await _register(client, "Export2")
    c1 = await _contact(client, tok, name="Charlie Export")
    c2 = await _contact(client, tok, name="Delta Export")

    r = await client.get(f"/crm/contacts/export/csv?selected={c1}", headers=_h(tok))
    assert r.status_code == 200
    body = r.text
    assert "Charlie Export" in body
    assert "Delta Export" not in body


# ---------------------------------------------------------------------------
# Merge contacts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_contacts_happy(client):
    tok = await _register(client, "Merge1")
    target = await _contact(client, tok, name="Winner Co")
    src1 = await _contact(client, tok, name="Loser A")
    src2 = await _contact(client, tok, name="Loser B")

    r = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": target,
        "source_contact_ids": [src1, src2],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["merged_into"] == target
    assert data["sources_merged"] == 2

    # Sources should be tombstoned
    r2 = await client.get(f"/crm/contacts/{src1}", headers=_h(tok))
    src1_state = r2.json()
    assert src1_state.get("deleted") or src1_state.get("merged_into") == target


@pytest.mark.asyncio
async def test_merge_contacts_repoints_docs(client):
    tok = await _register(client, "Merge2")
    target = await _contact(client, tok, name="Merge Target")
    source = await _contact(client, tok, name="Merge Source")

    # Create a finalized doc on the source contact
    doc_id = await _invoice(client, tok, source)

    r = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": target,
        "source_contact_ids": [source],
    })
    assert r.status_code == 200
    assert r.json()["docs_updated"] >= 1

    # Doc should now be re-pointed to target
    dr = await client.get(f"/docs/{doc_id}", headers=_h(tok))
    assert dr.json().get("contact_id") == target


@pytest.mark.asyncio
async def test_merge_contacts_target_cannot_be_in_sources(client):
    tok = await _register(client, "Merge3")
    c = await _contact(client, tok)
    r = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": c,
        "source_contact_ids": [c],
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_merge_contacts_empty_sources(client):
    tok = await _register(client, "Merge4")
    c = await _contact(client, tok)
    r = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": c,
        "source_contact_ids": [],
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_merge_contacts_deleted_target_rejected(client):
    tok = await _register(client, "Merge5")
    target = await _contact(client, tok)
    source = await _contact(client, tok)

    # Delete the target first
    await client.post("/crm/contacts/bulk/delete", headers=_h(tok), json={"contact_ids": [target]})

    r = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": target,
        "source_contact_ids": [source],
    })
    assert r.status_code == 422
    assert "deleted" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_merge_contacts_already_merged_source_rejected(client):
    tok = await _register(client, "Merge6")
    target1 = await _contact(client, tok, name="Target1")
    target2 = await _contact(client, tok, name="Target2")
    source = await _contact(client, tok, name="Source")

    # Merge source into target1 first
    r1 = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": target1,
        "source_contact_ids": [source],
    })
    assert r1.status_code == 200

    # Try to merge the already-merged source into target2 - should be rejected (deleted or merged)
    r2 = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": target2,
        "source_contact_ids": [source],
    })
    assert r2.status_code == 422
    # Source was tombstoned - either "already merged" or "already deleted" are acceptable
    detail = r2.json()["detail"].lower()
    assert "already merged" in detail or "already deleted" in detail or "deleted" in detail


@pytest.mark.asyncio
async def test_merge_contacts_deduplicates_people_by_email(client):
    tok = await _register(client, "Merge7")
    shared_email = f"shared-{uuid.uuid4().hex[:6]}@example.com"

    # Create target with a person
    r = await client.post("/crm/contacts", headers=_h(tok), json={
        "name": "Dedup Target",
        "contact_type": "customer",
    })
    target = r.json()["id"]
    await client.post(f"/crm/contacts/{target}/people", headers=_h(tok), json={
        "name": "Alice", "email": shared_email,
    })

    # Create source with a person sharing the same email
    r2 = await client.post("/crm/contacts", headers=_h(tok), json={
        "name": "Dedup Source",
        "contact_type": "customer",
    })
    source = r2.json()["id"]
    await client.post(f"/crm/contacts/{source}/people", headers=_h(tok), json={
        "name": "Alice Duplicate", "email": shared_email,
    })

    r3 = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": target,
        "source_contact_ids": [source],
    })
    assert r3.status_code == 200

    # Merged contact should only have 1 person with that email
    detail = await client.get(f"/crm/contacts/{target}", headers=_h(tok))
    people = detail.json().get("people", [])
    emails = [p.get("email", "").lower() for p in people]
    assert emails.count(shared_email.lower()) == 1


@pytest.mark.asyncio
async def test_merge_contacts_currency_mismatch_returns_warning(client):
    tok = await _register(client, "Merge8")

    r1 = await client.post("/crm/contacts", headers=_h(tok), json={
        "name": "USD Co", "contact_type": "customer", "currency": "USD",
    })
    target = r1.json()["id"]

    r2 = await client.post("/crm/contacts", headers=_h(tok), json={
        "name": "EUR Co", "contact_type": "customer", "currency": "EUR",
    })
    source = r2.json()["id"]

    r3 = await client.post("/crm/contacts/merge", headers=_h(tok), json={
        "target_contact_id": target,
        "source_contact_ids": [source],
    })
    # Should succeed (not reject), but include a warning
    assert r3.status_code == 200
    data = r3.json()
    assert len(data.get("warnings", [])) > 0
    assert any("currency" in w.lower() for w in data["warnings"])

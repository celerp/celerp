# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for POST /docs/{entity_id}/renumber."""

from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Renumber Co", "email": f"rn-{uuid.uuid4().hex[:8]}@test.test", "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_doc(client, token, doc_type="invoice", ref_id=None, status="draft") -> dict:
    payload: dict = {
        "doc_type": doc_type,
        "contact_name": "Test",
        "line_items": [{"description": "x", "quantity": 1, "unit_price": 100}],
        "subtotal": 100, "total": 100,
    }
    if ref_id:
        payload["ref_id"] = ref_id
    r = await client.post("/docs", headers=_h(token), json=payload)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    # Fetch full state (create returns minimal {event_id, id})
    r2 = await client.get(f"/docs/{doc_id}", headers=_h(token))
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _finalize(client, token, doc_id) -> None:
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(token))
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Basic happy paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renumber_draft_doc(client, session):
    """Renumbering a draft doc updates ref_id and doc_number in state."""
    token = await _register(client)
    doc = await _create_doc(client, token)
    doc_id = doc["id"]
    old_ref = doc["ref_id"]

    new_ref = f"CUSTOM-{uuid.uuid4().hex[:6].upper()}"
    r = await client.post(f"/docs/{doc_id}/renumber", headers=_h(token), json={"ref_id": new_ref})
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["ref_id"] == new_ref
    assert updated["doc_number"] == new_ref
    assert updated["id"] == doc_id  # entity_id unchanged


@pytest.mark.asyncio
async def test_renumber_finalized_invoice(client, session):
    """Renumbering works on finalized invoices - the primary use case."""
    token = await _register(client)
    doc = await _create_doc(client, token)
    await _finalize(client, token, doc["id"])

    # Fetch finalized state to get new ref_id (INV-...)
    r = await client.get(f"/docs/{doc['id']}", headers=_h(token))
    state = r.json()
    assert state["status"] == "final"
    doc_id = state["id"]

    new_ref = f"INV-CUSTOM-{uuid.uuid4().hex[:6].upper()}"
    r = await client.post(f"/docs/{doc_id}/renumber", headers=_h(token), json={"ref_id": new_ref})
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["ref_id"] == new_ref
    assert updated["doc_number"] == new_ref
    assert updated["status"] == "final"
    assert updated["id"] == doc_id  # entity_id still unchanged


@pytest.mark.asyncio
async def test_renumber_bill(client, session):
    """Renumbering works on bills."""
    token = await _register(client)
    doc = await _create_doc(client, token, doc_type="bill", ref_id=f"BILL-{uuid.uuid4().hex[:6].upper()}")
    doc_id = doc["id"]

    new_ref = f"VENDOR-INV-{uuid.uuid4().hex[:6].upper()}"
    r = await client.post(f"/docs/{doc_id}/renumber", headers=_h(token), json={"ref_id": new_ref})
    assert r.status_code == 200, r.text
    assert r.json()["ref_id"] == new_ref


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renumber_to_own_number_is_noop(client, session):
    """Renaming a doc to its own current ref_id returns 200 without emitting an event."""
    token = await _register(client)
    doc = await _create_doc(client, token, ref_id=f"NOOP-{uuid.uuid4().hex[:6].upper()}")
    doc_id = doc["id"]
    current_ref = doc["ref_id"]

    r = await client.post(f"/docs/{doc_id}/renumber", headers=_h(token), json={"ref_id": current_ref})
    assert r.status_code == 200, r.text
    assert r.json()["ref_id"] == current_ref


# ---------------------------------------------------------------------------
# Uniqueness enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renumber_conflict_with_existing_doc(client, session):
    """Renaming to a number already used by another doc is rejected 409."""
    token = await _register(client)
    ref_a = f"DOC-A-{uuid.uuid4().hex[:6].upper()}"
    ref_b = f"DOC-B-{uuid.uuid4().hex[:6].upper()}"
    doc_a = await _create_doc(client, token, ref_id=ref_a)
    doc_b = await _create_doc(client, token, ref_id=ref_b)

    # Try to rename B to A's number
    r = await client.post(f"/docs/{doc_b['id']}/renumber", headers=_h(token), json={"ref_id": ref_a})
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["detail"]


@pytest.mark.asyncio
async def test_renumber_chain_frees_old_number(client, session):
    """After A is renamed away, its old number can be taken by B."""
    token = await _register(client)
    ref_a = f"ORIG-{uuid.uuid4().hex[:6].upper()}"
    ref_b = f"TEMP-{uuid.uuid4().hex[:6].upper()}"
    doc_a = await _create_doc(client, token, ref_id=ref_a)
    doc_b = await _create_doc(client, token, ref_id=ref_b)

    # Rename A away from ref_a
    new_a = f"NEW-A-{uuid.uuid4().hex[:6].upper()}"
    r = await client.post(f"/docs/{doc_a['id']}/renumber", headers=_h(token), json={"ref_id": new_a})
    assert r.status_code == 200, r.text

    # Now B can take ref_a
    r = await client.post(f"/docs/{doc_b['id']}/renumber", headers=_h(token), json={"ref_id": ref_a})
    assert r.status_code == 200, r.text
    assert r.json()["ref_id"] == ref_a


# ---------------------------------------------------------------------------
# Voided docs blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renumber_voided_doc_blocked(client, session):
    """Voided documents cannot be renumbered - they are immutable audit records."""
    token = await _register(client)
    doc = await _create_doc(client, token)
    await _finalize(client, token, doc["id"])
    r = await client.post(f"/docs/{doc['id']}/void", headers=_h(token), json={"reason": "test"})
    assert r.status_code == 200

    r = await client.post(f"/docs/{doc['id']}/renumber", headers=_h(token), json={"ref_id": "NEW-NUM"})
    assert r.status_code == 409, r.text
    assert "Voided" in r.json()["detail"]


# ---------------------------------------------------------------------------
# entity_id immutability
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renumber_does_not_change_entity_id(client, session):
    """The internal entity_id (storage key) must never change after renumber."""
    token = await _register(client)
    ref = f"ORIG-EID-{uuid.uuid4().hex[:6].upper()}"
    doc = await _create_doc(client, token, ref_id=ref)
    original_id = doc["id"]

    new_ref = f"RENAMED-{uuid.uuid4().hex[:6].upper()}"
    r = await client.post(f"/docs/{original_id}/renumber", headers=_h(token), json={"ref_id": new_ref})
    assert r.status_code == 200
    # entity_id from doc state is still the original
    assert r.json()["id"] == original_id
    # And the doc is still retrievable by the original entity_id
    r2 = await client.get(f"/docs/{original_id}", headers=_h(token))
    assert r2.status_code == 200
    assert r2.json()["ref_id"] == new_ref


# ---------------------------------------------------------------------------
# patch_doc uniqueness regression (state scan, not entity_id lookup)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_doc_ref_id_uniqueness_uses_state_scan(client, session):
    """patch_doc ref_id uniqueness check must catch docs renamed away from their original entity_id."""
    token = await _register(client)
    ref_a = f"PATCH-A-{uuid.uuid4().hex[:6].upper()}"
    ref_b = f"PATCH-B-{uuid.uuid4().hex[:6].upper()}"
    doc_a = await _create_doc(client, token, ref_id=ref_a)
    doc_b = await _create_doc(client, token, ref_id=ref_b)

    # Rename B to a completely new ref; now ref_b is "free" in entity_id space but
    # still recorded in A's state if nothing changed - actually test: rename A to new_a
    new_a = f"PATCH-A-NEW-{uuid.uuid4().hex[:6].upper()}"
    r = await client.post(f"/docs/{doc_a['id']}/renumber", headers=_h(token), json={"ref_id": new_a})
    assert r.status_code == 200

    # Now try to patch B to use new_a via patch_doc - should be blocked
    r = await client.patch(
        f"/docs/{doc_b['id']}",
        headers=_h(token),
        json={"fields_changed": {"ref_id": {"old": ref_b, "new": new_a}}},
    )
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renumber_empty_ref_id_rejected(client, session):
    """Empty ref_id must be rejected with 422."""
    token = await _register(client)
    doc = await _create_doc(client, token)
    r = await client.post(f"/docs/{doc['id']}/renumber", headers=_h(token), json={"ref_id": "   "})
    assert r.status_code == 422, r.text

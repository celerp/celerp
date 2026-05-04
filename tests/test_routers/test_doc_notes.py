"""Tests for full-CRUD doc and list notes (Level 2 DRY notes parity with contacts).

Covers:
- POST /docs/{id}/notes - add note
- GET  /docs/{id}/notes - list notes
- PATCH /docs/{id}/notes/{note_id} - update
- DELETE /docs/{id}/notes/{note_id} - delete
- Same 4 for /lists/{id}/notes
- 404 on wrong doc_id / note_id
- Empty text rejected (422)
- Cross-doc note isolation (note of doc A not editable via doc B)
"""
from __future__ import annotations

import uuid

import pytest


async def _setup(client):
    """Register user and return (token, company_id)."""
    addr = f"notes-{uuid.uuid4().hex[:8]}@test.com"
    r = await client.post("/auth/register", json={"company_name": "Notes Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    companies = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token}"})
    company_id = companies.json()["items"][0]["company_id"]
    return token, company_id


def _h(token: str, company_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Company-ID": company_id}


async def _create_invoice(client, token, company_id):
    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "currency": "USD", "line_items": []},
        headers=_h(token, company_id),
    )
    assert r.status_code == 200
    return r.json()["id"]


async def _create_list(client, token, company_id):
    r = await client.post(
        "/lists",
        json={"list_type": "price_list", "currency": "USD", "line_items": []},
        headers=_h(token, company_id),
    )
    assert r.status_code == 200
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Doc notes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_notes_add_and_list(client):
    token, company_id = await _setup(client)
    doc_id = await _create_invoice(client, token, company_id)
    h = _h(token, company_id)

    # Initially empty
    r = await client.get(f"/docs/{doc_id}/notes", headers=h)
    assert r.status_code == 200
    assert r.json() == []

    # Add note
    r = await client.post(f"/docs/{doc_id}/notes", json={"note": "First note"}, headers=h)
    assert r.status_code == 200
    note_id = r.json()["id"]
    assert note_id.startswith("note:")

    # List returns it
    notes = (await client.get(f"/docs/{doc_id}/notes", headers=h)).json()
    assert len(notes) == 1
    assert notes[0]["note"] == "First note"
    assert notes[0]["id"] == note_id
    assert notes[0]["author_name"]  # should be populated


@pytest.mark.asyncio
async def test_doc_notes_newest_first(client):
    token, company_id = await _setup(client)
    doc_id = await _create_invoice(client, token, company_id)
    h = _h(token, company_id)

    await client.post(f"/docs/{doc_id}/notes", json={"note": "older"}, headers=h)
    await client.post(f"/docs/{doc_id}/notes", json={"note": "newer"}, headers=h)

    notes = (await client.get(f"/docs/{doc_id}/notes", headers=h)).json()
    assert len(notes) == 2
    assert notes[0]["note"] == "newer"
    assert notes[1]["note"] == "older"


@pytest.mark.asyncio
async def test_doc_notes_update(client):
    token, company_id = await _setup(client)
    doc_id = await _create_invoice(client, token, company_id)
    h = _h(token, company_id)

    note_id = (await client.post(f"/docs/{doc_id}/notes", json={"note": "original"}, headers=h)).json()["id"]

    r = await client.patch(f"/docs/{doc_id}/notes/{note_id}", json={"note": "updated"}, headers=h)
    assert r.status_code == 200

    notes = (await client.get(f"/docs/{doc_id}/notes", headers=h)).json()
    assert notes[0]["note"] == "updated"
    assert notes[0]["updated_at"] is not None


@pytest.mark.asyncio
async def test_doc_notes_delete(client):
    token, company_id = await _setup(client)
    doc_id = await _create_invoice(client, token, company_id)
    h = _h(token, company_id)

    note_id = (await client.post(f"/docs/{doc_id}/notes", json={"note": "to delete"}, headers=h)).json()["id"]

    r = await client.delete(f"/docs/{doc_id}/notes/{note_id}", headers=h)
    assert r.status_code == 200

    assert (await client.get(f"/docs/{doc_id}/notes", headers=h)).json() == []


@pytest.mark.asyncio
async def test_doc_notes_empty_rejected(client):
    token, company_id = await _setup(client)
    doc_id = await _create_invoice(client, token, company_id)
    h = _h(token, company_id)

    r = await client.post(f"/docs/{doc_id}/notes", json={"note": "   "}, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_doc_notes_update_wrong_id_404(client):
    token, company_id = await _setup(client)
    doc_id = await _create_invoice(client, token, company_id)
    h = _h(token, company_id)

    r = await client.patch(f"/docs/{doc_id}/notes/note:nonexistent", json={"note": "x"}, headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_doc_notes_delete_wrong_id_404(client):
    token, company_id = await _setup(client)
    doc_id = await _create_invoice(client, token, company_id)
    h = _h(token, company_id)

    r = await client.delete(f"/docs/{doc_id}/notes/note:nonexistent", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_doc_notes_cross_doc_isolation(client):
    """Note belonging to doc A cannot be edited/deleted via doc B."""
    token, company_id = await _setup(client)
    doc_a = await _create_invoice(client, token, company_id)
    doc_b = await _create_invoice(client, token, company_id)
    h = _h(token, company_id)

    note_id = (await client.post(f"/docs/{doc_a}/notes", json={"note": "doc A note"}, headers=h)).json()["id"]

    assert (await client.patch(f"/docs/{doc_b}/notes/{note_id}", json={"note": "hacked"}, headers=h)).status_code == 404
    assert (await client.delete(f"/docs/{doc_b}/notes/{note_id}", headers=h)).status_code == 404


# ---------------------------------------------------------------------------
# List notes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_notes_add_and_list(client):
    token, company_id = await _setup(client)
    list_id = await _create_list(client, token, company_id)
    h = _h(token, company_id)

    r = await client.get(f"/lists/{list_id}/notes", headers=h)
    assert r.status_code == 200
    assert r.json() == []

    r = await client.post(f"/lists/{list_id}/notes", json={"note": "list note"}, headers=h)
    assert r.status_code == 200
    note_id = r.json()["id"]

    notes = (await client.get(f"/lists/{list_id}/notes", headers=h)).json()
    assert len(notes) == 1
    assert notes[0]["note"] == "list note"
    assert notes[0]["id"] == note_id


@pytest.mark.asyncio
async def test_list_notes_update(client):
    token, company_id = await _setup(client)
    list_id = await _create_list(client, token, company_id)
    h = _h(token, company_id)

    note_id = (await client.post(f"/lists/{list_id}/notes", json={"note": "original"}, headers=h)).json()["id"]
    await client.patch(f"/lists/{list_id}/notes/{note_id}", json={"note": "updated"}, headers=h)

    notes = (await client.get(f"/lists/{list_id}/notes", headers=h)).json()
    assert notes[0]["note"] == "updated"


@pytest.mark.asyncio
async def test_list_notes_delete(client):
    token, company_id = await _setup(client)
    list_id = await _create_list(client, token, company_id)
    h = _h(token, company_id)

    note_id = (await client.post(f"/lists/{list_id}/notes", json={"note": "del"}, headers=h)).json()["id"]
    await client.delete(f"/lists/{list_id}/notes/{note_id}", headers=h)

    assert (await client.get(f"/lists/{list_id}/notes", headers=h)).json() == []


@pytest.mark.asyncio
async def test_list_notes_empty_rejected(client):
    token, company_id = await _setup(client)
    list_id = await _create_list(client, token, company_id)
    h = _h(token, company_id)

    r = await client.post(f"/lists/{list_id}/notes", json={"note": ""}, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_list_notes_wrong_id_404(client):
    token, company_id = await _setup(client)
    list_id = await _create_list(client, token, company_id)
    h = _h(token, company_id)

    r = await client.delete(f"/lists/{list_id}/notes/note:nonexistent", headers=h)
    assert r.status_code == 404

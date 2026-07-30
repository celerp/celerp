# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for entity file endpoints (contacts and docs)."""

from __future__ import annotations

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _headers(client) -> dict:
    reg = await client.post(
        "/auth/register",
        json={"company_name": "FilesCo", "email": "files@test.com", "name": "Admin", "password": "pw"},
    )
    token = reg.json()["access_token"]
    companies = await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token}"})
    company_id = companies.json()["items"][0]["company_id"]
    return {"Authorization": f"Bearer {token}", "X-Company-Id": company_id}


_SMALL_PDF = b"%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n"


# ── Contact file tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_contact_file_upload(client):
    h = await _headers(client)

    # Create a contact
    r = await client.post("/crm/contacts", json={"name": "Alice"}, headers=h)
    assert r.status_code == 200
    cid = r.json()["id"]

    # Upload a file
    r = await client.post(
        f"/crm/contacts/{cid}/files",
        files={"file": ("test.pdf", _SMALL_PDF, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200
    data = r.json()
    assert "id" in data
    fid = data["id"]

    # Verify appears in contact projection
    r = await client.get(f"/crm/contacts/{cid}", headers=h)
    assert r.status_code == 200
    contact = r.json()
    files = contact.get("files", [])
    assert any(f.get("id") == fid for f in files), f"File {fid} not in {files}"


@pytest.mark.asyncio
async def test_contact_file_tag_update(client):
    h = await _headers(client)

    r = await client.post("/crm/contacts", json={"name": "Bob"}, headers=h)
    assert r.status_code == 200
    cid = r.json()["id"]

    r = await client.post(
        f"/crm/contacts/{cid}/files",
        files={"file": ("doc.pdf", _SMALL_PDF, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200
    fid = r.json()["id"]

    # Update tag
    r = await client.post(
        f"/crm/contacts/{cid}/files/{fid}/tag",
        data={"document_tag": "contracts"},
        headers=h,
    )
    assert r.status_code == 200

    r = await client.get(f"/crm/contacts/{cid}", headers=h)
    files = r.json().get("files", [])
    match = next((f for f in files if f.get("id") == fid), None)
    assert match is not None
    assert match.get("document_tag") == "contracts"


@pytest.mark.asyncio
async def test_contact_file_description_update(client):
    h = await _headers(client)

    r = await client.post("/crm/contacts", json={"name": "Carol"}, headers=h)
    assert r.status_code == 200
    cid = r.json()["id"]

    r = await client.post(
        f"/crm/contacts/{cid}/files",
        files={"file": ("desc.pdf", _SMALL_PDF, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200
    fid = r.json()["id"]

    r = await client.patch(
        f"/crm/contacts/{cid}/files/{fid}/description",
        data={"description": "My important receipt"},
        headers=h,
    )
    assert r.status_code == 200

    r = await client.get(f"/crm/contacts/{cid}", headers=h)
    files = r.json().get("files", [])
    match = next((f for f in files if f.get("id") == fid), None)
    assert match is not None
    assert match.get("description") == "My important receipt"


@pytest.mark.asyncio
async def test_contact_file_delete(client):
    h = await _headers(client)

    r = await client.post("/crm/contacts", json={"name": "Dave"}, headers=h)
    assert r.status_code == 200
    cid = r.json()["id"]

    r = await client.post(
        f"/crm/contacts/{cid}/files",
        files={"file": ("del.pdf", _SMALL_PDF, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200
    fid = r.json()["id"]

    r = await client.delete(f"/crm/contacts/{cid}/files/{fid}", headers=h)
    assert r.status_code == 200

    r = await client.get(f"/crm/contacts/{cid}", headers=h)
    files = r.json().get("files", [])
    assert not any(f.get("id") == fid for f in files), "File should be deleted"


# ── Doc file tests ─────────────────────────────────────────────────────────────

async def _create_draft_doc(client, h: dict) -> str:
    """Create a minimal draft invoice and return its entity_id."""
    r = await client.post(
        "/docs",
        json={
            "doc_type": "invoice",
            "contact_id": None,
            "line_items": [],
            "currency": "USD",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_doc_file_upload(client):
    h = await _headers(client)
    doc_id = await _create_draft_doc(client, h)

    r = await client.post(
        f"/docs/{doc_id}/files",
        files={"file": ("invoice-scan.pdf", _SMALL_PDF, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200
    data = r.json()
    assert "id" in data
    fid = data["id"]

    r = await client.get(f"/docs/{doc_id}", headers=h)
    assert r.status_code == 200
    doc = r.json()
    files = doc.get("files", [])
    assert any(f.get("id") == fid for f in files), f"File {fid} not in {files}"


async def _ledger_by_type(client, h: dict, entity_id: str) -> dict:
    r = await client.get("/ledger", params={"entity_id": entity_id, "limit": 200}, headers=h)
    assert r.status_code == 200, r.text
    out: dict[str, list[dict]] = {}
    for e in r.json()["items"]:
        out.setdefault(e["event_type"], []).append(e)
    return out


@pytest.mark.asyncio
async def test_contact_file_events_capture_filename(client):
    """M6: contact file events keep the filename in the activity log (incl. delete)."""
    h = await _headers(client)
    cid = (await client.post("/crm/contacts", json={"name": "Eve"}, headers=h)).json()["id"]
    fid = (await client.post(f"/crm/contacts/{cid}/files",
                             files={"file": ("evidence.pdf", _SMALL_PDF, "application/pdf")},
                             headers=h)).json()["id"]
    assert (await client.post(f"/crm/contacts/{cid}/files/{fid}/tag",
                              data={"document_tag": "contracts"}, headers=h)).status_code == 200
    assert (await client.patch(f"/crm/contacts/{cid}/files/{fid}/description",
                               data={"description": "signed"}, headers=h)).status_code == 200
    assert (await client.delete(f"/crm/contacts/{cid}/files/{fid}", headers=h)).status_code == 200

    by_type = await _ledger_by_type(client, h, cid)
    fname = by_type["crm.contact.file_attached"][0]["data"].get("filename")
    assert fname, "attached event must carry the filename"
    for et in ("crm.contact.file_tagged", "crm.contact.file_description_updated", "crm.contact.file_deleted"):
        assert et in by_type, f"missing event {et}"
        assert by_type[et][0]["data"].get("filename") == fname, f"{et} did not capture the filename"


@pytest.mark.asyncio
async def test_doc_file_events_capture_filename(client):
    """M6: doc file events keep the filename in the activity log (incl. delete)."""
    h = await _headers(client)
    doc_id = await _create_draft_doc(client, h)
    fid = (await client.post(f"/docs/{doc_id}/files",
                             files={"file": ("scan.pdf", _SMALL_PDF, "application/pdf")},
                             headers=h)).json()["id"]
    assert (await client.patch(f"/docs/{doc_id}/files/{fid}/tag",
                               data={"document_tag": "scans"}, headers=h)).status_code == 200
    assert (await client.patch(f"/docs/{doc_id}/files/{fid}/description",
                               data={"description": "page 1"}, headers=h)).status_code == 200
    assert (await client.delete(f"/docs/{doc_id}/files/{fid}", headers=h)).status_code == 200

    by_type = await _ledger_by_type(client, h, doc_id)
    fname = by_type["doc.file_attached"][0]["data"].get("filename")
    assert fname, "attached event must carry the filename"
    for et in ("doc.file_tagged", "doc.file_description_updated", "doc.file_deleted"):
        assert et in by_type, f"missing event {et}"
        assert by_type[et][0]["data"].get("filename") == fname, f"{et} did not capture the filename"


@pytest.mark.asyncio
async def test_doc_file_delete(client):
    h = await _headers(client)
    doc_id = await _create_draft_doc(client, h)

    r = await client.post(
        f"/docs/{doc_id}/files",
        files={"file": ("todel.pdf", _SMALL_PDF, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200
    fid = r.json()["id"]

    r = await client.delete(f"/docs/{doc_id}/files/{fid}", headers=h)
    assert r.status_code == 200

    r = await client.get(f"/docs/{doc_id}", headers=h)
    doc = r.json()
    files = doc.get("files", [])
    assert not any(f.get("id") == fid for f in files), "File should be deleted"


def test_files_section_is_public_symbol():
    """The shared files section is public: a module renders it against its own endpoints."""
    import ui.components.files as files_mod

    assert hasattr(files_mod, "files_section")
    assert not hasattr(files_mod, "_files_section")

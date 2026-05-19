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


@pytest.mark.asyncio
async def test_contact_file_has_uploaded_at(client):
    """File projection entry must include uploaded_at timestamp."""
    h = await _headers(client)
    r = await client.post("/crm/contacts", json={"name": "UploadDate Test"}, headers=h)
    assert r.status_code == 200
    cid = r.json()["id"]

    r = await client.post(
        f"/crm/contacts/{cid}/files",
        files={"file": ("ts_test.pdf", _SMALL_PDF, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200
    fid = r.json()["id"]

    r = await client.get(f"/crm/contacts/{cid}", headers=h)
    files = r.json().get("files", [])
    match = next((f for f in files if f.get("id") == fid), None)
    assert match is not None
    assert match.get("uploaded_at"), f"uploaded_at missing from file entry: {match}"


@pytest.mark.asyncio
async def test_doc_file_has_uploaded_at(client):
    """Doc file projection entry must include uploaded_at timestamp."""
    h = await _headers(client)
    doc_id = await _create_draft_doc(client, h)

    r = await client.post(
        f"/docs/{doc_id}/files",
        files={"file": ("ts_doc.pdf", _SMALL_PDF, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200
    fid = r.json()["id"]

    r = await client.get(f"/docs/{doc_id}", headers=h)
    assert r.status_code == 200
    files = r.json().get("files", [])
    match = next((f for f in files if f.get("id") == fid), None)
    assert match is not None
    assert match.get("uploaded_at"), f"uploaded_at missing from doc file entry: {match}"


@pytest.mark.asyncio
async def test_contact_file_download_resolves_data_dir(client):
    """GET /crm/contacts/{cid}/files/{fid} must return the file.

    Regression: url stored in the projection is /static/attachments/... (a
    web-relative path). The download handler was resolving it as a bare
    filesystem path relative to cwd, giving ./static/attachments/... - but
    the actual file lives under settings.data_dir / static/attachments/...
    Also: url was not stored in the contact file projection at all (missing
    from event data), so match.get("url") returned "" causing unconditional 404.
    """
    from celerp.config import settings

    h = await _headers(client)

    r = await client.post("/crm/contacts", json={"name": "DownloadTest"}, headers=h)
    assert r.status_code == 200
    cid = r.json()["id"]

    fake_pdf = b"%PDF-1.0\ntest-content"
    r = await client.post(
        f"/crm/contacts/{cid}/files",
        files={"file": ("dl_test.pdf", fake_pdf, "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200, f"Upload failed: {r.text}"
    fid = r.json()["id"]

    r = await client.get(f"/crm/contacts/{cid}/files/{fid}", headers=h)
    assert r.status_code == 200, (
        f"Download returned {r.status_code}. "
        "Route likely resolves path relative to cwd instead of settings.data_dir."
    )
    assert r.content == fake_pdf
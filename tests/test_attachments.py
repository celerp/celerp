# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for attachment endpoints (upload, delete, bulk ZIP, preview_image_id)."""

from __future__ import annotations

import io
import zipfile

import pytest
from httpx import AsyncClient


async def _token(client: AsyncClient) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "AttachCo", "email": "att@test.com", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_item(client: AsyncClient, token: str, sku: str) -> str:
    r = await client.post(
        "/items",
        json={"sku": sku, "name": f"Item {sku}", "quantity": 1, "sell_by": "piece"},
        headers=_h(token),
    )
    assert r.status_code == 200
    return r.json()["id"]


@pytest.fixture
def small_png() -> bytes:
    """Minimal valid 1x1 PNG."""
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
        b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )


@pytest.fixture
def small_pdf() -> bytes:
    return b"%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n"


@pytest.fixture
def small_mp4() -> bytes:
    # Minimal ftyp box header — enough for content-type dispatch
    return b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2avc1mp41"


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ── Upload: type inference ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_image_returns_metadata(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-IMG-001")
    resp = await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("photo.png", small_png, "image/png")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "image"
    assert data["filename"] == "photo.png"
    assert "/static/attachments/" in data["url"]
    assert data["size"] == len(small_png)


@pytest.mark.asyncio
async def test_upload_pdf_yields_certificate(client: AsyncClient, small_pdf: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-CERT-001")
    resp = await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("cert.pdf", small_pdf, "application/pdf")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    assert resp.json()["type"] == "certificate"


@pytest.mark.asyncio
async def test_upload_video_type(client: AsyncClient, small_mp4: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-VID-001")
    resp = await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("clip.mp4", small_mp4, "video/mp4")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    assert resp.json()["type"] == "video"


@pytest.mark.asyncio
async def test_upload_view360_via_query_param(client: AsyncClient, small_png: bytes):
    """An image MIME can be explicitly tagged as view_360."""
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-360-001")
    resp = await client.post(
        f"/items/{item_id}/attachments?attachment_type=view_360",
        files={"file": ("panorama.jpg", small_png, "image/jpeg")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    assert resp.json()["type"] == "view_360"


@pytest.mark.asyncio
async def test_upload_invalid_attachment_type_returns_422(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-BAD-TYPE")
    resp = await client.post(
        f"/items/{item_id}/attachments?attachment_type=banana",
        files={"file": ("x.png", small_png, "image/png")},
        headers=_h(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_unsupported_mime_returns_422(client: AsyncClient):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-BAD-001")
    resp = await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("script.sh", b"#!/bin/sh", "application/x-sh")},
        headers=_h(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_to_missing_item_returns_404(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    resp = await client.post(
        "/items/item:does-not-exist/attachments",
        files={"file": ("x.png", small_png, "image/png")},
        headers=_h(token),
    )
    assert resp.status_code == 404


# ── preview_image_id auto-management ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_image_sets_preview_image_id(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-PREV-001")
    att = (await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("cover.png", small_png, "image/png")},
        headers=_h(token),
    )).json()
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert item.get("preview_image_id") == att["id"]


@pytest.mark.asyncio
async def test_non_image_does_not_set_preview(client: AsyncClient, small_pdf: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-PREV-002")
    await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("cert.pdf", small_pdf, "application/pdf")},
        headers=_h(token),
    )
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert item.get("preview_image_id") is None


@pytest.mark.asyncio
async def test_delete_preview_falls_back_to_next_image(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-PREV-003")

    att1 = (await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("first.png", small_png, "image/png")},
        headers=_h(token),
    )).json()
    att2 = (await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("second.png", small_png, "image/png")},
        headers=_h(token),
    )).json()

    # att1 should be the preview
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert item["preview_image_id"] == att1["id"]

    # Delete att1; preview should fall back to att2
    await client.delete(f"/items/{item_id}/attachments/{att1['id']}", headers=_h(token))
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert item["preview_image_id"] == att2["id"]


@pytest.mark.asyncio
async def test_delete_last_image_clears_preview(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-PREV-004")

    att = (await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("img.png", small_png, "image/png")},
        headers=_h(token),
    )).json()
    await client.delete(f"/items/{item_id}/attachments/{att['id']}", headers=_h(token))
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert item.get("preview_image_id") is None


# ── PUT /preview endpoint ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_preview_image_explicit(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-SETPREV-001")

    att1 = (await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("a.png", small_png, "image/png")},
        headers=_h(token),
    )).json()
    att2 = (await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("b.png", small_png, "image/png")},
        headers=_h(token),
    )).json()

    # Explicitly switch preview to att2
    resp = await client.put(
        f"/items/{item_id}/attachments/{att2['id']}/preview",
        headers=_h(token),
    )
    assert resp.status_code == 200
    assert resp.json()["preview_image_id"] == att2["id"]

    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    assert item["preview_image_id"] == att2["id"]


@pytest.mark.asyncio
async def test_set_preview_non_image_returns_422(client: AsyncClient, small_png: bytes, small_pdf: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-SETPREV-002")

    await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("cover.png", small_png, "image/png")},
        headers=_h(token),
    )
    cert = (await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("cert.pdf", small_pdf, "application/pdf")},
        headers=_h(token),
    )).json()

    resp = await client.put(
        f"/items/{item_id}/attachments/{cert['id']}/preview",
        headers=_h(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_set_preview_missing_attachment_returns_404(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-SETPREV-003")
    await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("img.png", small_png, "image/png")},
        headers=_h(token),
    )
    resp = await client.put(
        f"/items/{item_id}/attachments/does-not-exist/preview",
        headers=_h(token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_preview_missing_item_returns_404(client: AsyncClient):
    token = await _token(client)
    resp = await client.put(
        "/items/item:ghost/attachments/att-id/preview",
        headers=_h(token),
    )
    assert resp.status_code == 404


# ── Multiple uploads accumulate ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_uploads_accumulate(client: AsyncClient, small_png: bytes, small_pdf: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-MULTI-001")
    await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("img.png", small_png, "image/png")},
        headers=_h(token),
    )
    await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("cert.pdf", small_pdf, "application/pdf")},
        headers=_h(token),
    )
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    atts = item.get("attachments") or []
    assert len(atts) == 2
    types = {a["type"] for a in atts}
    assert types == {"image", "certificate"}


# ── Delete ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_removes_from_item(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    item_id = await _seed_item(client, token, "ATT-DEL-001")
    att = (await client.post(
        f"/items/{item_id}/attachments",
        files={"file": ("x.png", small_png, "image/png")},
        headers=_h(token),
    )).json()
    att_id = att["id"]

    resp = await client.delete(f"/items/{item_id}/attachments/{att_id}", headers=_h(token))
    assert resp.status_code == 204

    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    atts = item.get("attachments") or []
    assert all(a["id"] != att_id for a in atts)


@pytest.mark.asyncio
async def test_delete_missing_item_returns_404(client: AsyncClient):
    token = await _token(client)
    resp = await client.delete(
        "/items/item:ghost/attachments/some-id",
        headers=_h(token),
    )
    assert resp.status_code == 404


# ── Bulk ZIP ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_matches_by_sku(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    await _seed_item(client, token, "BULK-A")
    await _seed_item(client, token, "BULK-B")
    zip_data = _make_zip({"BULK-A.png": small_png, "BULK-B.png": small_png})
    resp = await client.post(
        "/items/attachments/bulk",
        files={"file": ("batch.zip", zip_data, "application/zip")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["matched"] == 2
    assert result["unmatched"] == 0


@pytest.mark.asyncio
async def test_bulk_reports_unmatched(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    zip_data = _make_zip({"GHOST-999.png": small_png})
    resp = await client.post(
        "/items/attachments/bulk",
        files={"file": ("batch.zip", zip_data, "application/zip")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["unmatched"] == 1
    assert result["matched"] == 0


@pytest.mark.asyncio
async def test_bulk_cert_label_preserved(client: AsyncClient, small_pdf: bytes):
    token = await _token(client)
    sku = "BULK-CERT"
    item_id = await _seed_item(client, token, sku)
    zip_data = _make_zip({f"{sku}-cert-gia.pdf": small_pdf})
    resp = await client.post(
        "/items/attachments/bulk",
        files={"file": ("batch.zip", zip_data, "application/zip")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    atts = item.get("attachments") or []
    cert = next((a for a in atts if a.get("type") == "certificate"), None)
    assert cert is not None
    assert cert.get("label") == "gia"


@pytest.mark.asyncio
async def test_bulk_doc_label_backward_compat(client: AsyncClient, small_pdf: bytes):
    """-doc- suffix still works as certificate alias."""
    token = await _token(client)
    sku = "BULK-DOC"
    item_id = await _seed_item(client, token, sku)
    zip_data = _make_zip({f"{sku}-doc-cert.pdf": small_pdf})
    resp = await client.post(
        "/items/attachments/bulk",
        files={"file": ("batch.zip", zip_data, "application/zip")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    atts = item.get("attachments") or []
    doc = next((a for a in atts if a.get("type") == "certificate"), None)
    assert doc is not None
    assert doc.get("label") == "cert"


@pytest.mark.asyncio
async def test_bulk_360_typed_correctly(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    sku = "BULK-360"
    item_id = await _seed_item(client, token, sku)
    zip_data = _make_zip({f"{sku}-360-front.jpg": small_png})
    resp = await client.post(
        "/items/attachments/bulk",
        files={"file": ("batch.zip", zip_data, "application/zip")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    item = (await client.get(f"/items/{item_id}", headers=_h(token))).json()
    atts = item.get("attachments") or []
    att_360 = next((a for a in atts if a.get("type") == "view_360"), None)
    assert att_360 is not None
    assert att_360.get("label") == "front"


@pytest.mark.asyncio
async def test_bulk_invalid_zip_returns_422(client: AsyncClient):
    token = await _token(client)
    resp = await client.post(
        "/items/attachments/bulk",
        files={"file": ("notazip.zip", b"this is not a zip", "application/zip")},
        headers=_h(token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_skips_directories(client: AsyncClient, small_png: bytes):
    token = await _token(client)
    await _seed_item(client, token, "BULK-A2")
    zip_data = _make_zip({"subdir/": b"", "BULK-A2.png": small_png})
    resp = await client.post(
        "/items/attachments/bulk",
        files={"file": ("batch.zip", zip_data, "application/zip")},
        headers=_h(token),
    )
    assert resp.status_code == 200
    report = resp.json().get("report", [])
    assert all(r["file"] != "subdir/" for r in report)
    assert resp.json()["matched"] == 1

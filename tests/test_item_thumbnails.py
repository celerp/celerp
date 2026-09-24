# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Inventory list thumbnails: generated on upload, backfilled on first request,
served per company, and surfaced on every item as thumbnail_file_id."""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from celerp.services import attachments as att_svc


def _png(width: int = 640, height: int = 400, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


async def _headers(client, company: str = "ThumbCo", email: str = "thumbs@example.com") -> dict:
    r = await client.post(
        "/auth/register",
        json={"company_name": company, "email": email, "name": "Admin", "password": "pwvalid1"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _item(client, h: dict, sku: str) -> str:
    r = await client.post(
        "/items",
        json={"status": "available", "sku": sku, "name": "Thumb item", "quantity": 1, "sell_by": "piece"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _upload(client, h: dict, item_id: str, data: bytes, name: str = "photo.png") -> str:
    r = await client.post(f"/items/{item_id}/files", files={"file": (name, data, "image/png")}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _thumb_path(company_id: str, file_id: str):
    return att_svc.local_attachment_path(company_id, att_svc.thumbnail_name(file_id))


async def _company_id(client, h: dict) -> str:
    r = await client.get("/auth/my-companies", headers=h)
    return r.json()["items"][0]["company_id"]


@pytest.mark.asyncio
async def test_store_upload_writes_thumbnail(tmp_path, monkeypatch):
    from starlette.datastructures import UploadFile

    monkeypatch.setattr(att_svc.LocalBackend, "_root", property(lambda self: tmp_path))
    up = UploadFile(io.BytesIO(_png(640, 400)), filename="photo.png", headers={"content-type": "image/png"})
    meta = await att_svc.store_upload("co-1", up)
    thumb = tmp_path / "co-1" / att_svc.thumbnail_name(meta["id"])
    assert thumb.is_file()
    assert meta["thumb_url"].endswith("/" + att_svc.thumbnail_name(meta["id"]))
    with Image.open(thumb) as im:
        assert im.format == "JPEG"
        assert max(im.size) == 160
        assert im.size == (160, 100)


@pytest.mark.asyncio
async def test_thumbnail_route_lazy_backfill_idempotent(client):
    h = await _headers(client)
    company_id = await _company_id(client, h)
    item_id = await _item(client, h, "THUMB-LAZY")
    file_id = await _upload(client, h, item_id, _png())
    thumb = _thumb_path(company_id, file_id)
    assert thumb is not None
    thumb.unlink()  # a file uploaded before thumbnails existed has no thumbnail on disk

    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, max-age=86400"
    with Image.open(io.BytesIO(r.content)) as im:
        assert max(im.size) == 160
    created = _thumb_path(company_id, file_id)
    assert created is not None
    first_stat = os.stat(created)

    r2 = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r2.status_code == 200
    assert r2.content == r.content
    assert os.stat(created).st_mtime_ns == first_stat.st_mtime_ns  # served, not regenerated


@pytest.mark.asyncio
async def test_thumbnail_route_undecodable_returns_404(client):
    h = await _headers(client)
    item_id = await _item(client, h, "THUMB-BAD")
    file_id = await _upload(client, h, item_id, b"\x89PNG\r\n\x1a\n" + b"not an image")
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 404
    # A non-image file never has a thumbnail either.
    r = await client.post(
        f"/items/{item_id}/files",
        files={"file": ("spec.pdf", b"%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n", "application/pdf")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = await client.get(f"/items/{item_id}/files/{r.json()['id']}/thumbnail", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_thumbnail_route_other_company_404(client):
    h = await _headers(client)
    item_id = await _item(client, h, "THUMB-TENANT")
    file_id = await _upload(client, h, item_id, _png())
    # A second company for the same user: register is bootstrap-only, so the
    # tenant boundary is crossed by creating a company and using its token.
    r = await client.post("/companies", json={"name": "OtherCo"}, headers=h)
    assert r.status_code == 200, r.text
    other = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=other)
    assert r.status_code == 404
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_item_projection_thumbnail_file_id(client):
    h = await _headers(client)
    item_id = await _item(client, h, "THUMB-PROJ")
    r = await client.get(f"/items/{item_id}", headers=h)
    assert r.json()["thumbnail_file_id"] is None

    first = await _upload(client, h, item_id, _png(color=(10, 200, 10)), name="first.png")
    second = await _upload(client, h, item_id, _png(color=(10, 10, 200)), name="second.png")
    r = await client.get(f"/items/{item_id}", headers=h)
    assert r.json()["thumbnail_file_id"] == first  # the hero image

    r = await client.post(f"/items/{item_id}/files/{second}/hero", headers=h)
    assert r.status_code == 200, r.text
    r = await client.get(f"/items/{item_id}", headers=h)
    assert r.json()["thumbnail_file_id"] == second

    r = await client.get("/items", params={"q": "THUMB-PROJ"}, headers=h)
    rows = [it for it in r.json()["items"] if it["id"] == item_id]
    assert rows and rows[0]["thumbnail_file_id"] == second

    r = await client.delete(f"/items/{item_id}/files/{second}", headers=h)
    assert r.status_code == 204, r.text
    r = await client.get(f"/items/{item_id}", headers=h)
    assert r.json()["thumbnail_file_id"] == first

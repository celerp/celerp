# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Inventory list thumbnails: generated on upload, backfilled on first request,
served per company, and surfaced on every item as thumbnail_file_id."""

from __future__ import annotations

import asyncio
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


def _thumb_name(file_id: str) -> str:
    return att_svc.thumbnail_id(file_id) + ".jpg"


def _thumb_path(company_id: str, file_id: str):
    return att_svc.local_attachment_path(company_id, _thumb_name(file_id))


async def _company_id(client, h: dict) -> str:
    r = await client.get("/auth/my-companies", headers=h)
    return r.json()["items"][0]["company_id"]


@pytest.mark.asyncio
async def test_store_upload_writes_thumbnail(tmp_path, monkeypatch):
    from starlette.datastructures import UploadFile

    monkeypatch.setattr(att_svc.LocalBackend, "_root", property(lambda self: tmp_path))
    up = UploadFile(io.BytesIO(_png(640, 400)), filename="photo.png", headers={"content-type": "image/png"})
    meta = await att_svc.store_upload("co-1", up)
    thumb = tmp_path / "co-1" / _thumb_name(meta["id"])
    assert thumb.is_file()
    assert "thumb_url" not in meta  # found by its id, never by a recorded URL
    with Image.open(thumb) as im:
        assert im.format == "JPEG"
        assert max(im.size) == 160
        assert im.size == (160, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize("att_id", ["../outside", "..\\outside", "sub/name", "..", ""])
async def test_local_store_writes_only_inside_the_company_folder(tmp_path, monkeypatch, att_id):
    monkeypatch.setattr(att_svc.LocalBackend, "_root", property(lambda self: tmp_path / "attachments"))
    with pytest.raises(ValueError):
        await att_svc.LocalBackend().store("co-1", att_id, b"x", "image/jpeg")
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == []


@pytest.mark.asyncio
async def test_imported_attachment_id_cannot_place_a_thumbnail_elsewhere(tmp_path, monkeypatch):
    from celerp.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    company_dir = tmp_path / "static" / "attachments" / "co-1"
    company_dir.mkdir(parents=True)
    (company_dir / "original.png").write_bytes(_png(64, 64))
    attachment = {"id": "..\\..\\escaped", "mime": "image/png",
                  "url": "/static/attachments/co-1/original.png"}
    assert await att_svc.get_or_create_thumbnail("co-1", attachment) is None
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["original.png"]


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
    item_id = await _item(client, h, "THUMB-OTHER")
    file_id = await _upload(client, h, item_id, _png())
    # A second company for the same user.
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


async def _user_with_role(client, session, admin_h: dict, role: str) -> dict:
    addr = f"{role}-{os.urandom(4).hex()}@example.com"
    r = await client.post(
        "/companies/me/users",
        json={"name": role.title(), "email": addr, "password": "testpass123", "role": role},
        headers=admin_h,
    )
    assert r.status_code == 200, r.text
    from celerp.services.session_tracker import clear as _clear_tracker
    await _clear_tracker(session)
    r2 = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r2.status_code == 200, r2.text
    return {"Authorization": f"Bearer {r2.json()['access_token']}"}


@pytest.mark.asyncio
async def test_thumbnail_and_download_require_view_inventory(client, session):
    """The thumbnail and download routes are gated on view_inventory like the item they
    belong to: a role without it is refused, not served."""
    h = await _headers(client, "ThumbGateCo", "gate@example.com")
    viewer = await _user_with_role(client, session, h, "viewer")
    item_id = await _item(client, h, "THUMB-GATE")
    file_id = await _upload(client, h, item_id, _png())
    assert (await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=viewer)).status_code == 200
    r = await client.patch(
        "/companies/me/role-permissions",
        json={"perm_key": "view_inventory", "role_key": "viewer", "granted": False},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert (await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=viewer)).status_code == 403
    assert (await client.get(f"/items/{item_id}/files/{file_id}", headers=viewer)).status_code == 403


@pytest.mark.asyncio
async def test_image_follows_the_image_field_visibility(client, session):
    """When the Image field is limited to managers, a lower role gets no image id on the item
    and cannot fetch the image or its thumbnail; other files stay available to it."""
    h = await _headers(client, "ThumbVisCo", "vis@example.com")
    viewer = await _user_with_role(client, session, h, "viewer")
    item_id = await _item(client, h, "THUMB-VIS")
    image = await _upload(client, h, item_id, _png())
    r = await client.post(f"/items/{item_id}/files",
                          files={"file": ("spec.txt", b"spec sheet", "text/plain")}, headers=h)
    assert r.status_code == 200, r.text
    text_file = r.json()["id"]
    await client.post(f"/items/{item_id}/files/{image}/hero", headers=h)

    schema = (await client.get("/companies/me/item-schema", headers=h)).json()
    fields = schema["fields"] if isinstance(schema, dict) else schema
    for f in fields:
        if f["key"] == "thumbnail":
            f["visible_to_roles"] = ["manager"]
    r = await client.patch("/companies/me/item-schema", headers=h, json={"fields": fields})
    assert r.status_code == 200, r.text

    item = (await client.get(f"/items/{item_id}", headers=viewer)).json()
    assert "thumbnail_file_id" not in item and "preview_image_id" not in item
    assert [f["id"] for f in _item_files(item)] == [text_file]
    assert image not in str(item)  # no image id or URL anywhere in the item
    rows = (await client.get("/items", params={"q": "THUMB-VIS"}, headers=viewer)).json()["items"]
    assert rows and all("thumbnail_file_id" not in row for row in rows)
    assert all(image not in str(row) for row in rows)
    assert (await client.get(f"/items/{item_id}/files/{image}/thumbnail", headers=viewer)).status_code == 404
    assert (await client.get(f"/items/{item_id}/files/{image}", headers=viewer)).status_code == 404
    assert (await client.get(f"/items/{item_id}/files/{text_file}", headers=viewer)).status_code == 200

    full = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert full["thumbnail_file_id"] == image
    assert {f["id"] for f in _item_files(full)} == {image, text_file}
    assert (await client.get(f"/items/{item_id}/files/{image}/thumbnail", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_file_routes_refuse_non_item_projection(client):
    """A document's id is not an item: every item file route answers 404 for it instead of
    reading the document projection as if it were an item."""
    h = await _headers(client, "ThumbKindCo", "kind@example.com")
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "c:1", "contact_name": "Alice",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 5, "line_total": 5}],
        "subtotal": 5, "tax": 0, "total": 5,
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await client.get(f"/items/{doc_id}/files/x/thumbnail", headers=h)).status_code == 404
    assert (await client.get(f"/items/{doc_id}/files/x", headers=h)).status_code == 404
    assert (await client.post(
        f"/items/{doc_id}/files", files={"file": ("p.png", _png(), "image/png")}, headers=h,
    )).status_code == 404


@pytest.mark.asyncio
async def test_upload_as_hero_replaces_preview(client):
    """A plain second upload keeps the first image as the preview; uploading with as_hero
    makes the new image the preview."""
    h = await _headers(client, "ThumbHeroCo", "hero@example.com")
    item_id = await _item(client, h, "THUMB-HERO")
    first = await _upload(client, h, item_id, _png())
    second = await _upload(client, h, item_id, _png(color=(30, 200, 30)), name="second.png")
    assert (await client.get(f"/items/{item_id}", headers=h)).json()["thumbnail_file_id"] == first
    r = await client.post(
        f"/items/{item_id}/files", params={"as_hero": "true"},
        files={"file": ("third.png", _png(color=(30, 30, 200)), "image/png")}, headers=h,
    )
    assert r.status_code == 200, r.text
    third = r.json()["id"]
    assert third not in (first, second)
    assert (await client.get(f"/items/{item_id}", headers=h)).json()["thumbnail_file_id"] == third


@pytest.mark.asyncio
async def test_store_upload_survives_thumbnail_store_failure(tmp_path, monkeypatch):
    """The original is stored and its metadata returned even when storing the derived
    thumbnail fails; the upload never reports an error for a missing derivative."""
    from starlette.datastructures import UploadFile

    monkeypatch.setattr(att_svc.LocalBackend, "_root", property(lambda self: tmp_path))
    original_store = att_svc.LocalBackend.store
    calls: list[str] = []

    async def _store(self, company_id, att_id, content, mime):
        calls.append(att_id)
        if len(calls) > 1:
            raise OSError("disk full")
        return await original_store(self, company_id, att_id, content, mime)

    monkeypatch.setattr(att_svc.LocalBackend, "store", _store)
    up = UploadFile(io.BytesIO(_png(640, 400)), filename="photo.png", headers={"content-type": "image/png"})
    meta = await att_svc.store_upload("co-2", up)
    assert len(calls) == 2
    assert (tmp_path / "co-2" / meta["url"].rsplit("/", 1)[-1]).is_file()
    assert not (tmp_path / "co-2" / _thumb_name(meta["id"])).exists()


@pytest.mark.asyncio
async def test_store_upload_makes_thumbnail_off_the_event_loop(tmp_path, monkeypatch):
    """Image decoding runs in a worker thread, never on the event loop."""
    import asyncio
    from starlette.datastructures import UploadFile

    monkeypatch.setattr(att_svc.LocalBackend, "_root", property(lambda self: tmp_path))
    threaded: list = []
    real_to_thread = asyncio.to_thread

    async def _to_thread(fn, *args, **kwargs):
        threaded.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(att_svc.asyncio, "to_thread", _to_thread)
    up = UploadFile(io.BytesIO(_png(640, 400)), filename="photo.png", headers={"content-type": "image/png"})
    meta = await att_svc.store_upload("co-3", up)
    assert att_svc.make_thumbnail in threaded
    assert (tmp_path / "co-3" / _thumb_name(meta["id"])).is_file()


def test_make_thumbnail_honors_exif_orientation():
    """A camera JPEG whose EXIF says rotate 90 degrees thumbnails upright: the stored
    640x400 pixels come out 100x160, matching how the browser shows the original."""
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (640, 400), (200, 30, 30)).save(buf, "JPEG", exif=exif)
    thumb = att_svc.make_thumbnail(buf.getvalue(), "image/jpeg")
    assert thumb is not None
    with Image.open(io.BytesIO(thumb)) as im:
        assert im.size == (100, 160)


def test_make_thumbnail_refuses_oversized_images(monkeypatch):
    """An image above the pixel bound is never decoded: no thumbnail, no error."""
    monkeypatch.setattr(att_svc, "_MAX_IMAGE_PIXELS", 1000)
    assert att_svc.make_thumbnail(_png(640, 400), "image/png") is None
    monkeypatch.setattr(att_svc, "_MAX_IMAGE_PIXELS", 40_000_000)
    assert att_svc.make_thumbnail(_png(640, 400), "image/png") is not None


def _zip(files: dict[str, bytes]) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _item_files(item: dict) -> list[dict]:
    return item.get("files") or (item.get("attributes") or {}).get("files") or []


class _CloudBackend:
    """A cloud-style backend that reads back only what it stored, like S3Backend."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.fail_read = False
        self.fail_thumb_store = False

    def _url(self, company_id, stored_id) -> str:
        return f"https://cdn.example.test/{company_id}/{stored_id}"

    async def store(self, company_id, att_id, content, mime):
        if self.fail_thumb_store and att_id.endswith("_thumb"):
            raise RuntimeError("storage unavailable")
        url = self._url(company_id, att_id)
        self.objects[url] = content
        return url

    async def read(self, company_id, url, max_bytes):
        self.reads.append(url)
        if self.fail_read:
            raise RuntimeError("read failed")
        if not url.startswith(self._url(company_id, "")):
            return None
        return self.objects.get(url)

    async def read_stored(self, company_id, stored_id, mime, max_bytes):
        return self.objects.get(self._url(company_id, stored_id))


def _thumb_bytes_ok(content: bytes) -> None:
    with Image.open(io.BytesIO(content)) as im:
        assert im.format == "JPEG" and max(im.size) == 160


@pytest.mark.asyncio
async def test_bulk_attached_cloud_image_previews_from_its_thumbnail(client, monkeypatch):
    backend = _CloudBackend()
    monkeypatch.setattr(att_svc, "_backend", backend)
    h = await _headers(client, "ThumbBulkCo", "thumbs-bulk@example.com")
    item_id = await _item(client, h, "THUMB-BULK")
    r = await client.post(
        "/items/files/bulk",
        files={"file": ("photos.zip", _zip({"THUMB-BULK.png": _png()}), "application/zip")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["matched"] == 1
    files = _item_files((await client.get(f"/items/{item_id}", headers=h)).json())
    assert len(files) == 1
    file_id = files[0]["id"]

    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 200, r.text
    _thumb_bytes_ok(r.content)
    assert backend.reads == []  # the stored thumbnail is served; the original is never read


@pytest.mark.asyncio
async def test_merged_item_keeps_the_file_thumbnail(client):
    h = await _headers(client, "ThumbMergeCo", "thumbs-merge@example.com")
    a = await _item(client, h, "THUMB-MERGE-A")
    b = await _item(client, h, "THUMB-MERGE-B")
    file_id = await _upload(client, h, a, _png())
    r = await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a}, headers=h)
    assert r.status_code == 200, r.text
    merged_id = r.json()["id"]
    r = await client.get(f"/items/{merged_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 200, r.text
    _thumb_bytes_ok(r.content)


async def _legacy_cloud_image(client, monkeypatch, company: str, data: bytes | None = None):
    """An item whose cloud-stored image was attached before thumbnails were made."""
    backend = _CloudBackend()
    monkeypatch.setattr(att_svc, "_backend", backend)
    h = await _headers(client, company, f"{company.lower()}@example.com")
    item_id = await _item(client, h, company.upper())
    backend.fail_thumb_store = True
    file_id = await _upload(client, h, item_id, _png())
    backend.fail_thumb_store = False
    original = next(u for u in backend.objects if u.endswith("/" + file_id))
    if data is not None:
        backend.objects[original] = data
    assert not any(u.endswith("_thumb") for u in backend.objects)
    return backend, h, item_id, file_id, original


async def _item_history(session, company_id: str, item_id: str):
    from sqlalchemy import func, select

    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection

    session.expire_all()
    events = await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id == item_id))
    row = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    return events, row.updated_at, row.version if hasattr(row, "version") else None


@pytest.mark.asyncio
async def test_legacy_cloud_image_gets_a_thumbnail_on_first_view(client, monkeypatch):
    backend, h, item_id, file_id, original = await _legacy_cloud_image(client, monkeypatch, "RepairCo")
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 200, r.text
    assert "location" not in r.headers
    _thumb_bytes_ok(r.content)
    assert backend.objects[backend._url(await _company_id(client, h), att_svc.thumbnail_id(file_id))] == r.content

    r2 = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r2.content == r.content
    assert backend.reads == [original]  # the stored thumbnail is served without reading the original again


@pytest.mark.asyncio
async def test_viewing_a_thumbnail_never_changes_the_item(client, session, monkeypatch):
    """Making a missing thumbnail on view writes storage only: no event, no change to the
    item's history or its place in lists ordered by last change."""
    _, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairQuietCo")
    company_id = await _company_id(client, h)
    before = await _item_history(session, company_id, item_id)
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 200, r.text
    assert await _item_history(session, company_id, item_id) == before


@pytest.mark.asyncio
async def test_thumbnail_never_follows_a_recorded_url(client, session, monkeypatch):
    """A file entry carrying a foreign original or thumbnail URL is never fetched from or
    redirected to: the preview only ever comes from this company's own storage."""
    from sqlalchemy.orm.attributes import flag_modified

    from celerp.models.projections import Projection

    backend = _CloudBackend()
    monkeypatch.setattr(att_svc, "_backend", backend)
    h = await _headers(client, "ThumbForeignCo", "thumbs-foreign@example.com")
    company_id = await _company_id(client, h)
    item_id = await _item(client, h, "THUMB-FOREIGN")
    row = await session.get(Projection, {"company_id": company_id, "entity_id": item_id})
    state = dict(row.state)
    state["files"] = [{"id": "planted", "filename": "p.png", "mime": "image/png", "size": 1,
                       "url": "http://169.254.169.254/latest/meta-data",
                       "thumb_url": "https://attacker.example.test/pixel.png", "is_hero": True}]
    row.state = state
    flag_modified(row, "state")
    await session.commit()

    r = await client.get(f"/items/{item_id}/files/planted/thumbnail", headers=h)
    assert r.status_code == 404
    assert "location" not in r.headers
    assert backend.objects == {}  # nothing fetched, nothing stored


@pytest.mark.asyncio
async def test_cloud_thumbnail_has_no_preview_when_the_read_fails(client, monkeypatch):
    backend, h, item_id, file_id, original = await _legacy_cloud_image(client, monkeypatch, "RepairReadCo")
    backend.fail_read = True
    for expected_reads in (1, 2):
        r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
        assert r.status_code == 404 and "location" not in r.headers
        assert len(backend.reads) == expected_reads  # nothing stored, so the next view retries


@pytest.mark.asyncio
async def test_cloud_thumbnail_skips_an_original_that_does_not_decode(client, monkeypatch):
    backend, h, item_id, file_id, _ = await _legacy_cloud_image(
        client, monkeypatch, "RepairBytesCo", data=b"not an image")
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 404 and "location" not in r.headers
    assert len(backend.reads) == 1
    assert not any(u.endswith("_thumb") for u in backend.objects)


@pytest.mark.asyncio
async def test_cloud_thumbnail_has_no_preview_when_it_cannot_be_stored(client, monkeypatch):
    backend, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairStoreCo")
    backend.fail_thumb_store = True
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 404 and "location" not in r.headers
    assert len(backend.reads) == 1


@pytest.mark.asyncio
async def test_thumbnail_waits_for_a_free_slot(client, monkeypatch):
    """A view past the limit waits for a slot rather than loading another full original."""
    backend, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairBusyCo")
    monkeypatch.setattr(att_svc, "_thumbnail_jobs", att_svc._MAX_THUMBNAIL_JOBS)

    async def _finish_other_job():
        await asyncio.sleep(0.3)
        att_svc._thumbnail_jobs -= 1

    freeing = asyncio.create_task(_finish_other_job())
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    await freeing
    assert r.status_code == 200, r.text
    assert len(backend.reads) == 1


@pytest.mark.asyncio
async def test_thumbnail_has_no_preview_when_no_slot_frees_up(client, monkeypatch):
    backend, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairFullCo")
    monkeypatch.setattr(att_svc, "_thumbnail_jobs", att_svc._MAX_THUMBNAIL_JOBS)
    monkeypatch.setattr(att_svc, "_THUMBNAIL_JOB_WAIT_S", 0.3)
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code == 404 and "location" not in r.headers
    assert backend.reads == []


@pytest.mark.asyncio
async def test_view_only_role_gets_a_cloud_thumbnail(client, session, monkeypatch):
    backend, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairViewCo")
    viewer = await _user_with_role(client, session, h, "viewer")
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=viewer)
    assert r.status_code == 200, r.text
    _thumb_bytes_ok(r.content)


class _S3Object:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self, amt=None):
        return self._data if amt is None else self._data[:amt]


class _S3Client:
    def __init__(self, data: bytes, length: int | None = None) -> None:
        self.data, self.length, self.keys = data, length, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_object(self, Bucket, Key):
        self.keys.append((Bucket, Key))
        return {"ContentLength": len(self.data) if self.length is None else self.length,
                "Body": _S3Object(self.data)}


@pytest.mark.asyncio
async def test_s3_read_back_reads_only_the_companys_own_objects(monkeypatch):
    fake = _S3Client(b"original")
    monkeypatch.setattr(att_svc, "_s3_client", lambda *a: fake)
    s3 = att_svc.S3Backend("https://s3.example.test", "bucket", "k", "s")
    base = "https://s3.example.test/bucket/attachments"
    assert await s3.read("co1", f"{base}/co1/a1.JPG", 100) == b"original"
    assert fake.keys == [("bucket", "attachments/co1/a1.JPG")]  # older uploads kept their own suffix
    for foreign in (f"{base}/co2/a1.png", "https://elsewhere.example.test/bucket/attachments/co1/a1.png",
                    f"{base}/co1/../co2/a1.png", f"{base}/co1/"):
        assert await s3.read("co1", foreign, 100) is None
    assert len(fake.keys) == 1


@pytest.mark.asyncio
async def test_s3_read_back_refuses_an_original_over_the_limit(monkeypatch):
    s3 = att_svc.S3Backend("", "bucket", "k", "s")
    url = "https://bucket.s3.amazonaws.com/attachments/co1/a1.png"
    monkeypatch.setattr(att_svc, "_s3_client", lambda *a: _S3Client(b"x" * 11))
    assert await s3.read("co1", url, 10) is None
    monkeypatch.setattr(att_svc, "_s3_client", lambda *a: _S3Client(b"x" * 11, length=0))
    assert await s3.read("co1", url, 10) is None  # a missing or wrong length still stops at the limit


class _MissingKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _S3MissingClient(_S3Client):
    async def get_object(self, Bucket, Key):
        self.keys.append((Bucket, Key))
        raise _MissingKey()


@pytest.mark.asyncio
async def test_s3_reads_a_stored_thumbnail_by_its_id(monkeypatch):
    fake = _S3Client(b"thumb")
    monkeypatch.setattr(att_svc, "_s3_client", lambda *a: fake)
    s3 = att_svc.S3Backend("https://s3.example.test", "bucket", "k", "s")
    assert await s3.read_stored("co1", "a1_thumb", "image/jpeg", 100) == b"thumb"
    assert fake.keys == [("bucket", "attachments/co1/a1_thumb.jpg")]
    assert await s3.read_stored("co1", "../co2/a1_thumb", "image/jpeg", 100) is None
    assert len(fake.keys) == 1
    missing = _S3MissingClient(b"")
    monkeypatch.setattr(att_svc, "_s3_client", lambda *a: missing)
    assert await s3.read_stored("co1", "a2_thumb", "image/jpeg", 100) is None

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
    assert meta.get("thumb_url") is None


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
    assert meta["thumb_url"]


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


class _RemoteBackend:
    """A cloud-style backend: keeps nothing on disk and hands back an absolute URL."""

    async def store(self, company_id, att_id, content, mime):
        return f"https://cdn.example.test/{company_id}/{att_id}"


def _zip(files: dict[str, bytes]) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _item_files(item: dict) -> list[dict]:
    return item.get("files") or (item.get("attributes") or {}).get("files") or []


@pytest.mark.asyncio
async def test_bulk_attached_remote_image_previews_from_its_thumbnail(client, monkeypatch):
    monkeypatch.setattr(att_svc, "_backend", _RemoteBackend())
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
    assert r.status_code in (302, 307), r.text
    assert r.headers["location"].endswith("/" + att_svc.thumbnail_id(file_id))


@pytest.mark.asyncio
async def test_merged_item_keeps_the_file_thumbnail(client):
    h = await _headers(client, "ThumbMergeCo", "thumbs-merge@example.com")
    a = await _item(client, h, "THUMB-MERGE-A")
    b = await _item(client, h, "THUMB-MERGE-B")
    file_id = await _upload(client, h, a, _png())
    r = await client.post("/items/merge", json={"source_entity_ids": [a, b], "target_sku_from": a}, headers=h)
    assert r.status_code == 200, r.text
    merged = (await client.get(f"/items/{r.json()['id']}", headers=h)).json()
    thumbs = {f["id"]: f.get("thumb_url") for f in _item_files(merged)}
    assert thumbs.get(file_id), thumbs


@pytest.mark.asyncio
async def test_connector_download_records_the_thumbnail_url(monkeypatch):
    from types import SimpleNamespace

    import celerp.connectors.images as images
    import celerp.events.engine as engine_mod

    emitted: dict = {}

    async def _emit(session, **kw):
        emitted.update(kw["data"])

    async def _store(company_id, upload):
        return {"id": "att-1", "filename": "p.png", "mime": "image/png", "size": 3,
                "url": "https://cdn.example.test/c/att-1",
                "thumb_url": "https://cdn.example.test/c/att-1_thumb"}

    class _Resp:
        content = b"png"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class _Http:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return _Resp()

    class _Session:
        async def get(self, *a, **k):
            return SimpleNamespace(state={"files": []})

    monkeypatch.setattr(engine_mod, "emit_event", _emit)
    monkeypatch.setattr(att_svc, "store_upload", _store)
    monkeypatch.setattr(images.httpx, "AsyncClient", _Http)

    ok = await images.download_and_emit_file(
        _Session(), "co", "item:1", "user:1", "https://img.example.test/p.png", "p.png", "product_images", True,
    )
    assert ok is True
    assert emitted["thumb_url"] == "https://cdn.example.test/c/att-1_thumb"


class _CloudBackend:
    """A cloud-style backend that can read back what it stored, like S3Backend."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.reads = 0
        self.fail_read = False
        self.fail_thumb_store = False

    async def store(self, company_id, att_id, content, mime):
        if self.fail_thumb_store and att_id.endswith("_thumb"):
            raise RuntimeError("storage unavailable")
        url = f"https://cdn.example.test/{company_id}/{att_id}"
        self.objects[url] = content
        return url

    async def read(self, company_id, url, max_bytes):
        self.reads += 1
        if self.fail_read:
            raise RuntimeError("read failed")
        return self.objects.get(url)


async def _legacy_cloud_image(client, monkeypatch, company: str, data: bytes | None = None):
    """An item whose cloud-stored image was attached before thumbnails were recorded."""
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
    assert _item_files((await client.get(f"/items/{item_id}", headers=h)).json())[0]["thumb_url"] is None
    return backend, h, item_id, file_id, original


async def _recorded_thumb(client, h, item_id) -> str | None:
    return _item_files((await client.get(f"/items/{item_id}", headers=h)).json())[0]["thumb_url"]


@pytest.mark.asyncio
async def test_legacy_cloud_image_gets_a_thumbnail_on_first_view(client, monkeypatch):
    backend, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairCo")
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.status_code in (302, 307), r.text
    thumb_url = r.headers["location"]
    assert thumb_url.endswith("/" + att_svc.thumbnail_id(file_id))
    with Image.open(io.BytesIO(backend.objects[thumb_url])) as im:
        assert max(im.size) == 160
    assert await _recorded_thumb(client, h, item_id) == thumb_url

    r2 = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r2.headers["location"] == thumb_url
    assert backend.reads == 1  # the recorded thumbnail is served without reading the original again


@pytest.mark.asyncio
async def test_cloud_repair_falls_back_to_the_original_when_the_read_fails(client, monkeypatch):
    backend, h, item_id, file_id, original = await _legacy_cloud_image(client, monkeypatch, "RepairReadCo")
    backend.fail_read = True
    for expected_reads in (1, 2):
        r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
        assert r.headers["location"] == original
        assert backend.reads == expected_reads  # nothing recorded, so the next view retries
    assert await _recorded_thumb(client, h, item_id) is None


@pytest.mark.asyncio
async def test_cloud_repair_skips_an_original_that_does_not_decode(client, monkeypatch):
    backend, h, item_id, file_id, original = await _legacy_cloud_image(
        client, monkeypatch, "RepairBytesCo", data=b"not an image")
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.headers["location"] == original
    assert backend.reads == 1
    assert await _recorded_thumb(client, h, item_id) is None
    assert not any(u.endswith("_thumb") for u in backend.objects)


@pytest.mark.asyncio
async def test_cloud_repair_falls_back_when_the_thumbnail_cannot_be_stored(client, monkeypatch):
    backend, h, item_id, file_id, original = await _legacy_cloud_image(client, monkeypatch, "RepairStoreCo")
    backend.fail_thumb_store = True
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.headers["location"] == original
    assert backend.reads == 1
    assert await _recorded_thumb(client, h, item_id) is None


@pytest.mark.asyncio
async def test_cloud_repair_waits_for_a_free_slot(client, monkeypatch):
    """A view past the repair limit waits for a slot rather than loading the full original."""
    backend, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairBusyCo")
    monkeypatch.setattr(att_svc, "_remote_repairs", att_svc._MAX_REMOTE_REPAIRS)

    async def _finish_other_repair():
        await asyncio.sleep(0.3)
        att_svc._remote_repairs -= 1

    freeing = asyncio.create_task(_finish_other_repair())
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    await freeing
    assert r.headers["location"].endswith("/" + att_svc.thumbnail_id(file_id))
    assert backend.reads == 1


@pytest.mark.asyncio
async def test_cloud_repair_falls_back_when_no_slot_frees_up(client, monkeypatch):
    backend, h, item_id, file_id, original = await _legacy_cloud_image(client, monkeypatch, "RepairFullCo")
    monkeypatch.setattr(att_svc, "_remote_repairs", att_svc._MAX_REMOTE_REPAIRS)
    monkeypatch.setattr(att_svc, "_REMOTE_REPAIR_WAIT_S", 0.3)
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=h)
    assert r.headers["location"] == original
    assert backend.reads == 0


@pytest.mark.asyncio
async def test_view_only_role_repairs_a_cloud_thumbnail(client, session, monkeypatch):
    backend, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairViewCo")
    viewer = await _user_with_role(client, session, h, "viewer")
    r = await client.get(f"/items/{item_id}/files/{file_id}/thumbnail", headers=viewer)
    assert r.headers["location"].endswith("/" + att_svc.thumbnail_id(file_id))
    assert await _recorded_thumb(client, h, item_id) == r.headers["location"]


@pytest.mark.asyncio
async def test_repaired_thumbnail_is_recorded_once_per_file(client, session, monkeypatch):
    """Views racing on the same file each try to record the same thumbnail; the ledger keeps one."""
    from celerp_inventory.routes_attachments import _record_thumbnail
    from sqlalchemy import func, select
    from celerp.models.ledger import LedgerEntry

    _, h, item_id, file_id, _ = await _legacy_cloud_image(client, monkeypatch, "RepairOnceCo")
    company_id = await _company_id(client, h)
    match = {"id": file_id, "filename": "photo.png"}
    url = f"https://cdn.example.test/{company_id}/{att_svc.thumbnail_id(file_id)}"
    for _ in range(2):
        await _record_thumbnail(session, company_id, item_id, match, url, None)
    count = await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.entity_id == item_id, LedgerEntry.event_type == "item.file.thumbnail_set"))
    assert count == 1
    assert await _recorded_thumb(client, h, item_id) == url


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
async def test_s3_read_back_is_limited_to_its_own_company_prefix(monkeypatch):
    fake = _S3Client(b"original")
    monkeypatch.setattr(att_svc, "_s3_client", lambda *a: fake)
    s3 = att_svc.S3Backend("https://s3.example.test", "bucket", "k", "s")
    base = "https://s3.example.test/bucket/attachments"
    assert await s3.read("co1", f"{base}/co1/a1.JPG", 100) == b"original"
    assert fake.keys == [("bucket", "attachments/co1/a1.JPG")]  # older uploads kept their own suffix
    for foreign in (f"{base}/co2/a1.png", "https://elsewhere.example.test/bucket/attachments/co1/a1.png",
                    f"{base}/co1/../co2/a1.png", f"{base}/co1/"):
        assert await s3.read("co1", foreign, 100) is None
    assert len(fake.keys) == 1  # nothing outside this company's prefix is ever requested


@pytest.mark.asyncio
async def test_s3_read_back_refuses_an_original_over_the_limit(monkeypatch):
    s3 = att_svc.S3Backend("", "bucket", "k", "s")
    url = "https://bucket.s3.amazonaws.com/attachments/co1/a1.png"
    monkeypatch.setattr(att_svc, "_s3_client", lambda *a: _S3Client(b"x" * 11))
    assert await s3.read("co1", url, 10) is None
    monkeypatch.setattr(att_svc, "_s3_client", lambda *a: _S3Client(b"x" * 11, length=0))
    assert await s3.read("co1", url, 10) is None  # a missing or wrong length still stops at the limit

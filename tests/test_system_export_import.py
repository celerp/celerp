# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for system export/import — POST /system/export and POST /system/import."""

from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client) -> str:
    email = f"sys-{uuid.uuid4().hex[:10]}@test.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Export Co", "email": email, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_items(client, token: str, count: int = 3) -> None:
    records = [
        {
            "entity_id": f"item:{uuid.uuid4().hex}",
            "event_type": "item.created",
            "idempotency_key": f"seed:{uuid.uuid4().hex}",
            "source": "test",
            "data": {"sku": f"SKU{i}", "name": f"Item {i}", "quantity": float(i)},
        }
        for i in range(count)
    ]
    r = await client.post("/items/import/batch", json={"records": records}, headers=_h(token))
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Export tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_export_returns_zip(client) -> None:
    token = await _register(client)
    r = await client.post("/system/export", headers=_h(token))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    cd = r.headers["content-disposition"]
    assert ".celerp" in cd


@pytest.mark.anyio
async def test_export_archive_structure(client) -> None:
    token = await _register(client)
    r = await client.post("/system/export", headers=_h(token))
    assert r.status_code == 200

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert "meta.json" in names
    assert "company.json" in names
    assert "ledger.jsonl" in names


@pytest.mark.anyio
async def test_export_meta_fields(client) -> None:
    token = await _register(client)
    r = await client.post("/system/export", headers=_h(token))
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    meta = json.loads(zf.read("meta.json"))
    assert meta["format_version"] == 1
    assert "exported_at" in meta
    assert "company_slug" in meta


@pytest.mark.anyio
async def test_export_company_json(client) -> None:
    token = await _register(client)
    r = await client.post("/system/export", headers=_h(token))
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    co = json.loads(zf.read("company.json"))
    assert "id" in co
    assert "name" in co
    assert "settings" in co
    assert "locations" in co
    assert "users" in co
    # auth_hash must NOT be exported
    for u in co["users"]:
        assert "auth_hash" not in u


@pytest.mark.anyio
async def test_export_ledger_contains_events(client) -> None:
    token = await _register(client)
    await _seed_items(client, token, count=3)
    r = await client.post("/system/export", headers=_h(token))
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    lines = [l for l in zf.read("ledger.jsonl").decode().splitlines() if l.strip()]
    assert len(lines) >= 3  # at least 3 item events
    ev = json.loads(lines[0])
    assert "entity_id" in ev
    assert "event_type" in ev
    assert "idempotency_key" in ev


@pytest.mark.anyio
async def test_export_requires_auth(client) -> None:
    r = await client.post("/system/export")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Import tests
# ---------------------------------------------------------------------------


def _build_minimal_archive(slug: str = "import-co") -> bytes:
    """Build a minimal valid .celerp archive with one item event."""
    buf = io.BytesIO()
    idem_key = f"seed:{uuid.uuid4().hex}"
    entity_id = f"item:{uuid.uuid4().hex}"

    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("meta.json", json.dumps({"format_version": 1, "exported_at": "2026-01-01T00:00:00+00:00", "company_slug": slug}))
        zf.writestr("company.json", json.dumps({
            "id": str(uuid.uuid4()),
            "name": "Import Co",
            "slug": slug,
            "settings": {},
            "created_at": "2026-01-01T00:00:00+00:00",
            "locations": [],
            "users": [],
        }))
        zf.writestr("ledger.jsonl", json.dumps({
            "entity_id": entity_id,
            "entity_type": "item",
            "event_type": "item.created",
            "data": {"sku": "SKU-IMPORT", "name": "Imported Item", "quantity": 1.0},
            "source": "test",
            "idempotency_key": idem_key,
            "metadata_": None,
            "ts": "2026-01-01T00:00:00+00:00",
        }))
    buf.seek(0)
    return buf.read()


@pytest.mark.anyio
async def test_import_creates_company_and_replays_events(client) -> None:
    token = await _register(client)
    archive = _build_minimal_archive(slug=f"import-co-{uuid.uuid4().hex[:6]}")
    r = await client.post(
        "/system/import",
        files={"file": ("snapshot.celerp", io.BytesIO(archive), "application/zip")},
        headers=_h(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["events_replayed"] == 1
    assert body["events_skipped"] == 0
    assert body["slug"]


@pytest.mark.anyio
async def test_import_slug_collision_renames(client) -> None:
    token = await _register(client)
    slug = f"collide-co-{uuid.uuid4().hex[:6]}"

    archive = _build_minimal_archive(slug=slug)
    r1 = await client.post(
        "/system/import",
        files={"file": ("snap.celerp", io.BytesIO(archive), "application/zip")},
        headers=_h(token),
    )
    assert r1.status_code == 200

    # Second import of same slug — must not 422/500; slug should be renamed
    archive2 = _build_minimal_archive(slug=slug)
    r2 = await client.post(
        "/system/import",
        files={"file": ("snap2.celerp", io.BytesIO(archive2), "application/zip")},
        headers=_h(token),
    )
    assert r2.status_code == 200
    assert r2.json()["slug"] != slug


@pytest.mark.anyio
async def test_import_rejects_bad_zip(client) -> None:
    token = await _register(client)
    r = await client.post(
        "/system/import",
        files={"file": ("bad.celerp", io.BytesIO(b"notazip"), "application/zip")},
        headers=_h(token),
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_import_rejects_wrong_format_version(client) -> None:
    token = await _register(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("meta.json", json.dumps({"format_version": 99, "exported_at": "x", "company_slug": "x"}))
        zf.writestr("company.json", json.dumps({}))
        zf.writestr("ledger.jsonl", "")
    buf.seek(0)
    r = await client.post(
        "/system/import",
        files={"file": ("snap.celerp", buf, "application/zip")},
        headers=_h(token),
    )
    assert r.status_code == 422


@pytest.mark.anyio
async def test_import_requires_auth(client) -> None:
    archive = _build_minimal_archive()
    r = await client.post(
        "/system/import",
        files={"file": ("snap.celerp", io.BytesIO(archive), "application/zip")},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_export_then_import_round_trip(client) -> None:
    """Export a company with items, import to a fresh slug, verify events count matches."""
    token = await _register(client)
    await _seed_items(client, token, count=4)

    # Export
    export_r = await client.post("/system/export", headers=_h(token))
    assert export_r.status_code == 200
    archive_bytes = export_r.content

    # Patch archive slug to avoid collision
    zf_in = zipfile.ZipFile(io.BytesIO(archive_bytes))
    company_data = json.loads(zf_in.read("company.json"))
    new_slug = f"{company_data['slug']}-rt-{uuid.uuid4().hex[:6]}"
    company_data["slug"] = new_slug
    meta = json.loads(zf_in.read("meta.json"))
    meta["company_slug"] = new_slug

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf_out:
        zf_out.writestr("meta.json", json.dumps(meta))
        zf_out.writestr("company.json", json.dumps(company_data))
        zf_out.writestr("ledger.jsonl", zf_in.read("ledger.jsonl").decode())
    buf.seek(0)

    # Import
    import_r = await client.post(
        "/system/import",
        files={"file": ("round-trip.celerp", buf, "application/zip")},
        headers=_h(token),
    )
    assert import_r.status_code == 200, import_r.text
    body = import_r.json()
    assert body["ok"] is True
    assert body["events_replayed"] >= 4
    assert body["slug"] == new_slug


# ---------------------------------------------------------------------------
# Restart endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_system_restart_returns_ok(client):
    """POST /system/restart must return 200 ok (actual SIGTERM is not sent in tests)."""
    from unittest.mock import patch
    token = await _register(client)
    # Patch _send_sigterm so it doesn't actually kill the test process
    with patch("celerp.routers.system._send_sigterm"):
        r = await client.post("/system/restart", headers=_h(token))
    assert r.status_code == 200
    assert r.json()["restarting"] is True


@pytest.mark.asyncio
async def test_system_restart_requires_auth(client):
    """POST /system/restart must reject unauthenticated requests."""
    r = await client.post("/system/restart")
    assert r.status_code == 401

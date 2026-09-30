# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""System Recovery: the whole-installation backup and restore, separate from company backups.
Covers the page and who may use it, the unchanged /backup/* routes and relay contract, honest
restore results, the safety point, and ending every session after a recovery."""

from __future__ import annotations

import base64
import io
import json
import re
import secrets
import tarfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fasthtml.common import to_xml

from company_backup_support import company, owner, token
from migration_support import auth, code_config, real_client, real_engine  # noqa: F401
from test_company_backup_ui import RECOVER, _anchors, _link, _page, ui  # noqa: F401

pytestmark = pytest.mark.asyncio

DESTRUCTIVE = "Replaces the Celerp database and installation files, affecting every company and user."
SAFETY_WARNING = "A safety backup could not be made before restoring."
PAGE = "/settings/system-recovery"
LEGACY_ROUTES = {
    "/backup/trigger": "post", "/backup/list": "get", "/backup/restore/{backup_id}": "post",
    "/backup/export": "get", "/backup/export/{backup_id}": "get", "/backup/import": "post",
    "/backup/import-bootstrap": "post",
}


def _key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _archive(path, *, dump: bytes = b"PGDUMP", meta: dict | None = None, members: tuple = ("database.dump", "meta.json")):
    """A whole-installation archive in the .celerp-backup layout."""
    meta = {"pg_version": "unknown", "company_name": "Harbor Goods Ltd", "enabled_modules": [], **(meta or {})}
    with tarfile.open(path, "w:gz") as tar:
        for name, body in (("database.dump", dump), ("meta.json", json.dumps(meta).encode())):
            if name in members:
                info = tarfile.TarInfo(name=name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
    return path


def _result(**kw):
    from celerp.services.backup import BackupResult
    return BackupResult(**{"ok": True, "size_bytes": 0, **kw})


def _import_internals(monkeypatch, tmp_path, *, safety=None) -> dict:
    """Stub the database, file and module steps of run_import; returns the recorded calls."""
    import celerp.connectors.ownership as ownership
    from celerp.config import settings
    from celerp.services import backup_import

    calls: dict[str, list] = {"safety": [], "restore": []}
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    async def _safety(label):
        calls["safety"].append(label)
        return safety or _result()

    async def _restore(dump_bytes, url):
        calls["restore"].append(dump_bytes)

    @asynccontextmanager
    async def _guard():
        yield

    async def _none(*a, **kw):
        return None

    async def _no_modules():
        return []

    monkeypatch.setattr(backup_import, "_safety_backup", _safety)
    monkeypatch.setattr(backup_import, "_run_pg_restore", _restore)
    monkeypatch.setattr(ownership, "connector_maintenance_guard", _guard)
    for name in ("_revoke_current_connector_state", "_dispose_engine", "_reconcile_schema",
                 "_clear_restored_connector_state", "_extract_files"):
        monkeypatch.setattr(backup_import, name, _none)
    monkeypatch.setattr(backup_import, "_activate_modules", AsyncMock(return_value=False))
    monkeypatch.setattr(backup_import, "_read_modules_from_restored_db", _no_modules)
    return calls


async def _install_owner(engine):
    user = await owner(engine)
    cid = await company(engine, user, "Alpha Trading", "alpha-marker")
    return user, cid, await token(engine, user, cid)


async def _company_owner(engine):
    """An owner of their own company who is not the installation owner."""
    user = await owner(engine, "other@example.com", name="Other")
    cid = await company(engine, user, "Beta Trading", "beta-marker")
    return user, cid, await token(engine, user, cid)


@pytest.fixture
def cloud_on(monkeypatch):
    """A connected paid cloud account with backups configured."""
    import celerp.gateway.state as gw_state
    from celerp.config import settings
    monkeypatch.setattr(settings, "gateway_token", "gateway-test-token")
    monkeypatch.setattr(settings, "backup_encryption_key", _key())
    monkeypatch.setattr(gw_state, "get_subscription_state", lambda: ("cloud", "active"))


def _controls(page: str) -> set[str]:
    """Every /backup/* target on the page: links, forms and HTMX actions."""
    return set(re.findall(r'(?:href|action|hx-get|hx-post|hx-delete)="(/backup/[^"]*)"', page))


def _cleared(r: httpx.Response, name: str) -> bool:
    for header in r.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            value = header.split(";", 1)[0].split("=", 1)[1].strip('"')
            return value == "" or "max-age=0" in header.lower() or "expires=thu, 01 jan 1970" in header.lower()
    return False


# ── Page and access ──────────────────────────────────────────────────────────

async def test_system_recovery_page_install_owner_only(ui, real_engine):
    """Only the installation owner can open System Recovery; another company owner is refused with 403."""
    _, _, tok = await _install_owner(real_engine)
    ui.cookies.set("celerp_token", tok)
    assert (await ui.get(PAGE)).status_code == 200

    _, _, other = await _company_owner(real_engine)
    ui.cookies.set("celerp_token", other)
    r = await ui.get(PAGE)
    assert r.status_code == 403, r.text
    assert DESTRUCTIVE not in _page(r)


async def test_system_recovery_states_destructive_scope(ui, real_engine):
    """System Recovery states that it replaces the whole installation, with the local export and import."""
    _, _, tok = await _install_owner(real_engine)
    ui.cookies.set("celerp_token", tok)
    page = _page(await ui.get(PAGE))
    assert DESTRUCTIVE in page
    assert {"/backup/export", "/backup/import"} <= _controls(page)


async def test_cloud_snapshot_actions_under_system_recovery(ui, real_engine, cloud_on):
    """The cloud recovery points and their actions live on System Recovery."""
    _, _, tok = await _install_owner(real_engine)
    ui.cookies.set("celerp_token", tok)
    page = _page(await ui.get(PAGE))
    assert {"/backup/export", "/backup/import", "/backup/trigger", "/backup/list"} <= _controls(page)


async def test_backup_tab_has_no_system_recovery_actions(ui, real_engine, cloud_on):
    """The company Backup tab has no whole-installation or cloud snapshot actions."""
    _, _, tok = await _install_owner(real_engine)
    ui.cookies.set("celerp_token", tok)
    r = await ui.get("/settings/general?tab=backup")
    assert r.status_code == 200, r.text
    page = _page(r)
    assert _link(page, "/company-backup/download", "Download company backup")
    assert _controls(page) == set()


async def test_cloud_summary_links_to_system_recovery():
    """The cloud settings backup summary links to System Recovery."""
    from ui.routes.settings_cloud import _backup_summary_card
    card = _backup_summary_card(gw_ok=True, backup_data={
        "db": {"last_run": "2026-09-29T02:00:00+00:00", "ok": True}, "next_db_utc": None})
    html = to_xml(card)
    assert f'href="{PAGE}"' in html
    assert 'href="/settings/general?tab=backup"' not in html


async def test_factory_reset_backup_link_unchanged():
    """The factory reset card still offers the whole-installation export first."""
    from ui.routes.settings import _factory_reset_card
    assert 'href="/backup/export"' in to_xml(_factory_reset_card())


async def test_legacy_import_on_fresh_install_is_system_recovery(ui, real_engine):
    """On a fresh install the whole-installation restore is a small link and its page states it replaces everything."""
    page = _page(await ui.get("/setup"))
    links = [(h, c) for h, c, t in _anchors(page) if h == "/setup/import-backup"]
    assert len(links) == 1 and "quick-link-action" not in links[0][1]
    assert _link(page, "/setup/import-backup", RECOVER)

    r = await ui.get("/setup/import-backup")
    assert r.status_code == 200, r.text
    assert DESTRUCTIVE in _page(r)


# ── Routes and authorization ─────────────────────────────────────────────────

async def test_legacy_routes_keep_names():
    """Every whole-installation route keeps its path and method."""
    from celerp.main import app
    paths = app.openapi()["paths"]
    for path, method in LEGACY_ROUTES.items():
        assert method in paths.get(path, {}), path


async def test_system_recovery_api_install_owner_only(real_client, real_engine):
    """Every System Recovery API route refuses a company owner who is not the installation owner."""
    _, _, tok = await _company_owner(real_engine)
    for method, path in (("post", "/backup/trigger"), ("get", "/backup/list"), ("post", "/backup/restore/snap-1"),
                         ("get", "/backup/export"), ("get", "/backup/export/snap-1")):
        r = await getattr(real_client, method)(path, headers=auth(tok))
        assert r.status_code == 403, path
        assert r.json()["detail"] == "Installation owner access required"


async def test_legacy_import_api_install_owner_only(real_client, real_engine, tmp_path, monkeypatch):
    """The whole-installation import refuses a company owner who is not the installation owner, and never imports."""
    from celerp.services import backup_import
    run = AsyncMock(return_value=_result())
    monkeypatch.setattr(backup_import, "run_import", run)
    await owner(real_engine)
    _, _, tok = await _company_owner(real_engine)
    archive = _archive(tmp_path / "whole.celerp-backup").read_bytes()
    r = await real_client.post("/backup/import", files={"file": ("whole.celerp-backup", archive)}, headers=auth(tok))
    assert r.status_code == 403
    r = await real_client.post("/backup/import-bootstrap", files={"file": ("whole.celerp-backup", archive)})
    assert r.status_code == 403
    assert "System Recovery" in r.text and "Settings > Backup" not in r.text
    run.assert_not_called()


async def test_system_recovery_continue_install_owner_only(ui, real_client, real_engine, tmp_path, monkeypatch):
    """Continuing a recovery, by import or cloud restore, is refused for anyone but the installation owner."""
    from celerp.services import backup_import, backup_repo
    run = AsyncMock(return_value=_result())
    snap = AsyncMock(return_value=_result())
    monkeypatch.setattr(backup_import, "run_import", run)
    monkeypatch.setattr(backup_repo, "restore_snapshot", snap)
    await owner(real_engine)
    _, _, tok = await _company_owner(real_engine)
    archive = _archive(tmp_path / "whole.celerp-backup").read_bytes()

    r = await real_client.post("/backup/import", files={"file": ("whole.celerp-backup", archive)}, headers=auth(tok))
    assert r.status_code == 403
    r = await real_client.post("/backup/restore/snap-1", headers=auth(tok))
    assert r.status_code == 403

    ui.cookies.set("celerp_token", tok)
    r = await ui.post("/backup/import", files={"file": ("whole.celerp-backup", archive, "application/octet-stream")})
    assert r.status_code in (200, 403)
    assert "Imported backup" not in r.text
    r = await ui.post("/backup/restore/snap-1")
    assert r.status_code in (200, 403)
    assert "Database restored" not in r.text
    run.assert_not_called()
    snap.assert_not_called()


# ── Restore results ──────────────────────────────────────────────────────────

async def test_cloud_restore_failure_propagates(tmp_path, monkeypatch):
    """A cloud restore returns the importer's result: a failure stays a failure and warnings pass through."""
    from celerp.config import settings
    from celerp.services import backup_import, backup_repo
    monkeypatch.setattr(settings, "backup_encryption_key", _key())
    archive = tmp_path / "snap.celerp-backup"

    async def _reassemble(snapshot_id):
        archive.write_bytes(b"archive")
        return archive

    monkeypatch.setattr(backup_repo, "reassemble_snapshot", _reassemble)
    failed = _result(ok=False, error="pg_restore exited with status 1")
    monkeypatch.setattr(backup_import, "run_import", AsyncMock(return_value=failed))
    result = await backup_repo.restore_snapshot("snap-1")
    assert result.ok is False
    assert result.error == "pg_restore exited with status 1"
    assert not archive.exists()

    done = _result(size_bytes=42, warnings=["celerp-example-widgets"], restart_scheduled=True)
    monkeypatch.setattr(backup_import, "run_import", AsyncMock(return_value=done))
    result = await backup_repo.restore_snapshot("snap-1")
    assert result.ok is True
    assert result.warnings == ["celerp-example-widgets"]
    assert result.restart_scheduled is True


async def test_cloud_restore_failure_shown_in_ui(ui, real_engine, tmp_path, monkeypatch):
    """A failed cloud restore shows the failure on System Recovery, never the success message."""
    from celerp.config import settings
    from celerp.services import backup_import, backup_repo
    monkeypatch.setattr(settings, "backup_encryption_key", _key())

    async def _reassemble(snapshot_id):
        return _archive(tmp_path / "snap.celerp-backup")

    monkeypatch.setattr(backup_repo, "reassemble_snapshot", _reassemble)
    monkeypatch.setattr(backup_import, "run_import",
                        AsyncMock(return_value=_result(ok=False, error="pg_restore exited with status 1")))
    _, _, tok = await _install_owner(real_engine)
    ui.cookies.set("celerp_token", tok)

    r = await ui.post("/backup/restore/snap-1")
    assert r.status_code == 200, r.text
    page = _page(r)
    assert "Restore failed" in page and "pg_restore exited with status 1" in page
    assert "Database restored" not in page
    assert "restart" not in page.lower()
    assert not _cleared(r, "celerp_token")


async def test_cloud_snapshot_relay_payload_unchanged(tmp_path, monkeypatch):
    """Cloud snapshots use the same relay paths and payloads as before."""
    import hashlib

    from celerp.config import settings
    from celerp.services import backup_repo

    real_client_cls = httpx.AsyncClient
    relay: list[tuple[str, str, bytes]] = []
    stored: dict[str, bytes] = {}
    snapshots: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "storage.test":
            if request.method == "PUT":
                stored[path] = request.content
                return httpx.Response(200)
            return httpx.Response(200, content=stored[path])
        relay.append((request.method, path, request.content))
        if path == "/repo/missing":
            return httpx.Response(200, json={"missing": json.loads(request.content)["hashes"]})
        blob = re.fullmatch(r"/repo/blob/([0-9a-f]{64})/(upload|download)-url", path)
        if blob:
            return httpx.Response(200, json={"url": f"https://storage.test/blobs/{blob.group(1)}"})
        if path == "/repo/snapshot":
            body = json.loads(request.content)
            snapshots.append({"id": "snap-1", "manifest_hash": body["manifest_hash"], "label": body["label"],
                              "size_bytes": 1, "created_at": "2026-09-29T02:00:00Z"})
            return httpx.Response(200, json={"id": "snap-1"})
        if path == "/repo/snapshots":
            return httpx.Response(200, json={"items": snapshots, "total_bytes": 0, "quota_bytes": None})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    @asynccontextmanager
    async def _relay():
        async with _client(base_url="https://relay.test", timeout=60) as client:
            yield client

    att = tmp_path / "static" / "attachments"
    att.mkdir(parents=True)
    (att / "receipt.pdf").write_bytes(b"RECEIPT")
    dump = b"PGDUMP-CUSTOM-FORMAT"

    async def _meta():
        return {"celerp_version": "1.0.0", "pg_version": "16", "created_at": "2026-09-29T02:00:00Z",
                "company_name": "Harbor Goods Ltd", "enabled_modules": []}

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    monkeypatch.setattr(backup_repo, "_relay", _relay)
    monkeypatch.setattr(backup_repo, "dump_database", lambda url: dump)
    monkeypatch.setattr(backup_repo, "_build_meta", _meta)
    monkeypatch.setattr(backup_repo, "_attachment_dirs", lambda: [att])
    monkeypatch.setattr(settings, "backup_encryption_key", _key())
    monkeypatch.setattr(settings, "cloud_disconnected", False)

    result = await backup_repo.run_snapshot(label="nightly")
    assert result.ok, result.error
    hashes = json.loads(relay[0][2])
    assert relay[0][:2] == ("POST", "/repo/missing") and set(hashes) == {"hashes"}
    assert hashes["hashes"] == sorted(hashes["hashes"]) and len(hashes["hashes"]) == 3
    assert {hashlib.sha256(dump).hexdigest(), hashlib.sha256(b"RECEIPT").hexdigest()} <= set(hashes["hashes"])
    uploads = relay[1:-1]
    assert sorted((m, p) for m, p, _ in uploads) == sorted(
        ("POST", f"/repo/blob/{h}/upload-url") for h in hashes["hashes"])
    assert all(body == b"" for _, _, body in uploads)
    method, path, body = relay[-1]
    assert (method, path) == ("POST", "/repo/snapshot")
    snapshot = json.loads(body)
    assert set(snapshot) == {"manifest_hash", "blobs", "label"} and snapshot["label"] == "nightly"
    assert all(set(b) == {"hash", "size"} for b in snapshot["blobs"])
    assert {b["hash"] for b in snapshot["blobs"]} == set(hashes["hashes"])
    assert snapshot["manifest_hash"] in hashes["hashes"]
    assert sorted(stored) == sorted(f"/blobs/{h}" for h in hashes["hashes"])

    relay.clear()
    listed = await backup_repo.list_snapshots()
    assert [(m, p) for m, p, _ in relay] == [("GET", "/repo/snapshots")]
    assert listed["items"][0]["id"] == "snap-1"

    relay.clear()
    path = await backup_repo.reassemble_snapshot("snap-1")
    try:
        assert [(m, p) for m, p, _ in relay][:2] == [
            ("GET", "/repo/snapshots"), ("GET", f"/repo/blob/{snapshot['manifest_hash']}/download-url")]
        assert sorted(p for _, p, _ in relay[2:]) == sorted(
            f"/repo/blob/{h}/download-url" for h in (hashlib.sha256(dump).hexdigest(),
                                                      hashlib.sha256(b"RECEIPT").hexdigest()))
        with tarfile.open(path, "r:gz") as tar:
            assert tar.extractfile("database.dump").read() == dump
            assert tar.extractfile("attachments/receipt.pdf").read() == b"RECEIPT"
            assert json.loads(tar.extractfile("meta.json").read())["company_name"] == "Harbor Goods Ltd"
    finally:
        path.unlink(missing_ok=True)


# ── Safety point ─────────────────────────────────────────────────────────────

async def test_system_recovery_validates_before_safety_point(real_client, real_engine, tmp_path, monkeypatch):
    """An invalid archive is refused before any safety point or restore is attempted."""
    from celerp.services import backup_import
    calls = _import_internals(monkeypatch, tmp_path)
    bad = [tmp_path / "not-an-archive.celerp-backup",
           _archive(tmp_path / "no-dump.celerp-backup", members=("meta.json",)),
           _archive(tmp_path / "no-meta.celerp-backup", members=("database.dump",))]
    bad[0].write_bytes(b"not a tar archive")
    for path in bad:
        result = await backup_import.run_import(path)
        assert result.ok is False and result.error, path.name
    assert calls == {"safety": [], "restore": []}

    _, _, tok = await _install_owner(real_engine)
    for path in bad:
        r = await real_client.post("/backup/import", files={"file": (path.name, path.read_bytes())}, headers=auth(tok))
        assert r.status_code == 200 and "flash--error" in r.text, path.name
    assert calls == {"safety": [], "restore": []}


async def test_system_recovery_warns_when_safety_point_fails(real_engine, tmp_path, monkeypatch):
    """When the safety backup fails the restore still runs and the result carries the safety warning."""
    from celerp.services import backup_import
    calls = _import_internals(monkeypatch, tmp_path, safety=_result(ok=False, error="relay unavailable"))
    result = await backup_import.run_import(_archive(tmp_path / "whole.celerp-backup", dump=b"PGDUMP-DATA"))
    assert result.ok is True, result.error
    assert calls["restore"] == [b"PGDUMP-DATA"]
    assert len(calls["safety"]) == 1
    assert any(SAFETY_WARNING in w for w in result.warnings)


async def test_system_recovery_continues_when_safety_point_fails(real_client, real_engine, tmp_path, monkeypatch):
    """A whole-installation import whose safety backup fails still restores and says the safety backup was not made."""
    calls = _import_internals(monkeypatch, tmp_path, safety=_result(ok=False, error="relay unavailable"))
    _, _, tok = await _install_owner(real_engine)
    archive = _archive(tmp_path / "whole.celerp-backup", dump=b"PGDUMP-DATA").read_bytes()
    r = await real_client.post("/backup/import", files={"file": ("whole.celerp-backup", archive)}, headers=auth(tok))
    assert r.status_code == 200, r.text
    assert calls["restore"] == [b"PGDUMP-DATA"]
    assert "Imported backup from Harbor Goods Ltd" in r.text
    assert SAFETY_WARNING in r.text
    assert "Import failed" not in r.text


# ── Sessions after a recovery ────────────────────────────────────────────────

async def test_pre_restore_token_rejected_after_system_recovery(real_client, real_engine, tmp_path, monkeypatch):
    """A token issued before a whole-installation restore is rejected after it."""
    from celerp.services import backup_import
    _import_internals(monkeypatch, tmp_path)
    _, _, tok = await _install_owner(real_engine)
    assert (await real_client.get("/companies/me", headers=auth(tok))).status_code == 200

    result = await backup_import.run_import(_archive(tmp_path / "whole.celerp-backup"))
    assert result.ok is True, result.error
    r = await real_client.get("/companies/me", headers=auth(tok))
    assert r.status_code == 401


@pytest.mark.parametrize("kind", ["import", "cloud"])
async def test_system_recovery_success_ends_session(ui, real_engine, tmp_path, monkeypatch, kind):
    """A successful recovery clears the session cookies and leads to restart or sign-in."""
    from celerp.config import settings
    from celerp.services import backup_import, backup_repo
    monkeypatch.setattr(settings, "backup_encryption_key", _key())
    archive = _archive(tmp_path / "whole.celerp-backup")

    async def _reassemble(snapshot_id):
        return _archive(tmp_path / "snap.celerp-backup")

    monkeypatch.setattr(backup_repo, "reassemble_snapshot", _reassemble)
    monkeypatch.setattr(backup_import, "run_import", AsyncMock(return_value=_result(size_bytes=6)))
    _, _, tok = await _install_owner(real_engine)
    ui.cookies.set("celerp_token", tok)
    ui.cookies.set("celerp_refresh", "refresh-before-restore")

    if kind == "import":
        r = await ui.post("/backup/import", files={"file": ("whole.celerp-backup", archive.read_bytes(),
                                                           "application/octet-stream")})
    else:
        r = await ui.post("/backup/restore/snap-1")
    assert r.status_code in (200, 303), r.text
    assert _cleared(r, "celerp_token") and _cleared(r, "celerp_refresh")
    lead = r.headers.get("location", "") + r.headers.get("hx-redirect", "") + r.text
    assert "/login" in lead or "/backup/restart-app" in lead or "restart" in lead.lower()
    assert "Import failed" not in r.text and "Restore failed" not in r.text


async def test_system_recovery_sign_in_button_is_labelled():
    """The sign-in button offered after a recovery shows its label, never a raw text key."""
    from celerp_backup.routes import _restore_flash
    body = _restore_flash(_result(), "Restored.").body.decode()
    assert re.search(r'<a href="/login"[^>]*>Sign in</a>', body), body
    assert "system_recovery." not in body

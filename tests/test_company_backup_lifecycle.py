# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What restoring a company backup leads to, and what it leaves behind: the name of the
company it makes, the outcome it reports, what it says travels and what does not, the
modules it gets ready first, and the backup and upload files, which are private and
kept only while a restore can still go on."""

from __future__ import annotations

import gzip
import io
import json
import os
import stat
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from company_backup_support import company, confirm, download, member, members, owner, read, restore, rezip, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())
    return tmp_path


async def _setup(engine, name: str = "Alpha Trading", marker: str = "alpha-marker"):
    user = await owner(engine)
    cid = await company(engine, user, name, marker)
    return user, cid, await token(engine, user, cid)


async def _name(engine, cid) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT name FROM companies WHERE id = :c"),
                                   {"c": uuid.UUID(str(cid))})).scalar_one()


async def _companies(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM companies"))).scalar_one()


async def _fresh_install(engine) -> None:
    """An installation with no users or companies left, as on first run."""
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))


def _uploads(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "company_backups" / "uploads"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ── E1: destination company name ─────────────────────────────────────────────

async def test_copy_beside_same_name_gets_distinct_default_name(real_client, real_engine):
    """A copy restored beside a company of the same name is named apart from it."""
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data)
    assert preview.json()["destination_name"] == "Alpha Trading (Restored)"
    r = await real_client.post("/company-backups/restore", json=confirm(preview), headers=auth(tok))
    assert r.status_code == 201, r.text
    assert r.json()["company_name"] == "Alpha Trading (Restored)"
    assert await _name(real_engine, r.json()["company_id"]) == "Alpha Trading (Restored)"
    # A second, separate backup of the same company gets the next free name.
    again = await read(real_client, tok, await download(real_client, tok))
    assert again.json()["destination_name"] == "Alpha Trading (Restored 2)"


async def test_unambiguous_name_is_kept(real_client, real_engine):
    """A backup restored by someone without a company of that name keeps its name."""
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    other = await owner(real_engine, "second@example.test", "Second")
    theirs = await company(real_engine, other, "Beta Goods", "beta-marker")
    preview = await read(real_client, await token(real_engine, other, theirs), data, mode="new_company")
    assert preview.json()["destination_name"] == "Alpha Trading"


async def test_chosen_destination_name_is_checked(real_client, real_engine):
    """The owner may name the copy; a blank name or one of their own companies is refused
    with nothing restored, and a distinct name is used as given."""
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data)
    before = await _companies(real_engine)
    for name, status in (("   ", 422), ("Alpha Trading", 409), ("alpha trading", 409)):
        r = await real_client.post("/company-backups/restore", json={**confirm(preview), "company_name": name},
                                   headers=auth(tok))
        assert r.status_code == status, (name, r.text)
        assert "Nothing was restored." in r.json()["detail"]
    assert await _companies(real_engine) == before
    r = await real_client.post("/company-backups/restore", json={**confirm(preview), "company_name": "Alpha Copy"},
                               headers=auth(tok))
    assert r.status_code == 201, r.text
    assert await _name(real_engine, r.json()["company_id"]) == "Alpha Copy"


async def test_existing_company_never_renamed_by_retry(real_client, real_engine):
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    first = await restore(real_client, tok, data)
    preview = await read(real_client, tok, data)
    r = await real_client.post("/company-backups/restore", json={**confirm(preview), "company_name": "Renamed"},
                               headers=auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["company_id"] == first.json()["company_id"]
    assert await _name(real_engine, first.json()["company_id"]) == "Alpha Trading (Restored)"


# ── E2: explicit outcomes ────────────────────────────────────────────────────

async def test_restore_reports_its_outcome(real_client, real_engine):
    """Created, opened the existing company, or opened it and added its team: the response
    says which, never a bare flag."""
    user, cid, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    first = await restore(real_client, tok, data)
    assert first.json()["outcome"] == "created" and "created" not in first.json()
    second = await restore(real_client, tok, data)
    assert second.json()["outcome"] == "opened_existing"
    assert second.json()["company_id"] == first.json()["company_id"]
    # A team member of the source company joins after the first restore: the next restore
    # opens the same company and gives them access.
    teammate = await owner(real_engine, "teammate@example.test", "Teammate")
    await member(real_engine, teammate, cid, "manager")
    third = await restore(real_client, tok, data)
    assert third.json()["outcome"] == "opened_existing_team_added"
    assert third.json()["team_members"] == 1


async def test_bootstrap_repeat_reports_opened_existing(real_client, real_engine, monkeypatch):
    user, cid, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    await _fresh_install(real_engine)
    account = {"name": "First Owner", "email": "first@example.test", "password": "firstpw1234"}

    r = await real_client.post("/company-backups/bootstrap/read", files={"file": ("a.celerp-company", data)})
    assert r.status_code == 200, r.text
    body = {"upload_token": r.json()["upload_token"], **account}
    first = await real_client.post("/company-backups/bootstrap/restore", json=body)
    assert first.status_code == 201 and first.json()["outcome"] == "created"
    again = await real_client.post("/company-backups/bootstrap/restore", json=body)
    assert again.status_code == 200, again.text
    assert again.json()["outcome"] == "opened_existing"
    assert again.json()["company_id"] == first.json()["company_id"]
    wrong = await real_client.post("/company-backups/bootstrap/restore", json={**body, "password": "otherpw1234"})
    assert wrong.status_code == 409


# ── E3: what travels with a backup, stated as policy ─────────────────────────

async def test_role_permission_policy_is_explicit():
    """Role permissions staying with the installation is a named policy, not one entry in
    a list of dropped settings."""
    from celerp.services import company_backup as cb
    assert "role_grants" not in cb.DROPPED_SETTINGS
    assert cb.ROLE_PERMISSIONS_SETTING == "role_grants"
    assert cb._kept_settings({"role_grants": {"viewer": ["x"]}, "currency": "THB"}) == {"currency": "THB"}


async def test_preview_states_what_travels_and_whose_role_permissions_apply(real_client, real_engine):
    user, cid, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    teammate = await owner(real_engine, "teammate@example.test", "Teammate")
    await member(real_engine, teammate, cid, "manager")
    same = (await read(real_client, tok, data)).json()
    assert same["scope"] == {"included": ["records", "settings", "attachments"],
                             "excluded": ["users", "passwords", "sessions", "connections", "share_links"],
                             "role_permissions": "source"}
    other = await owner(real_engine, "second@example.test", "Second")
    theirs = await company(real_engine, other, "Beta Goods", "beta-marker")
    elsewhere = (await read(real_client, await token(real_engine, other, theirs), data, mode="new_company")).json()
    assert elsewhere["scope"]["role_permissions"] == "destination"


# ── H: private, short-lived files ────────────────────────────────────────────

@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
async def test_staged_upload_is_private_with_its_metadata(real_client, real_engine, _data_dir):
    user, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    r = await read(real_client, tok, data, name="alpha books.celerp-company")
    upload_token = r.json()["upload_token"]
    stage = _data_dir / "company_backups" / "uploads" / f"{user}-{upload_token}.upload"
    meta = stage.with_suffix(".json")
    assert stage.is_file() and meta.is_file()
    assert _mode(stage) == 0o600 and _mode(meta) == 0o600
    assert _mode(stage.parent) == 0o700
    facts = json.loads(meta.read_text())
    assert facts["file_name"] == "alpha books.celerp-company"
    assert facts["owner"] == str(user) and facts["mode"] == "settings"
    assert len(facts["sha256"]) == 64 and facts["created_at"]


async def test_successful_restore_removes_upload(real_client, real_engine, _data_dir):
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data)
    first = await real_client.post("/company-backups/restore", json=confirm(preview), headers=auth(tok))
    assert first.status_code == 201
    assert [p for p in _uploads(_data_dir) if p.suffix != ".json"] == []
    record = json.loads(next(p for p in _uploads(_data_dir)).read_text())
    assert record["restored"] == {"company_id": first.json()["company_id"]}
    assert "sha256" not in record and "file_name" not in record
    # The response was lost and the restore is sent again: it opens the company it made.
    again = await real_client.post("/company-backups/restore", json=confirm(preview), headers=auth(tok))
    assert again.status_code == 200 and again.json()["outcome"] == "opened_existing"
    assert again.json()["company_id"] == first.json()["company_id"]
    assert (await restore(real_client, tok, data)).json()["outcome"] == "opened_existing"
    assert [p for p in _uploads(_data_dir) if p.suffix != ".json"] == []


async def test_stale_preview_keeps_upload(real_client, real_engine, _data_dir):
    user, cid, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data)
    await company(real_engine, user, "Alpha Trading (Restored)", "taken-marker")
    r = await real_client.post("/company-backups/restore", json=confirm(preview), headers=auth(tok))
    assert r.status_code == 409 and r.json()["code"] == "stale_preview", r.text
    assert r.json()["plan"]["destination_name"] == "Alpha Trading (Restored 2)"
    assert len(_uploads(_data_dir)) == 2  # the staged file and its metadata
    fresh = {**confirm(preview), "plan_fingerprint": r.json()["plan"]["plan_fingerprint"]}
    assert (await real_client.post("/company-backups/restore", json=fresh, headers=auth(tok))).status_code == 201


async def test_refused_restore_removes_upload(real_client, real_engine, _data_dir):
    """A restore refused for good leaves nothing behind."""
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data)
    r = await real_client.post("/company-backups/restore", json={**confirm(preview), "company_name": "Alpha Trading"},
                               headers=auth(tok))
    assert r.status_code == 409
    assert len(_uploads(_data_dir)) == 2  # a name to correct keeps the upload
    stage = next(p for p in _uploads(_data_dir) if p.suffix == ".upload")
    stage.write_bytes(b"not a backup any more")
    r = await real_client.post("/company-backups/restore", json=confirm(preview), headers=auth(tok))
    assert r.status_code == 422
    assert _uploads(_data_dir) == []


async def test_cancel_removes_upload_and_only_the_owners(real_client, real_engine, _data_dir):
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    upload_token = (await read(real_client, tok, data)).json()["upload_token"]
    other = await owner(real_engine, "second@example.test", "Second")
    theirs = await token(real_engine, other, await company(real_engine, other, "Beta Goods", "beta-marker"))
    r = await real_client.post("/company-backups/discard", json={"upload_token": upload_token}, headers=auth(theirs))
    assert r.status_code == 204
    assert len(_uploads(_data_dir)) == 2
    for bad in ("../x", "a/b", ""):
        r = await real_client.post("/company-backups/discard", json={"upload_token": bad}, headers=auth(tok))
        assert r.status_code == 204
    assert len(_uploads(_data_dir)) == 2
    r = await real_client.post("/company-backups/discard", json={"upload_token": upload_token}, headers=auth(tok))
    assert r.status_code == 204
    assert _uploads(_data_dir) == []


async def test_download_leaves_no_copy(real_client, real_engine, _data_dir):
    _, _, tok = await _setup(real_engine)
    await download(real_client, tok)
    left = [p for p in (_data_dir / "company_backups").rglob("*") if p.is_file()]
    assert left == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
async def test_export_file_is_private(real_engine, _data_dir):
    from celerp.services import company_backup as cb
    from celerp.services import company_backup_files as files
    _, cid, _ = await _setup(real_engine)
    out = files.export_path()
    await cb.export_company_snapshot(cid, out)
    assert _mode(out) == 0o600 and _mode(out.parent) == 0o700


async def test_startup_sweep_clears_orphans_and_keeps_live_uploads(real_client, real_engine, _data_dir):
    from celerp.services import company_backup_files as files
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    await read(real_client, tok, data)
    live = _uploads(_data_dir)
    uploads = _data_dir / "company_backups" / "uploads"
    old = uploads / "someone-0123abcd.upload"
    old.write_bytes(b"x")
    old.with_suffix(".json").write_text("{}")
    lonely = uploads / "someone-9999.json"
    lonely.write_text("{}")
    past = time.time() - 2 * 24 * 3600
    for p in (old, old.with_suffix(".json")):
        os.utime(p, (past, past))
    exports = files.export_path().parent
    orphan = exports / "crashed.export"
    orphan.write_bytes(b"x")
    files.sweep_transient_files()
    assert _uploads(_data_dir) == live
    assert not orphan.exists()


# ── I3: telling bad files apart ──────────────────────────────────────────────

async def test_bad_files_are_told_apart(real_client, real_engine):
    from celerp.services import company_backup as cb
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    cases = {
        b"just some text": cb.NOT_A_BACKUP,
        gzip.compress(b"pg_dump"): cb.SYSTEM_BACKUP,
        data[: len(data) // 2]: cb.INCOMPLETE,
    }
    for body, message in cases.items():
        r = await read(real_client, tok, body)
        assert r.status_code == 422 and r.json()["detail"] == message, r.text
    assert "damaged or incomplete" in cb.INCOMPLETE


# ── F: modules a backup needs, prepared in the flow ──────────────────────────

def _needs_module(data: bytes, name: str, version: str = "1.0.0") -> bytes:
    parts = members(data)
    meta = json.loads(parts["manifest.json"])
    meta["modules"]["enabled"] = [*meta["modules"]["enabled"], name]
    meta["modules"].setdefault("versions", {})[name] = version
    parts["manifest.json"] = json.dumps(meta).encode()
    return rezip(parts)


@pytest.fixture()
def installed(tmp_path, monkeypatch):
    """A MODULE_DIR holding acme-widgets (turned off), and a config file to turn it on in."""
    from celerp.config import write_config
    from celerp.modules import loader
    root = tmp_path / "modules"
    pkg = root / "acme-widgets"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('PLUGIN_MANIFEST = {"name": "acme-widgets", "version": "1.0.0", '
                                     '"display_name": "Widgets"}\n')
    monkeypatch.setenv("MODULE_DIR", str(root))
    cfg = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg))
    write_config({"modules": {"enabled": []}})
    monkeypatch.setattr(loader, "_load_errors", {})
    restarts: list[bool] = []
    from celerp.modules import requirements
    monkeypatch.setattr(requirements, "schedule_restart", lambda: restarts.append(True) or True)
    return root, restarts


def _first_party(monkeypatch, *names: str) -> None:
    from celerp.modules import loader
    monkeypatch.setattr(loader, "is_first_party", lambda path: Path(path).name in names)
    monkeypatch.setattr(loader, "first_party_names", lambda: frozenset(names))


def _start_running(monkeypatch, name: str) -> None:
    from celerp.modules import loader
    monkeypatch.setattr(loader, "_loaded", [*loader._loaded, {"name": name, "version": "1.0.0"}])


async def test_backup_needing_an_off_module_previews_its_preparation(real_client, real_engine, installed,
                                                                     monkeypatch, _data_dir):
    """A bundled module that is off is turned on from the restore itself; after the restart
    the same staged backup restores. Nothing is restored before then."""
    _first_party(monkeypatch, "acme-widgets")
    _, _, tok = await _setup(real_engine)
    data = _needs_module(await download(real_client, tok), "acme-widgets")
    preview = await read(real_client, tok, data)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["modules"] == [{"name": "acme-widgets", "label": "Widgets", "status": "enable_required",
                                "first_party": True}]
    assert body["modules_ready"] is False
    before = await _companies(real_engine)
    r = await real_client.post("/company-backups/restore", json=confirm(preview), headers=auth(tok))
    assert r.status_code == 409 and r.json()["code"] == "modules_required", r.text
    assert await _companies(real_engine) == before
    assert len(_uploads(_data_dir)) == 2
    _, restarts = installed
    r = await real_client.post("/company-backups/prepare", json={"upload_token": body["upload_token"]},
                               headers=auth(tok))
    assert r.status_code == 202, r.text
    assert r.json() == {"restart": True, "restarting": True}
    assert restarts == [True]
    from celerp.config import read_config
    assert "acme-widgets" in read_config()["modules"]["enabled"]
    # After the restart the module runs; the same upload is checked again and restored.
    _start_running(monkeypatch, "acme-widgets")
    again = await real_client.get("/company-backups/staged", params={"upload_token": body["upload_token"]},
                                  headers=auth(tok))
    assert again.status_code == 200, again.text
    assert again.json()["modules_ready"] is True
    r = await real_client.post("/company-backups/restore", json={
        "upload_token": body["upload_token"], "mode": "settings",
        "plan_fingerprint": again.json()["plan_fingerprint"]}, headers=auth(tok))
    assert r.status_code == 201, r.text


async def test_module_from_outside_celerp_needs_the_owners_consent(real_client, real_engine, installed,
                                                                   monkeypatch):
    _first_party(monkeypatch)
    _, _, tok = await _setup(real_engine)
    data = _needs_module(await download(real_client, tok), "acme-widgets")
    upload_token = (await read(real_client, tok, data)).json()["upload_token"]
    r = await real_client.post("/company-backups/prepare", json={"upload_token": upload_token}, headers=auth(tok))
    assert r.status_code == 409, r.text
    assert "Widgets" in r.json()["detail"]
    from celerp.config import read_config
    assert read_config()["modules"]["enabled"] == []
    r = await real_client.post("/company-backups/prepare", json={"upload_token": upload_token,
                                                                 "consent": ["acme-widgets"]}, headers=auth(tok))
    assert r.status_code == 202, r.text
    assert read_config()["modules"]["enabled"] == ["acme-widgets"]


async def test_first_run_prepares_modules_before_any_owner_exists(real_client, real_engine, installed,
                                                                  monkeypatch):
    """On a fresh installation the backup's modules are prepared before the first owner or
    company is created, and the same upload restores after the restart."""
    _first_party(monkeypatch, "acme-widgets")
    _, _, tok = await _setup(real_engine)
    data = _needs_module(await download(real_client, tok), "acme-widgets")
    await _fresh_install(real_engine)
    r = await real_client.post("/company-backups/bootstrap/read", files={"file": ("a.celerp-company", data)})
    assert r.status_code == 200, r.text
    upload_token = r.json()["upload_token"]
    assert r.json()["modules_ready"] is False
    account = {"name": "First Owner", "email": "first@example.test", "password": "firstpw1234"}
    refused = await real_client.post("/company-backups/bootstrap/restore",
                                     json={"upload_token": upload_token, **account})
    assert refused.status_code == 409 and refused.json()["code"] == "modules_required"
    async with real_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0
    r = await real_client.post("/company-backups/bootstrap/prepare", json={"upload_token": upload_token})
    assert r.status_code == 202, r.text
    _start_running(monkeypatch, "acme-widgets")
    again = await real_client.get("/company-backups/bootstrap/staged", params={"upload_token": upload_token})
    assert again.json()["modules_ready"] is True
    done = await real_client.post("/company-backups/bootstrap/restore", json={"upload_token": upload_token, **account})
    assert done.status_code == 201, done.text


async def test_first_run_imports_a_missing_module_in_flow(real_client, real_engine, installed, monkeypatch, tmp_path):
    """A module the installation lacks is imported through the module importer from the
    restore itself, then prepared, with the same upload kept."""
    _first_party(monkeypatch)
    _, _, tok = await _setup(real_engine)
    data = _needs_module(await download(real_client, tok), "acme-gadgets")
    await _fresh_install(real_engine)
    r = await real_client.post("/company-backups/bootstrap/read", files={"file": ("a.celerp-company", data)})
    assert r.status_code == 200, r.text
    upload_token = r.json()["upload_token"]
    assert r.json()["modules"][0]["status"] == "missing"
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("acme-gadgets/__init__.py", 'PLUGIN_MANIFEST = {"name": "acme-gadgets", "version": "1.0.0", '
                                                '"display_name": "Gadgets"}\n')
    r = await real_client.post("/company-backups/bootstrap/import-module", data={"upload_token": upload_token},
                               files={"file": ("acme-gadgets.zip", buf.getvalue())})
    assert r.status_code == 200, r.text
    assert r.json()["modules"] == [{"name": "acme-gadgets", "label": "Gadgets", "status": "enable_required",
                                    "first_party": False}]
    assert r.json()["upload_token"] == upload_token

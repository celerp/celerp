# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Company backups: what a .celerp-company file carries, how it is classified, how it is
bounded, how attachments travel, and every refusal that must leave the database untouched."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import io
import json
import os
import re
import struct
import subprocess
import sys
import tarfile
import uuid
import warnings
import zipfile
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import text

from company_backup_support import (
    FakeCloud,
    company,
    confirm,
    download,
    manifest,
    member,
    members,
    owner,
    read,
    replayed,
    restore,
    rezip,
    settle,
    sha256,
    snapshot,
    token,
    unchanged_except,
)
from migration_support import OWNER_EMAIL, auth, code_config, count, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent

_BK_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_BK_MODULE = "zz-widgets"
_BK_PREFIX = "zz_"
_BK_CODE_MARKER = "zz-widget-code-body-7f3a"
_BK_DROPPED_SETTINGS = ("role_grants", "ai_memory", "lock_date_set_by", "reorder_alert_email", "column_prefs",
                        "pay_tip_shown", "reorder_last_scan_at", "restored_backup")


# ── Shared helpers (module level, distinct names) ─────────────────────────────

@pytest.fixture(autouse=True)
def _bk_modules_running(monkeypatch):
    """Every bundled module and the fake third-party one run here, as on an installation
    that turned them on; tests model a module that is not running by taking it out."""
    from celerp.modules import loader
    running = [loader.read_manifest(p) for p in (REPO_ROOT / "default_modules").iterdir() if (p / "__init__.py").is_file()]
    monkeypatch.setattr(loader, "_loaded", [*loader._loaded, *running, {"name": _BK_MODULE, "version": "2.0.0"}])


def _bk_running_version(monkeypatch, name: str, version: str) -> None:
    """This process runs *version* of *name*, whatever the installed copy on disk says."""
    from celerp.modules import loader
    monkeypatch.setattr(loader, "_loaded", [*(m for m in loader._loaded if m["name"] != name),
                                            {"name": name, "version": version}])


def _bk_not_running(monkeypatch, name: str) -> None:
    from celerp.modules import loader
    monkeypatch.setattr(loader, "_loaded", [m for m in loader._loaded if m["name"] != name])


def _bk_cb():
    """The company backup service module."""
    return importlib.import_module("celerp.services.company_backup")


def _bk_local(monkeypatch, tmp_path):
    """Local attachment storage and staged uploads under tmp_path."""
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(attachments, "_backend", attachments.LocalBackend())


def _bk_cloud(monkeypatch, tmp_path) -> FakeCloud:
    """The fake cloud attachment backend, installed as the configured backend."""
    from celerp.config import settings
    from celerp.services import attachments
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    fake = FakeCloud()
    monkeypatch.setattr(attachments, "_backend", fake)
    return fake


_BK_DECLARED = {"zz_widgets": "include", "zz_gadgets": "include"}


def _bk_fake_module(tmp_path, monkeypatch, *, version: str = "2.0.0", backup: dict = _BK_DECLARED) -> Path:
    """Install a third-party module owning the zz_ table prefix in a MODULE_DIR entry,
    declaring how its tables travel with a company backup."""
    root = tmp_path / "bk-modules"
    pkg = root / _BK_MODULE
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "PLUGIN_MANIFEST = {\n"
        f'    "name": "{_BK_MODULE}",\n'
        f'    "version": "{version}",\n'
        '    "display_name": "Widgets",\n'
        f'    "table_prefix": "{_BK_PREFIX}",\n'
        f'    "company_backup": {backup!r},\n'
        "}\n\n\n"
        "def widget_code():\n"
        f'    return "{_BK_CODE_MARKER}"\n')
    existing = [e for e in os.environ.get("MODULE_DIR", "").split(",") if e.strip() and e.strip() != str(root)]
    monkeypatch.setenv("MODULE_DIR", ",".join([str(root), *existing]))
    _bk_running_version(monkeypatch, _BK_MODULE, version)
    return pkg


def _bk_uninstall_module(tmp_path, monkeypatch) -> None:
    """Remove the fake module's MODULE_DIR entry, as if it were never installed here."""
    root = str(tmp_path / "bk-modules")
    kept = [e for e in os.environ.get("MODULE_DIR", "").split(",") if e.strip() and e.strip() != root]
    monkeypatch.setenv("MODULE_DIR", ",".join(kept))


async def _bk_sql(engine, sql: str, **params) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _bk_drop(engine, *tables: str) -> None:
    async with engine.begin() as conn:
        for t in tables:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))


async def _bk_scalar(engine, sql: str, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _bk_settings(engine, cid) -> dict:
    raw = await _bk_scalar(engine, "SELECT settings::text FROM companies WHERE id = :c", c=cid)
    return json.loads(raw or "{}")


async def _bk_set_settings(engine, cid, settings: dict) -> None:
    await _bk_sql(engine, "UPDATE companies SET settings = CAST(:s AS json) WHERE id = :c",
                  s=json.dumps(settings), c=cid)


async def _bk_seed_portable(engine, cid, marker: str) -> None:
    """One row in every portable table that the base company helper does not fill."""
    from celerp_accounting.models import (
        Account,
        BankAccount,
        BankStatementLine,
        ReconciliationRule,
        ReconciliationSession,
    )
    from celerp_labels.models import LabelTemplate

    from celerp.models.company import WorkCenter
    async with maker(engine)() as s:
        loc = await s.scalar(text("SELECT id FROM locations WHERE company_id = :c LIMIT 1"), {"c": cid})
        s.add(WorkCenter(company_id=cid, name=f"{marker} bench", wip_location_id=loc, is_default=True))
        s.add(Account(company_id=cid, code="1111", name=f"{marker} cash", account_type="asset"))
        bank = BankAccount(company_id=cid, chart_account_code="1111", bank_name=f"{marker} bank",
                           account_number="****1234", bank_type="checking", currency="USD")
        s.add(bank)
        await s.flush()
        rec = ReconciliationSession(company_id=cid, bank_account_id=bank.id, statement_date="2026-01-31",
                                    statement_balance=100)
        s.add(rec)
        await s.flush()
        s.add(BankStatementLine(company_id=cid, reconciliation_id=rec.id, line_date="2026-01-15",
                                description=f"{marker} deposit", amount=100))
        s.add(ReconciliationRule(company_id=cid, bank_account_id=bank.id, match_pattern=f"{marker} fee",
                                 target_account_code="1111"))
        s.add(LabelTemplate(company_id=cid, name=f"{marker} label", fields=[{"key": "name"}]))
        await s.commit()


async def _bk_extra_ledger(engine, cid, n: int, marker: str) -> None:
    """n more ledger events, and the records they produce, for the company."""
    async with engine.begin() as conn:
        for i in range(n):
            await conn.execute(text(
                "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, idempotency_key) "
                "VALUES (:c, :e, 'item', 'item.created', CAST(:d AS json), 'api', :k)"),
                {"c": cid, "e": f"item:x{i}", "d": json.dumps({"name": f"{marker}-{i}"}), "k": f"k-{marker}-{i}"})
    await settle(engine, cid)


async def _bk_point_at(engine, cid, url: str) -> None:
    """Reference an attachment URL from the company's ledger data and so its record."""
    await _bk_set_data(engine, cid, {"name": "photo item", "attachments": [{"url": url, "name": "photo.png"}]})


async def _bk_set_data(engine, cid, data: dict) -> None:
    """Replace the company's ledger event data and rebuild its record from it."""
    await _bk_sql(engine, "UPDATE ledger SET data = CAST(:d AS json) WHERE company_id = :c",
                  c=cid, d=json.dumps(data))
    await settle(engine, cid)


def _bk_local_file(tmp_path, cid, name: str, body: bytes) -> str:
    folder = tmp_path / "static" / "attachments" / str(cid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(body)
    return f"/static/attachments/{cid}/{name}"


def _bk_edit_manifest(data: bytes, change) -> bytes:
    """The backup with its manifest changed in place by ``change(manifest)``."""
    parts = members(data)
    m = json.loads(parts["manifest.json"])
    change(m)
    parts["manifest.json"] = json.dumps(m).encode()
    return rezip(parts)


def _bk_raw_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """A zip holding exactly these entries, duplicates included."""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, body in entries:
                zf.writestr(name, body)
    return buf.getvalue()


def _bk_lie_about_size(data: bytes, name: str, claimed: int) -> bytes:
    """The zip with one member's declared uncompressed size replaced in both headers."""
    buf = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        offset, pos = zf.getinfo(name).header_offset, zf.start_dir
    struct.pack_into("<I", buf, offset + 22, claimed)
    while bytes(buf[pos:pos + 4]) == b"PK\x01\x02":
        n, e, c = struct.unpack_from("<HHH", buf, pos + 28)
        if bytes(buf[pos + 46:pos + 46 + n]).decode() == name:
            struct.pack_into("<I", buf, pos + 24, claimed)
        pos += 46 + n + e + c
    return bytes(buf)


def _bk_system_backup() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("meta.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    return buf.getvalue()


def _bk_file(tmp_path, data: bytes, name: str = "in.celerp-company") -> Path:
    path = tmp_path / "bk-files" / f"{uuid.uuid4().hex}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _bk_refused(engine, client, tok: str, user_id, tmp_path, data: bytes, *messages: str,
                      status: int = 422) -> None:
    """The backup is refused by the preview endpoint and by the restore service, and nothing
    was written: the database matches its snapshot and no company was added."""
    cb = _bk_cb()
    before = await snapshot(engine)
    companies = await count(engine, "companies")
    r = await read(client, tok, data)
    assert r.status_code == status, r.text
    for message in messages:
        assert message in r.json()["detail"], r.json()["detail"]
    with pytest.raises(cb.BackupError) as err:
        await cb.restore_company(_bk_file(tmp_path, data), mode="new_company", user_id=user_id)
    assert err.value.status_code == status
    for message in messages:
        assert message in err.value.detail, err.value.detail
    assert err.value.detail.endswith("Nothing was restored."), err.value.detail
    assert await snapshot(engine) == before
    assert await count(engine, "companies") == companies


async def _bk_modules_required(engine, client, tok: str, user_id, tmp_path, data: bytes, module: str,
                               status: str) -> None:
    """The backup is previewed with what ``module`` needs before it can be restored, and a
    restore through the API or the service is refused naming it; nothing was written."""
    cb = _bk_cb()
    before = await snapshot(engine)
    companies = await count(engine, "companies")
    r = await read(client, tok, data)
    assert r.status_code == 200, r.text
    assert r.json()["modules_ready"] is False
    needed = {m["name"]: m for m in r.json()["modules"]}
    assert needed[module]["status"] == status, r.json()["modules"]
    refused = await client.post("/company-backups/restore", json=confirm(r), headers=auth(tok))
    assert refused.status_code == 409 and refused.json()["code"] == "modules_required", refused.text
    assert needed[module]["label"] in refused.json()["detail"]
    with pytest.raises(cb.ModulesRequired) as err:
        await cb.restore_company(_bk_file(tmp_path, data), mode="new_company", user_id=user_id)
    assert err.value.detail.endswith("Nothing was restored."), err.value.detail
    assert await snapshot(engine) == before
    assert await count(engine, "companies") == companies


async def _bk_restore_new(client, tok: str, data: bytes) -> str:
    """Restore the backup as a new company through the API; returns its id."""
    r = await restore(client, tok, data, mode="new_company")
    assert r.status_code == 201, r.text
    return r.json()["company_id"]


async def _bk_setup(engine, name: str = "Alpha Trading", marker: str = "alpha-marker", **kw):
    """An owner, one company and an owner token for it."""
    user = await owner(engine)
    cid = await company(engine, user, name, marker, **kw)
    return user, cid, await token(engine, user, cid)


async def _bk_run(engine, user_id, cid, *, status: str = "completed", prepared_by: str | None = "Example Accounting",
                  marker: str = "run") -> uuid.UUID:
    """A migration run for the company created by the user."""
    from celerp.models.migration import MigrationRun
    async with maker(engine)() as s:
        run = MigrationRun(company_id=cid, created_by_user_id=user_id, source_system="fake_source",
                           scan_claim_sha256=hashlib.sha256(f"{marker}-{uuid.uuid4()}".encode()).hexdigest(),
                           source_artifact_sha256="0" * 64, adapter_version="1", cif_version="1",
                           mode="full_history", status=status, prepared_by=prepared_by)
        s.add(run)
        await s.commit()
        return run.id


async def _bk_normalized_rows(engine, table: str, cid, *, attributed: bool = False) -> list[str]:
    """The company's rows in a table with identity columns removed and every uuid replaced
    by a placeholder, sorted. With ``attributed`` each ledger row is first given the form a
    restore elsewhere keeps: no link to a user of this installation, and the author's name
    with a one-way reference to their account in its metadata."""
    actor = ("(to_jsonb(x) || jsonb_build_object('actor_id', NULL) || COALESCE((SELECT jsonb_build_object("
             "'metadata', COALESCE(CAST(x.metadata AS jsonb), '{}'::jsonb) || jsonb_build_object("
             "'backup_actor', jsonb_build_object('name', u.name, 'user_ref', "
             "encode(sha256(convert_to('celerp-backup-actor:' || CAST(u.id AS text), 'UTF8')), 'hex')))) "
             "FROM users u WHERE u.id = x.actor_id), '{}'::jsonb))") if attributed else "to_jsonb(x)"
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            f"SELECT ({actor} - 'id' - 'company_id')::text FROM \"{table}\" x "
            "WHERE company_id::text = :c"), {"c": str(cid)})).scalars().all()
    return sorted(_BK_UUID.sub("<id>", r) for r in rows)


async def _bk_company_text(engine, cid, tables) -> str:
    """Every portable row and the settings of one company as text."""
    parts = [json.dumps(await _bk_settings(engine, cid))]
    async with engine.connect() as conn:
        for t in tables:
            parts += (await conn.execute(text(f'SELECT to_jsonb(x)::text FROM "{t}" x WHERE company_id::text = :c'),
                                         {"c": str(cid)})).scalars().all()
    return "\n".join(parts)


class _BkSqlSpy:
    """Records, per statement, how many rows of one table a fetch returned or an insert wrote."""

    def __init__(self, table: str) -> None:
        self.fetches: list[int] = []
        self.inserts: list[int] = []
        self._from = re.compile(rf'\bFROM\s+"?{table}"?(?!\w)', re.IGNORECASE)
        self._insert = re.compile(rf'^\s*INSERT\s+INTO\s+"?{table}"?(?!\w)', re.IGNORECASE)
        self._saved: list[tuple[type, object]] = []

    def _sql(self, statement) -> str:
        from sqlalchemy.dialects import postgresql
        try:
            return str(statement.compile(dialect=postgresql.dialect()))
        except Exception:
            return str(statement) if isinstance(statement, str) else ""

    def __enter__(self):
        from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
        spy = self
        for cls in (AsyncSession, AsyncConnection):
            original = cls.execute
            self._saved.append((cls, original))

            async def execute(this, statement, *args, _original=original, **kw):
                result = await _original(this, statement, *args, **kw)
                sql = spy._sql(statement)
                if spy._insert.search(sql):
                    params = args[0] if args else kw.get("params")
                    spy.inserts.append(len(params) if isinstance(params, list) else result.rowcount)
                elif spy._from.search(sql) and getattr(result, "returns_rows", False):
                    frozen = result.freeze()
                    spy.fetches.append(len(frozen.data))
                    return frozen()
                return result
            cls.execute = execute
        return self

    def __exit__(self, *exc):
        for cls, original in self._saved:
            cls.execute = original
        return False


# ── Removed names and hand-off ───────────────────────────────────────────────

async def test_activation_payload_has_no_handoff():
    """The Connect activation payload builder takes and sends no hand-off id."""
    from celerp.gateway.state import activate_payload
    assert "handoff_id" not in inspect.signature(activate_payload).parameters
    with pytest.raises(TypeError):
        activate_payload("iid", handoff_id="h-1")
    assert "handoff_id" not in activate_payload("iid", first_boot=True, activation_verifier="v", boot_id="b")


@pytest.mark.parametrize("verifier", ["", "pending-verifier"])
async def test_checkin_payload_has_no_handoff(real_engine, real_client, tmp_path, monkeypatch, verifier):
    """The startup check-in and activation report no hand-off id, even after a backup was restored."""
    import celerp.config
    import celerp.db
    import celerp.gateway.state as state
    from celerp.config import settings
    from celerp.main import _try_auto_activate
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine)
    assert (await restore(real_client, tok, await download(real_client, tok))).status_code == 201

    @asynccontextmanager
    async def session_ctx():
        async with maker(real_engine)() as s:
            yield s
    sent: list[tuple[str, dict]] = []

    async def post(url, payload, **_):
        sent.append((url, payload))
    monkeypatch.setattr(celerp.db, "get_session_ctx", session_ctx)
    monkeypatch.setattr(celerp.config, "ensure_instance_id", lambda: "iid")
    monkeypatch.setattr(state, "relay_http_url", lambda: "https://relay.test")
    monkeypatch.setattr(state, "relay_post_with_retry", post)
    monkeypatch.setattr(settings, "activation_verifier", verifier)
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    await _try_auto_activate()
    expected = "https://relay.test/auth/activate" if verifier else "https://relay.test/auth/checkin"
    assert [url for url, _ in sent] == [expected]
    assert "handoff_id" not in sent[0][1]


async def test_company_copy_names_removed(real_engine, real_client):
    """No shipped code or copy uses the old company copy names, and the old API is gone."""
    patterns = ["company_copy", "company-copies", "company-copy", "Independent company copy", "handoff_id", "open-copy"]
    args = ["git", "grep", "-n", "-I", "-F"]
    for p in patterns:
        args += ["-e", p]
    args += ["--", ".", ":(exclude)tests", ":(exclude,glob)**/migrations/**"]
    found = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True)
    assert found.returncode == 1, found.stdout or found.stderr

    _, _, tok = await _bk_setup(real_engine)
    for method, path in (("post", "/company-copies"), ("post", "/company-copies/read"),
                         ("post", "/company-copies/open"), ("get", "/company-copies")):
        r = await getattr(real_client, method)(path, headers=auth(tok))
        assert r.status_code == 404, (path, r.status_code)


async def test_export_has_no_preparer_input(real_engine, real_client, tmp_path, monkeypatch):
    """The download takes no preparer: only an optional migration run id, and a supplied name is ignored."""
    from celerp.main import app
    _bk_local(monkeypatch, tmp_path)
    # The OpenAPI schema lists every registered route whatever Starlette's route-table layout.
    operations = app.openapi()["paths"].get("/company-backups/download", {})
    assert list(operations) == ["get"]
    assert [p["name"] for p in operations["get"].get("parameters", [])] == ["run_id"]
    assert "requestBody" not in operations["get"]
    _, _, tok = await _bk_setup(real_engine)
    data = await download(real_client, tok, prepared_by="Someone Else")
    assert b"Someone Else" not in b"".join(members(data).values())
    assert not manifest(data).get("provenance")


async def test_download_uses_celerp_company_extension(real_engine, real_client, tmp_path, monkeypatch):
    """The downloaded file is named .celerp-company and declares the company backup format."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine)
    r = await real_client.get("/company-backups/download", headers=auth(tok))
    assert r.status_code == 200, r.text
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert re.search(r'filename="?[^";]+\.celerp-company"?', disposition), disposition
    assert cb.EXTENSION == ".celerp-company"
    m = manifest(r.content)
    assert (m["format"], m["format_version"]) == (cb.FORMAT, cb.FORMAT_VERSION) == ("celerp-company-backup", 1)


async def test_migration_download_carries_provenance(real_engine, real_client, tmp_path, monkeypatch):
    """A download from a completed migration records who prepared it and the source system."""
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    run = await _bk_run(real_engine, user, cid)
    data = await download(real_client, tok, run_id=str(run))
    m = manifest(data)
    assert m["provenance"] == {"prepared_by": "Example Accounting", "source_system": "fake_source"}
    assert m["company"]["id"] == str(cid)
    assert not manifest(await download(real_client, tok)).get("provenance")

    running = await _bk_run(real_engine, user, cid, status="running", marker="second")
    r = await real_client.get("/company-backups/download", params={"run_id": str(running)}, headers=auth(tok))
    assert r.status_code >= 400


# ── What a backup carries ────────────────────────────────────────────────────

async def test_backup_carries_enabled_module_state(real_engine, real_client, tmp_path, monkeypatch):
    """The company's enabled modules are recorded in the backup and enabled again on restore."""
    from celerp.modules.registry import get_enabled
    _bk_local(monkeypatch, tmp_path)
    enabled = ["celerp-inventory", "celerp-labels"]
    _, _, tok = await _bk_setup(real_engine, settings={"currency": "THB", "enabled_modules": enabled})
    data = await download(real_client, tok)
    m = manifest(data)
    assert sorted(m["modules"]["enabled"]) == enabled
    assert set(m["modules"]["versions"]) >= set(enabled)
    new = await _bk_restore_new(real_client, tok, data)
    assert get_enabled(await _bk_settings(real_engine, new)) == set(enabled)


async def test_backup_carries_portable_state_only(real_engine, real_client, tmp_path, monkeypatch):
    """A backup holds the manifest, one member per portable table and attachment files, and
    the company's settings without installation or person keys."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    settings = {"currency": "THB", "role_grants": {"clerk": ["grant-x"]}, "ai_memory": ["memory-x"],
                "lock_date_set_by": "someone", "reorder_alert_email": "alerts@example.com",
                "column_prefs": {"items": ["name"]}, "pay_tip_shown": True, "reorder_last_scan_at": "2026-01-01",
                "restored_backup": {"backup_id": "older"}}
    user, cid, tok = await _bk_setup(real_engine, settings=settings)
    await _bk_seed_portable(real_engine, cid, "alpha-marker")
    url = _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo")
    await _bk_point_at(real_engine, cid, url)
    data = await download(real_client, tok)
    m, parts = manifest(data), members(data)
    async with maker(real_engine)() as s:
        portable = await cb.classify(s)
    assert set(m["tables"]) == set(portable) == set(cb.PORTABLE_TABLES)
    expected = {"manifest.json", *(f"tables/{t}.jsonl" for t in portable), "attachments/photo.png"}
    assert set(parts) == expected
    assert m["company"]["settings"]["currency"] == "THB"
    for key in _BK_DROPPED_SETTINGS:
        assert key not in m["company"]["settings"], key
    for t in portable:
        lines = [line for line in parts[f"tables/{t}.jsonl"].split(b"\n") if line.strip()]
        assert len(lines) == m["tables"][t]["rows"] >= 1, t
    for secret in ("grant-x", "memory-x", "alerts@example.com"):
        assert secret.encode() not in b"".join(parts.values()), secret


async def test_no_cross_company_rows(real_engine, real_client, tmp_path, monkeypatch):
    """A backup of one company carries no row, setting, name or file of another company."""
    _bk_local(monkeypatch, tmp_path)
    user, a, tok = await _bk_setup(real_engine, settings={"currency": "THB"})
    b = await company(real_engine, user, "Beta Trading", "beta-marker", settings={"currency": "USD-beta"})
    await _bk_seed_portable(real_engine, a, "alpha-marker")
    await _bk_seed_portable(real_engine, b, "beta-marker")
    _bk_local_file(tmp_path, b, "beta.png", b"beta-file")
    data = await download(real_client, tok)
    parts = members(data)
    body = b"".join(parts.values())
    assert b"alpha-marker" in body and b"Alpha Trading" in body
    for foreign in (b"beta-marker", b"Beta Trading", b"USD-beta", b"beta-file", str(b).encode()):
        assert foreign not in body, foreign
    for name, content in parts.items():
        if name.startswith("tables/"):
            for line in filter(None, content.split(b"\n")):
                row = json.loads(line)
                if "company_id" in row:
                    assert row["company_id"] == str(a), name


async def test_excluded_installation_state(real_engine, real_client, tmp_path, monkeypatch):
    """Users, passwords, emails and memberships never leave with a backup, and a restore
    brings in no user of the source installation."""
    from celerp.models.company import User
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    clerk = await owner(real_engine, email="clerk@example.com", name="Clerk")
    await member(real_engine, clerk, cid, role="manager")
    async with maker(real_engine)() as s:
        auth_hash = (await s.get(User, user)).auth_hash
        clerk_hash = (await s.get(User, clerk)).auth_hash
    data = await download(real_client, tok)
    body = b"".join(members(data).values())
    for secret in (OWNER_EMAIL, "clerk@example.com", auth_hash, clerk_hash, str(user), str(clerk)):
        assert secret.encode() not in body, secret
    tables = set(manifest(data)["tables"])
    assert not tables & set(cb.EXCLUDED_TABLES)
    assert not tables & {"users", "user_companies", "companies"}

    new = await _bk_restore_new(real_client, tok, data)
    assert await count(real_engine, "user_companies", "company_id = :c", c=uuid.UUID(new)) == 1
    assert await count(real_engine, "user_companies", "company_id = :c AND user_id = :u AND role = 'owner'",
                       c=uuid.UUID(new), u=user) == 1
    assert await count(real_engine, "users") == 2


async def test_imported_items_travel_and_import_again_after_restore(real_engine, real_client, tmp_path, monkeypatch):
    """Items brought in by a file import come back with their prices in the restored company,
    the import history stays behind, and the same import can run again in the restored company."""
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    rows = [{"sku": "IMP-1", "name": "Imported one", "sell_by": "piece", "retail_price": 120},
            {"sku": "IMP-2", "name": "Imported two", "sell_by": "piece", "retail_price": 80}]
    r = await real_client.post("/items/import/rows", json={"rows": rows, "idempotency_key": "op-1"},
                               headers=auth(tok))
    assert r.status_code == 200 and r.json()["created"] == 2 and not r.json()["errors"], r.text
    assert await count(real_engine, "import_batches", "company_id = :c", c=cid) == 1

    new = uuid.UUID(await _bk_restore_new(real_client, tok, await download(real_client, tok)))
    assert await count(real_engine, "import_batches", "company_id = :c", c=new) == 0
    new_tok = await token(real_engine, (await _bk_scalar(
        real_engine, "SELECT user_id FROM user_companies WHERE company_id = :c", c=new)), new)
    items = (await real_client.get("/items", headers=auth(new_tok))).json()["items"]
    assert sorted((i["sku"], i["retail_price"]) for i in items if str(i.get("sku")).startswith("IMP-")) == [("IMP-1", 120), ("IMP-2", 80)]

    again = [{**row, "retail_price": row["retail_price"] + 1} for row in rows]
    r = await real_client.post("/items/import/rows", json={"rows": again, "upsert": True, "idempotency_key": "op-1"},
                               headers=auth(new_tok))
    assert r.status_code == 200 and r.json()["updated"] == 2 and not r.json()["errors"], r.text
    items = (await real_client.get("/items", headers=auth(new_tok))).json()["items"]
    assert sorted((i["sku"], i["retail_price"]) for i in items if str(i.get("sku")).startswith("IMP-")) == [("IMP-1", 121), ("IMP-2", 81)]
    items = (await real_client.get("/items", headers=auth(tok))).json()["items"]
    assert sorted((i["sku"], i["retail_price"]) for i in items if str(i.get("sku")).startswith("IMP-")) == [("IMP-1", 120), ("IMP-2", 80)]


async def _bk_add_connector(engine, cid, user, marker):
    from celerp.models.connector_config import ConnectorConfig
    async with maker(engine)() as s:
        s.add(ConnectorConfig(company_id=str(cid), connector="shopify", webhook_secret=marker))
        await s.commit()


async def _bk_add_outbound(engine, cid, user, marker):
    from celerp.models.connector_config import OutboundQueue
    async with maker(engine)() as s:
        s.add(OutboundQueue(company_id=str(cid), connector="shopify", entity_type="item", entity_id="item:1",
                            payload_json=json.dumps({"note": marker})))
        await s.commit()


async def _bk_add_share(engine, cid, user, marker):
    from celerp.models.share import DocShareToken
    async with maker(engine)() as s:
        s.add(DocShareToken(token=marker, company_id=cid, entity_id="item:1"))
        await s.commit()


async def _bk_add_notification(engine, cid, user, marker):
    from celerp.models.notification import Notification
    async with maker(engine)() as s:
        s.add(Notification(company_id=cid, user_id=user, category="system", title=marker, body=marker))
        await s.commit()


async def _bk_add_conversation(engine, cid, user, marker):
    from celerp.models.ai import AIConversation
    async with maker(engine)() as s:
        s.add(AIConversation(company_id=cid, user_id=user, title=marker))
        await s.commit()


async def _bk_add_ai_job(engine, cid, user, marker):
    from celerp.models.ai import AIBatchJob
    async with maker(engine)() as s:
        s.add(AIBatchJob(company_id=cid, user_id=user, total_files=1, query=marker, file_ids=[marker]))
        await s.commit()


async def _bk_add_import_batch(engine, cid, user, marker):
    from celerp.models.import_batch import ImportBatch
    async with maker(engine)() as s:
        s.add(ImportBatch(company_id=cid, entity_type="item", filename=marker, row_count=1,
                          entity_ids=["item:1"], idempotency_keys=[marker]))
        await s.commit()


async def _bk_add_migration_run(engine, cid, user, marker):
    await _bk_run(engine, user, cid, prepared_by=marker, marker=marker)


async def _bk_add_membership(engine, cid, user, marker):
    clerk = await owner(engine, email=f"{marker}@example.com", name=marker)
    await member(engine, clerk, cid, role="manager")


_BK_CATEGORIES = {
    "connector_configs": _bk_add_connector,
    "outbound_queue": _bk_add_outbound,
    "doc_share_tokens": _bk_add_share,
    "notifications": _bk_add_notification,
    "ai_conversations": _bk_add_conversation,
    "ai_batch_jobs": _bk_add_ai_job,
    "import_batches": _bk_add_import_batch,
    "migration_runs": _bk_add_migration_run,
    "user_companies": _bk_add_membership,
}


@pytest.mark.parametrize("table", sorted(_BK_CATEGORIES))
async def test_each_installation_state_category_excluded(real_engine, real_client, tmp_path, monkeypatch, table):
    """Each kind of installation-owned state is excluded from the backup and absent after restore."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    marker = f"excluded{uuid.uuid4().hex[:12]}"
    try:
        await _BK_CATEGORIES[table](real_engine, cid, user, marker)
        assert table in cb.EXCLUDED_TABLES and cb.EXCLUDED_TABLES[table]
        data = await download(real_client, tok)
        parts = members(data)
        assert table not in manifest(data)["tables"]
        assert f"tables/{table}.jsonl" not in parts
        assert marker.encode() not in b"".join(parts.values())
        new = await _bk_restore_new(real_client, tok, data)
        expected = 1 if table == "user_companies" else 0
        assert await count(real_engine, table, "company_id::text = :c", c=new) == expected
    finally:
        await _bk_sql(real_engine, "DELETE FROM outbound_queue WHERE company_id = :c", c=str(cid))


async def test_connectors_and_share_tokens_excluded(real_engine, real_client, tmp_path, monkeypatch):
    """Connector credentials and share links stay behind: absent from the file and from the restored company."""
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    await _bk_add_connector(real_engine, cid, user, "whsec-connector-secret")
    await _bk_add_share(real_engine, cid, user, "share-token-secret")
    data = await download(real_client, tok)
    body = b"".join(members(data).values())
    assert b"whsec-connector-secret" not in body and b"share-token-secret" not in body
    new = await _bk_restore_new(real_client, tok, data)
    assert await count(real_engine, "connector_configs", "company_id = :c", c=new) == 0
    assert await count(real_engine, "doc_share_tokens", "company_id = :c", c=uuid.UUID(new)) == 0
    assert await count(real_engine, "connector_configs", "company_id = :c", c=str(cid)) == 1
    assert await count(real_engine, "doc_share_tokens", "company_id = :c", c=cid) == 1


# ── Classification ───────────────────────────────────────────────────────────

def _bk_import_bundled_models() -> None:
    """Import every bundled module's model files so each model registers with the metadata."""
    base = REPO_ROOT / "default_modules"
    for outer in sorted(p for p in base.iterdir() if p.is_dir() and (p / "__init__.py").exists()):
        for inner in sorted(p for p in outer.iterdir() if p.is_dir() and (p / "__init__.py").exists()):
            for model in sorted(inner.glob("models*.py")):
                if str(outer) not in sys.path:
                    sys.path.insert(0, str(outer))
                importlib.import_module(f"{inner.name}.{model.stem}")


async def test_every_company_table_classified_full_schema(real_engine):
    """Every company table in the shipped schema, bundled modules included, is portable,
    excluded with a reason, or owned by a module prefix, and exactly one of these."""
    from celerp.models.base import Base
    from celerp.modules.importer import valid_table_prefixes
    from celerp.services.migrations import company_tables
    cb = _bk_cb()
    _bk_import_bundled_models()
    async with maker(real_engine)() as s:
        database = set(await company_tables(s))
        portable = await cb.classify(s)
    declared = {name for name, table in Base.metadata.tables.items() if "company_id" in table.columns}
    prefixes = tuple(valid_table_prefixes().values())
    for table in sorted(database | declared):
        groups = [table in cb.PORTABLE_TABLES, table in cb.EXCLUDED_TABLES,
                  bool(prefixes) and table.startswith(prefixes)]
        assert sum(groups) == 1, (table, groups)
    assert all(isinstance(r, str) and r.strip() for r in cb.EXCLUDED_TABLES.values())
    assert set(cb.PORTABLE_TABLES) <= database
    assert set(portable) == set(cb.PORTABLE_TABLES) | {t for t in database if prefixes and t.startswith(prefixes)}


async def test_no_table_in_both_groups():
    """No table is both portable and excluded, and neither list claims a module-prefix table."""
    from celerp.modules.importer import valid_table_prefixes
    cb = _bk_cb()
    assert isinstance(cb.PORTABLE_TABLES, frozenset) and isinstance(cb.EXCLUDED_TABLES, dict)
    assert not set(cb.PORTABLE_TABLES) & set(cb.EXCLUDED_TABLES)
    prefixes = tuple(valid_table_prefixes().values())
    if prefixes:
        assert not [t for t in (*cb.PORTABLE_TABLES, *cb.EXCLUDED_TABLES) if t.startswith(prefixes)]
    assert set(cb.PORTABLE_TABLES) == {"locations", "work_centers", "ledger", "projections", "accounts",
                                       "bank_accounts", "reconciliation_sessions", "reconciliation_rules",
                                       "bank_statement_lines", "label_templates"}


async def test_export_refuses_unclassified_table(real_engine, real_client, tmp_path, monkeypatch):
    """A company table nobody classified, holding the company's rows, stops the export
    without naming the table, and no file is written."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    await _bk_sql(real_engine, "CREATE TABLE bk_unknown_things (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, note text)")
    await _bk_sql(real_engine, "INSERT INTO bk_unknown_things (id, company_id) VALUES (gen_random_uuid(), :c)",
                  c=str(cid))
    try:
        r = await real_client.get("/company-backups/download", headers=auth(tok))
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == cb.UNSUPPORTED
        async with maker(real_engine)() as s:
            assert "bk_unknown_things" not in await cb.classify(s)
        out = tmp_path / "bk-out" / "books.celerp-company"
        out.parent.mkdir()
        with pytest.raises(cb.BackupError):
            await cb.export_company_snapshot(cid, out)
        assert list(out.parent.iterdir()) == []
    finally:
        await _bk_drop(real_engine, "bk_unknown_things")


@pytest.mark.parametrize("reason", ["unclassified_table", "unreadable_attachment"])
async def test_export_refusal_surfaces_on_settings_and_migration_download(real_engine, real_client, tmp_path,
                                                                        monkeypatch, reason):
    """An export refusal reaches both the Settings download and the migration completion download."""
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    run = await _bk_run(real_engine, user, cid)
    if reason == "unclassified_table":
        await _bk_sql(real_engine, "CREATE TABLE bk_unknown_things (id uuid primary key, "
                                   "company_id uuid not null references companies(id) on delete cascade)")
        await _bk_sql(real_engine, "INSERT INTO bk_unknown_things (id, company_id) VALUES (gen_random_uuid(), :c)",
                      c=str(cid))
        named = "cannot back up yet"
    else:
        named = f"/static/attachments/{cid}/missing.png"
        await _bk_point_at(real_engine, cid, named)
    try:
        for params in ({}, {"run_id": str(run)}):
            r = await real_client.get("/company-backups/download", params=params or None, headers=auth(tok))
            assert r.status_code == 409, (params, r.text)
            detail = r.json()["detail"]
            assert named in detail and detail.endswith("Nothing was backed up."), detail
    finally:
        await _bk_drop(real_engine, "bk_unknown_things")


async def test_module_prefix_table_round_trip(real_engine, real_client, tmp_path, monkeypatch):
    """A third-party module's prefix table in the normal shape travels with the backup and is restored."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    user, cid, tok = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _bk_sql(real_engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, note text)")
    try:
        old = uuid.uuid4()
        await _bk_sql(real_engine, "INSERT INTO zz_widgets (id, company_id, note) VALUES (:i, :c, 'widget-marker')",
                      i=old, c=cid)
        async with maker(real_engine)() as s:
            assert "zz_widgets" in await cb.classify(s)
        data = await download(real_client, tok)
        assert manifest(data)["tables"]["zz_widgets"]["rows"] == 1
        assert b"widget-marker" in members(data)["tables/zz_widgets.jsonl"]
        new = await _bk_restore_new(real_client, tok, data)
        async with real_engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id, note FROM zz_widgets WHERE company_id = :c"),
                                       {"c": new})).all()
        assert [r.note for r in rows] == ["widget-marker"] and rows[0].id != old
    finally:
        await _bk_drop(real_engine, "zz_widgets")


def _bk_shadow_module(tmp_path, name: str, prefix: str) -> None:
    """A module copied by hand next to the fake one (it never passed the install
    check), whose manifest claims the fake module's table and leaves it out of backups."""
    pkg = tmp_path / "bk-modules" / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        f'PLUGIN_MANIFEST = {{"name": "{name}", "version": "1.0.0", "table_prefix": "{prefix}", '
        '"company_backup": {"zz_widgets": "exclude"}}\n')


async def test_malformed_hand_copied_prefix_does_not_take_over_a_module_table(real_engine, real_client, tmp_path,
                                                                              monkeypatch):
    """A hand-copied module with a malformed prefix owns nothing: the sound module keeps its table
    and the table still travels with the backup."""
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    _bk_shadow_module(tmp_path, "zz-shadow", "zz_wid")
    _, cid, tok = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _bk_sql(real_engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, note text)")
    try:
        await _bk_sql(real_engine, "INSERT INTO zz_widgets (id, company_id, note) VALUES (:i, :c, 'kept')",
                      i=uuid.uuid4(), c=cid)
        data = await download(real_client, tok)
        assert manifest(data)["tables"]["zz_widgets"]["rows"] == 1
    finally:
        await _bk_drop(real_engine, "zz_widgets")


async def test_hand_copied_prefix_claiming_a_core_table_owns_nothing(real_engine, real_client, tmp_path,
                                                                       monkeypatch):
    """A hand-copied module whose prefix reaches Celerp's own schema stamp is not its owner:
    the stamp is never attributed to the module, so its say-so neither stops nor shapes
    the backup."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    pkg = tmp_path / "bk-modules" / "zz-stamper"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        'PLUGIN_MANIFEST = {"name": "zz-stamper", "version": "1.0.0", "table_prefix": "alembic_", '
        '"company_backup": {"alembic_version": "include"}}\n')
    _, cid, tok = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    async with real_engine.connect() as conn:
        stamped = (await conn.execute(text("SELECT to_regclass('alembic_version')"))).scalar() is not None
    if not stamped:
        await _bk_sql(real_engine, "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
    try:
        async with maker(real_engine)() as s:
            plan = await cb._classify(s, strict=False)
        assert "alembic_version" not in plan.owners and "alembic_version" not in plan.order
        data = await download(real_client, tok)
        assert "alembic_version" not in manifest(data)["tables"]
    finally:
        if not stamped:
            await _bk_drop(real_engine, "alembic_version")


@pytest.mark.parametrize("prefix", ["label_", "marketplace_", "bank_"])
async def test_hand_copied_prefix_claiming_a_turned_off_bundled_module_table_owns_nothing(
        real_engine, tmp_path, monkeypatch, bundled_modules_unloaded, prefix):
    """A bundled module's tables are never attributed to a hand-copied module claiming
    them, even while the bundled module is turned off."""
    from celerp.modules.importer import valid_table_prefixes
    cb = _bk_cb()
    _bk_fake_module(tmp_path, monkeypatch)
    _bk_shadow_module(tmp_path, "zz-claimer", prefix)
    assert "zz-claimer" not in valid_table_prefixes()
    async with maker(real_engine)() as s:
        plan = await cb._classify(s, strict=False)
    assert plan.owners.get(bundled_modules_unloaded[prefix]) != "zz-claimer"
    assert "zz-claimer" not in plan.owners.values()


async def test_overlapping_hand_copied_prefix_stops_the_export_instead_of_dropping_a_table(
        real_engine, real_client, tmp_path, monkeypatch):
    """Two modules whose sound prefixes overlap own nothing, so the table is refused by name
    rather than silently left out of the backup on the copied module's say-so."""
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    _bk_shadow_module(tmp_path, "zz-zshadow", _BK_PREFIX)
    _, cid, tok = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _bk_sql(real_engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, note text)")
    try:
        r = await real_client.get("/company-backups/download", headers=auth(tok))
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "zz_widgets" in detail and detail.endswith("Nothing was backed up."), detail
    finally:
        await _bk_drop(real_engine, "zz_widgets")


_BK_BAD_SHAPES = {
    "no_company_id": "CREATE TABLE zz_widgets (id uuid primary key, note text)",
    "serial_key": "CREATE TABLE zz_widgets (id serial primary key, "
                  "company_id uuid not null references companies(id) on delete cascade, note text)",
    "composite_key": "CREATE TABLE zz_widgets (a uuid, b uuid, "
                     "company_id uuid not null references companies(id) on delete cascade, primary key (a, b))",
}


@pytest.mark.parametrize("shape", sorted(_BK_BAD_SHAPES))
async def test_module_table_outside_invariants_refused(real_engine, real_client, tmp_path, monkeypatch, shape):
    """A module prefix table the generic engine cannot carry stops the export, naming the module, never the table."""
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    _, _, tok = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _bk_sql(real_engine, _BK_BAD_SHAPES[shape])
    try:
        r = await real_client.get("/company-backups/download", headers=auth(tok))
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "Widgets" in detail and "zz_" not in detail and detail.endswith("Nothing was backed up.")
    finally:
        await _bk_drop(real_engine, "zz_widgets")


async def test_unsupported_module_refusal_names_module_and_table_before_archive(real_engine, tmp_path, monkeypatch):
    """The exporter refuses an unsupported module table before it opens any archive or writes any file."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    user = await owner(real_engine)
    cid = await company(real_engine, user, "Alpha Trading", "alpha-marker", settings={"enabled_modules": [_BK_MODULE]})
    await _bk_sql(real_engine, _BK_BAD_SHAPES["serial_key"])
    opened: list[str] = []
    real_init = zipfile.ZipFile.__init__

    def spying_init(self, file, mode="r", *args, **kw):
        if mode != "r":
            opened.append(str(file))
        real_init(self, file, mode, *args, **kw)
    monkeypatch.setattr(zipfile.ZipFile, "__init__", spying_init)
    out_dir = tmp_path / "bk-out"
    out_dir.mkdir()
    try:
        with pytest.raises(cb.BackupError) as err:
            await cb.export_company_snapshot(cid, out_dir / "books.celerp-company")
        assert err.value.status_code == 409
        assert "Widgets" in err.value.detail and "zz_" not in err.value.detail
        assert err.value.detail.endswith("Nothing was backed up.")
        assert opened == [] and list(out_dir.iterdir()) == []
    finally:
        await _bk_drop(real_engine, "zz_widgets")


_BK_LOOPS = {
    "self": ["ALTER TABLE zz_widgets ADD COLUMN parent_id uuid REFERENCES zz_widgets(id)"],
    "cycle": ["ALTER TABLE zz_widgets ADD COLUMN gadget_id uuid REFERENCES zz_gadgets(id)"],
}


@pytest.mark.parametrize("loop", sorted(_BK_LOOPS))
async def test_module_tables_referencing_in_a_loop_refused(real_engine, real_client, tmp_path, monkeypatch, loop):
    """Rows of a table that references itself, or of tables referencing each other, cannot be
    inserted parents first, so such tables are neither backed up nor restored, and nothing is written."""
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    user, cid, tok = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _bk_sql(real_engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade)")
    await _bk_sql(real_engine, "CREATE TABLE zz_gadgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, "
                               "widget_id uuid references zz_widgets(id))")
    try:
        widget = uuid.uuid4()
        await _bk_sql(real_engine, "INSERT INTO zz_widgets (id, company_id) VALUES (:i, :c)", i=widget, c=cid)
        await _bk_sql(real_engine, "INSERT INTO zz_gadgets (id, company_id, widget_id) VALUES (:i, :c, :w)",
                      i=uuid.uuid4(), c=cid, w=widget)
        data = await download(real_client, tok)
        assert {"zz_widgets", "zz_gadgets"} <= set(manifest(data)["tables"])
        for sql in _BK_LOOPS[loop]:
            await _bk_sql(real_engine, sql)
        r = await real_client.get("/company-backups/download", headers=auth(tok))
        assert r.status_code == 409, r.text
        assert "Widgets" in r.json()["detail"] and "zz_" not in r.json()["detail"]
        await _bk_refused(real_engine, real_client, tok, user, tmp_path, data)
    finally:
        await _bk_drop(real_engine, "zz_gadgets", "zz_widgets")


async def test_insert_order_from_foreign_keys(real_engine, tmp_path, monkeypatch):
    """Tables come out parents first for every foreign key between two backed-up tables."""
    cb = _bk_cb()
    _bk_fake_module(tmp_path, monkeypatch)
    # zz_gadgets sorts before zz_widgets but references it, so only a foreign-key order passes.
    await _bk_sql(real_engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade)")
    await _bk_sql(real_engine, "CREATE TABLE zz_gadgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, "
                               "widget_id uuid references zz_widgets(id))")
    try:
        async with maker(real_engine)() as s:
            order = await cb.classify(s)
            edges = (await s.execute(text(
                "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint WHERE contype = 'f'"
            ))).all()
        assert len(order) == len(set(order))
        assert {"zz_widgets", "zz_gadgets"} <= set(order)
        position = {t: i for i, t in enumerate(order)}
        checked = 0
        for child, parent in edges:
            child, parent = child.strip('"'), parent.strip('"')
            if child in position and parent in position and child != parent:
                assert position[parent] < position[child], (parent, child)
                checked += 1
        assert checked >= 3
    finally:
        await _bk_drop(real_engine, "zz_gadgets", "zz_widgets")


# ── Bounded work ─────────────────────────────────────────────────────────────

async def test_export_streams_rows_in_batches(real_engine, real_client, tmp_path, monkeypatch):
    """No single export fetch reads more than BATCH_ROWS rows of a table."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    monkeypatch.setattr(cb, "BATCH_ROWS", 2)
    _, cid, tok = await _bk_setup(real_engine)
    await _bk_extra_ledger(real_engine, cid, 5, "batch")
    for table in ("ledger", "projections"):
        with _BkSqlSpy(table) as spy:
            data = await download(real_client, tok)
        assert manifest(data)["tables"][table]["rows"] == 6
        assert spy.fetches and max(spy.fetches) <= 2, (table, spy.fetches)
        assert sum(spy.fetches) >= 6, (table, spy.fetches)


async def test_import_inserts_rows_in_batches(real_engine, real_client, tmp_path, monkeypatch):
    """No single restore insert writes more than BATCH_ROWS rows of a table."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    await _bk_extra_ledger(real_engine, cid, 5, "batch")
    data = await download(real_client, tok)
    monkeypatch.setattr(cb, "BATCH_ROWS", 2)
    with _BkSqlSpy("ledger") as ledger_spy, _BkSqlSpy("projections") as projection_spy:
        new = await _bk_restore_new(real_client, tok, data)
    for spy in (ledger_spy, projection_spy):
        assert spy.inserts and max(spy.inserts) <= 2, spy.inserts
        assert sum(spy.inserts) >= 6, spy.inserts
    assert await count(real_engine, "ledger", "company_id = :c", c=uuid.UUID(new)) == 6


async def test_export_and_restore_batches_end_at_the_byte_budget(real_engine, real_client, tmp_path, monkeypatch):
    """A batch ends at BATCH_BYTES of JSON as well as at BATCH_ROWS rows: with a budget
    smaller than one row, every export fetch and restore insert carries one row."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    await _bk_extra_ledger(real_engine, cid, 5, "wide")
    monkeypatch.setattr(cb, "BATCH_BYTES", 1, raising=False)
    with _BkSqlSpy("ledger") as spy:
        data = await download(real_client, tok)
    assert manifest(data)["tables"]["ledger"]["rows"] == 6
    assert spy.fetches and max(spy.fetches) == 1, spy.fetches
    assert sum(spy.fetches) == 6, spy.fetches
    with _BkSqlSpy("ledger") as spy:
        new = await _bk_restore_new(real_client, tok, data)
    assert spy.inserts and max(spy.inserts) == 1, spy.inserts
    assert await count(real_engine, "ledger", "company_id = :c", c=uuid.UUID(new)) == 6


def test_restore_batch_memory_stays_bounded_for_a_large_member(tmp_path, monkeypatch):
    """Reading a 16 MB table member of wide rows holds about one byte budget at a time,
    not a full BATCH_ROWS of rows."""
    import tracemalloc
    import zipfile
    cb = _bk_cb()
    budget = 1024 * 1024
    monkeypatch.setattr(cb, "BATCH_BYTES", budget, raising=False)
    path = tmp_path / "wide.zip"
    row = (json.dumps({"id": "x", "pad": "p" * (64 * 1024)}) + "\n").encode()
    with zipfile.ZipFile(path, "w") as zf, zf.open("tables/ledger.jsonl", "w") as fh:
        for _ in range(256):
            fh.write(row)
    with zipfile.ZipFile(path) as zf:
        tracemalloc.start()
        try:
            rows = 0
            for batch in cb._row_batches(cb._lines(zf, "tables/ledger.jsonl"), cb.BATCH_ROWS):
                rows += len(batch)
                del batch
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert rows == 256
    assert peak < 4 * budget, peak


def test_restore_memory_bounded_for_rows_of_empty_containers(tmp_path):
    """Parsed JSON can take about fifty times its text (a row of empty objects), so a row is
    bounded by its parsed nodes as well as its bytes. A row at MAX_ROW_BYTES of empty
    objects is refused before it is parsed, and the restore row path stays under 32 MB."""
    import tracemalloc
    import zipfile
    cb = _bk_cb()
    assert cb.MAX_ROW_BYTES == 8 * 1024 ** 2
    head, tail = b'{"id":1,"state":[', b"{}]}\n"
    row = head + b"{}," * ((cb.MAX_ROW_BYTES - len(head) - len(tail)) // 3) + tail
    assert len(row) <= cb.MAX_ROW_BYTES
    path = tmp_path / "amplified.zip"
    with zipfile.ZipFile(path, "w") as zf, zf.open("tables/ledger.jsonl", "w") as fh:
        fh.write(row)
    with zipfile.ZipFile(path) as zf:
        tracemalloc.start()
        try:
            with pytest.raises(cb.BackupError) as err:
                for batch in cb._row_batches(cb._lines(zf, "tables/ledger.jsonl"), cb.BATCH_ROWS):
                    cb._dump_rows(cb.remap(batch, {}))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert err.value.detail == cb.TOO_LARGE
    assert peak < 32 * 1024 ** 2, peak


def test_deeply_nested_row_is_refused_before_it_is_parsed():
    """A row nested deeper than MAX_ROW_DEPTH is refused as too large instead of failing
    while it is walked; brackets inside strings do not count."""
    cb = _bk_cb()
    deep = b'{"id":1,"state":' + b"[" * 5000 + b"]" * 5000 + b"}"
    with pytest.raises(cb.BackupError) as err:
        cb._parse_row(deep)
    assert err.value.detail == cb.TOO_LARGE
    text = b'{"id":1,"state":"' + b"[" * 5000 + b'"}'
    assert cb._parse_row(text)["state"] == "[" * 5000


async def test_company_with_a_large_document_backs_up_and_restores(real_engine, real_client, tmp_path, monkeypatch):
    """An ordinary invoice of 5,500 lines (over 1 MB as a row) is backed up and restored whole."""
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine)
    lines = [{"sku": f"GEM-{i:05d}", "name": "Blue sapphire oval cut 1.02ct",
              "description": "Natural blue sapphire, oval, heated, 6.8x5.1x3.4 mm",
              "quantity": 1, "unit_price": 1250, "line_total": 1250} for i in range(5500)]
    r = await real_client.post("/docs", headers=auth(tok), json={"doc_type": "invoice", "line_items": lines,
                                                                 "total": 1250 * 5500})
    assert r.status_code == 200, r.text
    data = await download(real_client, tok)
    assert max(len(line) for n, body in members(data).items() if n.startswith("tables/")
               for line in body.splitlines()) > 1024 ** 2
    new = await _bk_restore_new(real_client, tok, data)
    restored = await _bk_scalar(real_engine, "SELECT json_array_length(state->'line_items') FROM projections "
                                "WHERE company_id = :c AND state->>'doc_type' = 'invoice'", c=new)
    assert restored == 5500


async def test_attachments_processed_one_at_a_time(real_engine, real_client, tmp_path, monkeypatch):
    """Export writes each attachment before reading the next, and restore stores them one at a time."""
    from celerp.services import attachments
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    bodies = {f"photo{i}.png": f"photo-body-{i}".encode() for i in range(3)}
    urls = [_bk_local_file(tmp_path, cid, name, body) for name, body in bodies.items()]
    await _bk_set_data(real_engine, cid, {"attachments": [{"url": u} for u in urls]})

    events: list[tuple[str, str]] = []
    reading = {"now": 0, "max": 0}
    real_read = attachments.read_company_file

    async def spy_read(company_id, url, max_bytes):
        reading["now"] += 1
        reading["max"] = max(reading["max"], reading["now"])
        try:
            events.append(("read", url))
            return await real_read(company_id, url, max_bytes)
        finally:
            reading["now"] -= 1
    monkeypatch.setattr(attachments, "read_company_file", spy_read)
    monkeypatch.setattr(cb, "read_company_file", spy_read, raising=False)

    def arcname(value) -> str:
        return getattr(value, "filename", value)
    real_writestr, real_open, real_write = zipfile.ZipFile.writestr, zipfile.ZipFile.open, zipfile.ZipFile.write

    def spy_writestr(self, info, data, *a, **kw):
        events.append(("write", arcname(info)))
        return real_writestr(self, info, data, *a, **kw)

    def spy_open(self, name, mode="r", *a, **kw):
        if mode == "w":
            events.append(("write", arcname(name)))
        return real_open(self, name, mode, *a, **kw)

    def spy_write(self, filename, arcname_=None, *a, **kw):
        events.append(("write", arcname(arcname_ or filename)))
        return real_write(self, filename, arcname_, *a, **kw)
    monkeypatch.setattr(zipfile.ZipFile, "writestr", spy_writestr)
    monkeypatch.setattr(zipfile.ZipFile, "open", spy_open)
    monkeypatch.setattr(zipfile.ZipFile, "write", spy_write)
    data = await download(real_client, tok)
    monkeypatch.setattr(zipfile.ZipFile, "writestr", real_writestr)
    monkeypatch.setattr(zipfile.ZipFile, "open", real_open)
    monkeypatch.setattr(zipfile.ZipFile, "write", real_write)

    reads = [i for i, e in enumerate(events) if e[0] == "read"]
    assert len(reads) == 3 and reading["max"] == 1, events
    for before, after in zip(reads, reads[1:]):
        between = [name for kind, name in events[before + 1:after] if kind == "write"]
        assert any(name.startswith("attachments/") for name in between), events
    assert {n[len("attachments/"):] for n in members(data) if n.startswith("attachments/")} == set(bodies)

    storing = {"now": 0, "max": 0, "calls": []}
    real_store = attachments.store_company_file

    async def spy_store(company_id, name, content):
        storing["now"] += 1
        storing["max"] = max(storing["max"], storing["now"])
        storing["calls"].append(content)
        try:
            return await real_store(company_id, name, content)
        finally:
            storing["now"] -= 1
    monkeypatch.setattr(attachments, "store_company_file", spy_store)
    monkeypatch.setattr(cb, "store_company_file", spy_store, raising=False)
    await _bk_restore_new(real_client, tok, data)
    assert storing["max"] == 1
    assert sorted(storing["calls"]) == sorted(bodies.values())


# ── Attachments across backends ──────────────────────────────────────────────

async def test_local_attachments_round_trip(real_engine, real_client, tmp_path, monkeypatch):
    """A locally stored attachment is carried with its hash and restored under the new company."""
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    url = _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo")
    await _bk_point_at(real_engine, cid, url)
    data = await download(real_client, tok)
    m, parts = manifest(data), members(data)
    assert parts["attachments/photo.png"] == b"alpha-photo"
    [entry] = m["attachments"]
    assert (entry["url"], entry["name"], entry["size"], entry["sha256"]) == (
        url, "photo.png", len(b"alpha-photo"), sha256(b"alpha-photo"))
    new = await _bk_restore_new(real_client, tok, data)
    assert (tmp_path / "static" / "attachments" / new / "photo.png").read_bytes() == b"alpha-photo"
    assert (tmp_path / "static" / "attachments" / str(cid) / "photo.png").read_bytes() == b"alpha-photo"
    state = json.loads(await _bk_scalar(real_engine, "SELECT state::text FROM projections WHERE company_id = :c",
                                        c=uuid.UUID(new)))
    assert state["attachments"][0]["url"] == f"/static/attachments/{new}/photo.png"


async def test_fake_cloud_attachments_round_trip(real_engine, real_client, tmp_path, monkeypatch):
    """A cloud stored attachment is read and restored through the configured backend under the new company."""
    fake = _bk_cloud(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    url = fake.url(cid, "photo.png")
    fake.files[url] = b"cloud-photo"
    await _bk_point_at(real_engine, cid, url)
    data = await download(real_client, tok)
    assert members(data)["attachments/photo.png"] == b"cloud-photo"
    assert manifest(data)["attachments"][0]["sha256"] == sha256(b"cloud-photo")
    new = await _bk_restore_new(real_client, tok, data)
    assert fake.company_files(new) == {fake.url(new, "photo.png"): b"cloud-photo"}
    assert fake.files[url] == b"cloud-photo"
    state = json.loads(await _bk_scalar(real_engine, "SELECT state::text FROM projections WHERE company_id = :c",
                                        c=uuid.UUID(new)))
    assert state["attachments"][0]["url"] == fake.url(new, "photo.png")
    assert not (tmp_path / "static" / "attachments" / new).exists()


async def test_attachment_urls_rewritten_exactly(real_engine, real_client, tmp_path, monkeypatch):
    """Exact attachment URL values are replaced in projections and ledger data; other strings are untouched."""
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    url = _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo")
    elsewhere = "/static/attachments/elsewhere/photo.png"
    await _bk_set_data(real_engine, cid, {"attachments": [{"url": url}], "gallery": [[url]], "note": "photo.png",
                                          "files": {"main": url}, "mirror": elsewhere})
    new = await _bk_restore_new(real_client, tok, await download(real_client, tok))
    new_url = f"/static/attachments/{new}/photo.png"
    restored = json.loads(await _bk_scalar(real_engine, "SELECT state::text FROM projections WHERE company_id = :c",
                                           c=uuid.UUID(new)))
    data = json.loads(await _bk_scalar(real_engine, "SELECT data::text FROM ledger WHERE company_id = :c",
                                       c=uuid.UUID(new)))
    assert data == {"attachments": [{"url": new_url}], "gallery": [[new_url]], "note": "photo.png",
                    "files": {"main": new_url}, "mirror": elsewhere}
    assert restored == replayed(data)


@pytest.mark.parametrize("backend", ["local", "cloud"])
async def test_no_source_storage_url_in_restored_company(real_engine, real_client, tmp_path, monkeypatch, backend):
    """No source storage URL or source company id survives anywhere in the restored company."""
    cb = _bk_cb()
    if backend == "local":
        _bk_local(monkeypatch, tmp_path)
    else:
        fake = _bk_cloud(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    if backend == "local":
        url = _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo")
    else:
        url = fake.url(cid, "photo.png")
        fake.files[url] = b"alpha-photo"
    await _bk_point_at(real_engine, cid, url)
    new = await _bk_restore_new(real_client, tok, await download(real_client, tok))
    restored = await _bk_company_text(real_engine, new, sorted(cb.PORTABLE_TABLES))
    assert url not in restored and str(cid) not in restored
    assert "photo.png" in restored


async def test_backup_service_has_no_backend_specific_code():
    """The backup service knows nothing about where attachment bytes live."""
    source = (REPO_ROOT / "celerp" / "services" / "company_backup.py").read_text()
    for name in ("LocalBackend", "S3Backend", "company_attachment_dir", "static/attachments", "data_dir"):
        assert name not in source, name


# ── Refusals before any write ────────────────────────────────────────────────

async def test_tampered_attachment_hash_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch):
    """An attachment whose bytes no longer match the manifest hash is refused and nothing is written."""
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    await _bk_point_at(real_engine, cid, _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo"))
    parts = members(await download(real_client, tok))
    parts["attachments/photo.png"] = b"omega-photo"
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, rezip(parts), "damaged or was changed")
    assert sorted(p.name for p in (tmp_path / "static" / "attachments").iterdir()) == [str(cid)]


async def test_upload_size_limit_refused(real_engine, real_client, tmp_path, monkeypatch):
    """An upload one byte over the upload limit is refused as too large and nothing is written."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine)
    data = await download(real_client, tok)
    monkeypatch.setattr(cb, "MAX_UPLOAD_BYTES", len(data) - 1)
    before = await snapshot(real_engine)
    r = await read(real_client, tok, data)
    assert r.status_code == 413 and "too large" in r.json()["detail"], r.text
    assert await snapshot(real_engine) == before
    monkeypatch.setattr(cb, "MAX_UPLOAD_BYTES", len(data))
    assert (await read(real_client, tok, data)).status_code == 200


async def test_member_count_limit_refused(real_engine, real_client, tmp_path, monkeypatch):
    """An archive with more members than the member limit is refused and nothing is written."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    data = await download(real_client, tok)
    total = len(members(data))
    monkeypatch.setattr(cb, "MAX_MEMBERS", total - 1)
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "too large")
    monkeypatch.setattr(cb, "MAX_MEMBERS", total)
    assert (await read(real_client, tok, data)).status_code == 200


async def test_member_uncompressed_size_limit_refused(real_engine, real_client, tmp_path, monkeypatch):
    """A member larger uncompressed than the per-member limit is refused and nothing is written."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    data = await download(real_client, tok)
    largest = max(len(body) for body in members(data).values())
    monkeypatch.setattr(cb, "MAX_MEMBER_BYTES", largest - 1)
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "too large")
    monkeypatch.setattr(cb, "MAX_MEMBER_BYTES", largest)
    assert (await read(real_client, tok, data)).status_code == 200


async def test_aggregate_uncompressed_size_limit_refused(real_engine, real_client, tmp_path, monkeypatch):
    """An archive larger uncompressed in total than the aggregate limit is refused and nothing is written."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    data = await download(real_client, tok)
    total = sum(len(body) for body in members(data).values())
    monkeypatch.setattr(cb, "MAX_TOTAL_BYTES", total - 1)
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "too large")
    monkeypatch.setattr(cb, "MAX_TOTAL_BYTES", total)
    assert (await read(real_client, tok, data)).status_code == 200


@pytest.mark.parametrize("member", ["manifest", "row", "attachment"])
async def test_members_read_whole_have_their_own_limits(real_engine, real_client, tmp_path, monkeypatch, member):
    """The manifest, a single table row and an attachment are each read whole, so each is held
    to its own limit (an attachment to the ordinary attachment limit), well under the limit of
    a streamed table."""
    cb = _bk_cb()
    from celerp.services import attachments
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    await _bk_point_at(real_engine, cid, _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo" * 100))
    data = await download(real_client, tok)
    parts = members(data)
    rows = [line for n, body in parts.items() if n.startswith("tables/") for line in body.splitlines()]
    owner_of, limit, size = {"manifest": (cb, "MAX_MANIFEST_BYTES", len(parts["manifest.json"])),
                             "row": (cb, "MAX_ROW_BYTES", max(len(line) for line in rows)),
                             "attachment": (attachments, "MAX_FILE_BYTES", len(parts["attachments/photo.png"]))}[member]
    assert size < cb.MAX_MEMBER_BYTES
    monkeypatch.setattr(owner_of, limit, size - 1)
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "too large")
    monkeypatch.setattr(owner_of, limit, size)
    assert (await read(real_client, tok, data)).status_code == 200


async def test_oversized_row_refused_at_restore_insert(tmp_path, monkeypatch):
    """Rows are read one at a time within the row limit even where the archive was not checked first."""
    cb = _bk_cb()
    path = tmp_path / "rows.zip"
    path.write_bytes(rezip({"tables/t.jsonl": b"{}\n" + b"x" * 64 + b"\n"}))
    monkeypatch.setattr(cb, "MAX_ROW_BYTES", 32)
    import zipfile
    with zipfile.ZipFile(path) as zf, pytest.raises(cb.BackupError) as err:
        list(cb._lines(zf, "tables/t.jsonl"))
    assert "too large" in err.value.detail


async def test_record_too_large_to_restore_is_not_backed_up(real_engine, real_client, tmp_path, monkeypatch):
    """A company holding a record larger than a restore accepts gets a clear refusal instead of
    a backup it could not restore."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine)
    rows = [line for n, body in members(await download(real_client, tok)).items()
            if n.startswith("tables/") for line in body.splitlines()]
    monkeypatch.setattr(cb, "MAX_ROW_BYTES", max(len(line) for line in rows) - 1)
    r = await real_client.get("/company-backups/download", headers=auth(tok))
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == cb.ROW_TOO_LARGE_TO_BACK_UP


@pytest.mark.parametrize("limit", [("MAX_ROW_NODES", 3), ("MAX_ROW_DEPTH", 1)])
async def test_record_too_large_to_parse_is_not_backed_up(real_engine, real_client, tmp_path, monkeypatch, limit):
    """Export applies the restore's parsed-size limits too (values and nesting)."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine)
    monkeypatch.setattr(cb, *limit)
    r = await real_client.get("/company-backups/download", headers=auth(tok))
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == cb.ROW_TOO_LARGE_TO_BACK_UP
    assert "too large for a company backup" in r.json()["detail"]


@pytest.mark.parametrize("limit", ["MAX_MEMBERS", "MAX_MEMBER_BYTES", "MAX_TOTAL_BYTES", "MAX_UPLOAD_BYTES"])
async def test_backup_restore_would_refuse_is_not_made(real_engine, real_client, tmp_path, monkeypatch, limit):
    """Export checks the finished file against every size limit a restore applies (member
    count, member size, total size, upload size) and refuses instead of handing over a
    backup that restore would refuse."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine)
    data = await download(real_client, tok)
    parts = members(data)
    # Sizes shrink by a few bytes between two exports (timestamps), so size limits are
    # set a margin below this export rather than one byte below.
    monkeypatch.setattr(cb, limit, {"MAX_MEMBERS": len(parts) - 1,
                                    "MAX_MEMBER_BYTES": max(len(b) for b in parts.values()) - 64,
                                    "MAX_TOTAL_BYTES": sum(len(b) for b in parts.values()) - 64,
                                    "MAX_UPLOAD_BYTES": len(data) - 64}[limit])
    r = await real_client.get("/company-backups/download", headers=auth(tok))
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == cb.TOO_LARGE_TO_BACK_UP


@pytest.mark.parametrize("header", ["honest", "understated"])
async def test_zip_bomb_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch, header):
    """A small upload that inflates past the uncompressed limits is refused, whatever its headers claim."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    parts = members(await download(real_client, tok))
    original = len(parts["tables/locations.jsonl"])
    parts["tables/locations.jsonl"] = b"0" * (32 * 1024 * 1024)
    bomb = rezip(parts)
    monkeypatch.setattr(cb, "MAX_MEMBER_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(cb, "MAX_TOTAL_BYTES", 4 * 1024 * 1024)
    if header == "understated":
        bomb = _bk_lie_about_size(bomb, "tables/locations.jsonl", original)
    assert len(bomb) < 1024 * 1024 <= cb.MAX_UPLOAD_BYTES
    if header == "honest":
        await _bk_refused(real_engine, real_client, tok, user, tmp_path, bomb, "too large")
    else:
        before = await snapshot(real_engine)
        r = await read(real_client, tok, bomb)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert "too large" in detail or "damaged or was changed" in detail, detail
        assert await snapshot(real_engine) == before


_BK_MEMBER_CHANGES = {
    "duplicate_manifest": lambda p: [*p.items(), ("manifest.json", p["manifest.json"])],
    "duplicate_table": lambda p: [*p.items(), ("tables/locations.jsonl", p["tables/locations.jsonl"])],
    "unexpected_file": lambda p: [*p.items(), ("notes.txt", b"hello")],
    "unexpected_table": lambda p: [*p.items(), ("tables/users.jsonl", b"")],
    "path_traversal": lambda p: [*p.items(), ("attachments/../../evil.png", b"evil")],
}


@pytest.mark.parametrize("change", sorted(_BK_MEMBER_CHANGES))
async def test_duplicate_and_unexpected_members_refused(real_engine, real_client, tmp_path, monkeypatch, change):
    """Duplicate logical members and paths the format does not define are refused before any write."""
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    parts = members(await download(real_client, tok))
    data = _bk_raw_zip(_BK_MEMBER_CHANGES[change](parts))
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "damaged or was changed")


def _bk_changed_row(data: bytes) -> bytes:
    parts = members(data)
    parts["tables/locations.jsonl"] = parts["tables/locations.jsonl"].replace(b"alpha-marker", b"omega-marker")
    return rezip(parts)


def _bk_wrong_count(data: bytes) -> bytes:
    def change(m):
        m["tables"]["locations"]["rows"] += 1
    return _bk_edit_manifest(data, change)


def _bk_wrong_format(data: bytes) -> bytes:
    def change(m):
        m["format"] = "something-else"
    return _bk_edit_manifest(data, change)


_BK_TAMPERING = {
    "changed_row": (_bk_changed_row, "damaged or was changed"),
    "wrong_row_count": (_bk_wrong_count, "damaged or was changed"),
    "not_a_zip": (lambda _: b"this is not a backup", "not a Celerp company backup"),
    "wrong_format": (_bk_wrong_format, "not a Celerp company backup"),
    "system_backup": (lambda _: _bk_system_backup(),
                      "This is a whole-installation backup. Use System Recovery instead."),
}


@pytest.mark.parametrize("change", sorted(_BK_TAMPERING))
async def test_tampered_archives_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch, change):
    """Changed, foreign or whole-installation files are refused with a plain message and nothing is written."""
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    tamper, message = _BK_TAMPERING[change]
    data = tamper(await download(real_client, tok))
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, message)


@pytest.mark.parametrize("missing", ["tables/locations.jsonl", "attachments/photo.png"])
async def test_missing_member_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch, missing):
    """A backup missing a table or attachment member is refused and nothing is written."""
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    await _bk_point_at(real_engine, cid, _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo"))
    parts = members(await download(real_client, tok))
    assert missing in parts
    del parts[missing]
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, rezip(parts), "damaged or was changed")


def _bk_newer_version(data: bytes) -> bytes:
    def change(m):
        m["format_version"] = m["format_version"] + 1
    return _bk_edit_manifest(data, change)


def _bk_unknown_table(data: bytes) -> bytes:
    parts = members(data)
    m = json.loads(parts["manifest.json"])
    m["tables"]["future_things"] = {"columns": ["id", "company_id"], "rows": 0,
                                    "sha256": sha256(b""), "digest": sha256(b"")}
    parts["manifest.json"] = json.dumps(m).encode()
    parts["tables/future_things.jsonl"] = b""
    return rezip(parts)


@pytest.mark.parametrize("change", ["newer_format_version", "unknown_table"])
async def test_newer_schema_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch, change):
    """A backup from a newer format or carrying a table this Celerp does not know is refused before any write."""
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    data = await download(real_client, tok)
    data = _bk_newer_version(data) if change == "newer_format_version" else _bk_unknown_table(data)
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "newer version of Celerp")


@pytest.mark.parametrize("recorded", ["999.0.0", "newer_dev"])
async def test_backup_from_a_newer_celerp_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch,
                                                                recorded):
    """A backup made by a newer Celerp is refused before any write, even when its tables and
    columns all exist here: the newer copy's records may mean something this copy cannot read."""
    from packaging.version import Version
    from celerp.migrations.compatibility import running_version
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    if recorded == "newer_dev":
        v = Version(running_version())
        recorded = f"{v.major}.{v.minor}.{v.micro + 1}.dev1"

    def change(m):
        m["celerp_version"] = recorded
    data = _bk_edit_manifest(await download(real_client, tok), change)
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "newer version of Celerp")


@pytest.mark.parametrize("recorded", ["not a version", 7, ""])
async def test_backup_with_an_unreadable_celerp_version_refused_before_writes(real_engine, real_client, tmp_path,
                                                                              monkeypatch, recorded):
    """A backup whose recorded Celerp version cannot be read is refused as damaged, never guessed."""
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)

    def change(m):
        m["celerp_version"] = recorded
    data = _bk_edit_manifest(await download(real_client, tok), change)
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "damaged or was changed")


async def test_backup_records_the_celerp_that_made_it(real_engine, real_client, tmp_path, monkeypatch):
    """The manifest names the Celerp version that made it, so an older copy can refuse it."""
    from celerp.migrations.compatibility import running_version
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine)
    m = json.loads(members(await download(real_client, tok))["manifest.json"])
    assert m["celerp_version"] == running_version()


@pytest.mark.parametrize("table", ["locations", "ledger"])
async def test_unknown_archived_column_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch, table):
    """A backup listing a column this Celerp's table does not have is refused before any write."""
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)

    def change(m):
        m["tables"][table]["columns"].append("added_later")
    data = _bk_edit_manifest(await download(real_client, tok), change)
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "newer version of Celerp")


async def _bk_module_backup(engine, client, tmp_path, monkeypatch):
    """A backup of a company that uses the fake module and has a row in its prefix table."""
    _bk_local(monkeypatch, tmp_path)
    pkg = _bk_fake_module(tmp_path, monkeypatch)
    user, cid, tok = await _bk_setup(engine, settings={"enabled_modules": [_BK_MODULE]})
    await _bk_sql(engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                          "company_id uuid not null references companies(id) on delete cascade, note text)")
    await _bk_sql(engine, "INSERT INTO zz_widgets (id, company_id, note) VALUES (:i, :c, 'widget-marker')",
                  i=uuid.uuid4(), c=cid)
    return user, tok, pkg, await download(client, tok)


async def test_missing_module_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch):
    """A backup needing a module this installation does not have is refused, naming it, before any write."""
    try:
        user, tok, _, data = await _bk_module_backup(real_engine, real_client, tmp_path, monkeypatch)
        assert _BK_MODULE in manifest(data)["modules"]["enabled"]
        _bk_uninstall_module(tmp_path, monkeypatch)
        await _bk_modules_required(real_engine, real_client, tok, user, tmp_path, data, _BK_MODULE, "missing")
    finally:
        await _bk_drop(real_engine, "zz_widgets")


async def test_incompatible_module_version_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch):
    """A backup made with a newer module version than the one installed is refused, naming the module."""
    try:
        user, tok, pkg, data = await _bk_module_backup(real_engine, real_client, tmp_path, monkeypatch)
        assert manifest(data)["modules"]["versions"][_BK_MODULE] == "2.0.0"
        _bk_fake_module(tmp_path, monkeypatch, version="1.0.0")
        await _bk_modules_required(real_engine, real_client, tok, user, tmp_path, data, _BK_MODULE, "incompatible")
    finally:
        await _bk_drop(real_engine, "zz_widgets")


async def test_module_updated_on_disk_but_not_restarted_refused(real_engine, real_client, tmp_path, monkeypatch):
    """A module whose newer copy is installed but not yet running is refused until a restart,
    because the running code, not the files on disk, owns its tables."""
    try:
        user, tok, pkg, data = await _bk_module_backup(real_engine, real_client, tmp_path, monkeypatch)
        assert manifest(data)["modules"]["versions"][_BK_MODULE] == "2.0.0"
        _bk_running_version(monkeypatch, _BK_MODULE, "1.0.0")
        await _bk_modules_required(real_engine, real_client, tok, user, tmp_path, data, _BK_MODULE,
                                   "upgrade_restart_required")
    finally:
        await _bk_drop(real_engine, "zz_widgets")


# ── Modules ──────────────────────────────────────────────────────────────────

async def test_bundled_module_enabled_on_restore(real_engine, real_client, tmp_path, monkeypatch):
    """Bundled modules the backup records as enabled are enabled for the restored company."""
    from celerp.modules.registry import get_enabled
    _bk_local(monkeypatch, tmp_path)
    enabled = ["celerp-inventory", "celerp-labels"]
    _, _, tok = await _bk_setup(real_engine, settings={"currency": "THB", "enabled_modules": enabled})
    data = await download(real_client, tok)

    def settings_without_modules(m):
        m["company"]["settings"].pop("enabled_modules", None)
    data = _bk_edit_manifest(data, settings_without_modules)
    assert sorted(manifest(data)["modules"]["enabled"]) == enabled
    new = await _bk_restore_new(real_client, tok, data)
    restored = await _bk_settings(real_engine, new)
    assert get_enabled(restored) >= set(enabled)
    assert restored["currency"] == "THB"


async def test_manifest_records_module_versions(real_engine, real_client, tmp_path, monkeypatch):
    """The manifest records each enabled module's installed version, bundled and third-party alike."""
    from celerp.modules.loader import module_search_path, read_manifest, resolve_module_path
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    enabled = ["celerp-labels", _BK_MODULE]
    _, _, tok = await _bk_setup(real_engine, settings={"enabled_modules": enabled})
    versions = manifest(await download(real_client, tok))["modules"]["versions"]
    assert versions[_BK_MODULE] == "2.0.0"
    assert versions["celerp-labels"] == read_manifest(resolve_module_path("celerp-labels", module_search_path()))["version"]


async def test_backup_contains_no_module_code(real_engine, real_client, tmp_path, monkeypatch):
    """No module source or compiled code is ever packed into a company backup."""
    try:
        _, _, _, data = await _bk_module_backup(real_engine, real_client, tmp_path, monkeypatch)
        parts = members(data)
        assert not [n for n in parts if n.endswith((".py", ".pyc", ".so", ".whl")) or n.startswith("modules/")]
        assert _BK_CODE_MARKER.encode() not in b"".join(parts.values())
        assert b"def widget_code" not in b"".join(parts.values())
    finally:
        await _bk_drop(real_engine, "zz_widgets")


# ── Round trip ───────────────────────────────────────────────────────────────

async def test_round_trip_every_portable_table(real_engine, real_client, tmp_path, monkeypatch):
    """Every portable core and bundled-module table restores with the same rows as the source company."""
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine, settings={"currency": "THB"})
    await _bk_seed_portable(real_engine, cid, "alpha-marker")
    await _bk_extra_ledger(real_engine, cid, 2, "round")
    data = await download(real_client, tok)
    new = await _bk_restore_new(real_client, tok, data)
    assert new != str(cid)
    for table in sorted(cb.PORTABLE_TABLES):
        source = await _bk_normalized_rows(real_engine, table, cid, attributed=table == "ledger")
        restored = await _bk_normalized_rows(real_engine, table, new)
        assert source, table
        assert restored == source, table
    assert (await _bk_settings(real_engine, new))["currency"] == "THB"


# ── Restore ──


_R_SESSION_TABLES = frozenset({"session_registry", "user_auth_state"})
_R_MARKER = "alpha-marker"
_R_EARLY_GATES = ("validation", "compatibility", "references")
_R_GATES = ("validation", "compatibility", "rows_inserted", "references", "attachments", "read_back")


def _r_env(tmp_path, monkeypatch) -> None:
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)


async def _r_source(engine, *, name: str = "Alpha Trading", marker: str = _R_MARKER):
    """The installation owner, one company of theirs, and an owner token on it."""
    user = await owner(engine)
    cid = await company(engine, user, name, marker)
    return user, cid, await token(engine, user, cid)


async def _r_scalar(engine, sql: str, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def _r_location(engine, cid) -> str:
    return await _r_scalar(engine, "SELECT id::text FROM locations WHERE company_id = :c", c=cid)


async def _r_set_data(engine, cid, data: dict) -> None:
    """Replace the company's ledger event data and rebuild its record from it."""
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE ledger SET data = CAST(:d AS json) WHERE company_id = :c"),
                           {"d": json.dumps(data), "c": cid})
    await settle(engine, cid)


async def _r_rows(engine, table: str, cid) -> list[dict]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(f'SELECT to_jsonb(x)::text FROM "{table}" x WHERE company_id::text = :c'),
                                   {"c": str(cid)})).scalars().all()
    return [json.loads(row) for row in rows]


async def _r_company_tables(engine) -> list[str]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(
            "SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t "
            "ON t.table_name = c.table_name AND t.table_schema = c.table_schema "
            "WHERE c.table_schema = current_schema() AND c.column_name = 'company_id' "
            "AND t.table_type = 'BASE TABLE' ORDER BY 1"))).scalars().all())


async def _r_memberships(engine, cid) -> set[tuple[str, str, bool]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT user_id::text, role, is_active FROM user_companies WHERE company_id = :c"), {"c": cid})).all()
    return {tuple(row) for row in rows}


def _r_without_sessions(snap: dict) -> dict:
    return {t: rows for t, rows in snap.items() if t not in _R_SESSION_TABLES}


def _r_created(r) -> dict:
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["outcome"] == "created", body
    return body


def _r_refused(r) -> None:
    assert r.status_code in (409, 422), r.text
    assert r.json()["detail"].endswith("Nothing was restored."), r.text


def _r_table_lines(data: bytes, table: str) -> list[bytes]:
    return [line for line in members(data)[f"tables/{table}.jsonl"].split(b"\n") if line.strip()]


def _r_with_rows(data: bytes, table: str, edit) -> bytes:
    """The backup with one table's rows edited and that table's manifest hashes recomputed."""
    parts = members(data)
    rows = [edit(json.loads(line)) for line in _r_table_lines(data, table)]
    lines = [json.dumps(row) for row in rows]
    body = ("\n".join(lines) + "\n").encode()
    parts[f"tables/{table}.jsonl"] = body
    meta = json.loads(parts["manifest.json"])
    entry = meta["tables"][table]
    entry.update(rows=len(rows), sha256=sha256(body))
    if "digest" in entry:
        entry["digest"] = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()
    parts["manifest.json"] = json.dumps(meta).encode()
    return rezip(parts)


def _r_tampered(data: bytes) -> bytes:
    parts = members(data)
    parts["tables/projections.jsonl"] = parts["tables/projections.jsonl"].replace(b"alpha-marker", b"alpha-markex")
    return rezip(parts)


def _r_newer_column(data: bytes) -> bytes:
    parts = members(data)
    meta = json.loads(parts["manifest.json"])
    meta["tables"]["locations"]["columns"].append("added_later")
    parts["manifest.json"] = json.dumps(meta).encode()
    return rezip(parts)


@asynccontextmanager
async def _r_trigger(engine, table: str, body: str, when: str):
    """A temporary BEFORE INSERT trigger on ``table`` running ``body`` for rows matching ``when``."""
    name = f"r_trg_{uuid.uuid4().hex[:12]}"
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {body} RETURN NEW; END $$")
        await conn.exec_driver_sql(
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION {name}()")
    try:
        yield name
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name} ON {table}")
            await conn.exec_driver_sql(f"DROP FUNCTION IF EXISTS {name}()")


@asynccontextmanager
async def _r_company_insert_probe(engine):
    """Records whether any company row insert was attempted, even one later rolled back
    (sequence increments survive a rollback). Yields an async callable answering that."""
    name = f"r_probe_{uuid.uuid4().hex[:12]}"
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"CREATE SEQUENCE {name}")
    try:
        async with _r_trigger(engine, "companies", f"PERFORM nextval('{name}');", "true"):
            async def reached() -> bool:
                return bool(await _r_scalar(engine, f"SELECT is_called FROM {name}"))
            yield reached
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f"DROP SEQUENCE IF EXISTS {name}")


def _r_reject_rows(engine):
    """The database rejects the archived projection rows as they are inserted."""
    return _r_trigger(engine, "projections",
                      "RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = 'row rejected';",
                      f"strpos(NEW.state::text, '{_R_MARKER}') > 0")


def _r_alter_rows(engine):
    """Inserted projection rows silently differ from the archive, so read-back verification must fail."""
    return _r_trigger(engine, "projections", "NEW.version := NEW.version + 1000;",
                      f"strpos(NEW.state::text, '{_R_MARKER}') > 0")


class _RCloud(FakeCloud):
    """The fake cloud backend, also recording which companies it stored for and deleted."""

    def __init__(self) -> None:
        super().__init__()
        self.store_ids: list[str] = []
        self.delete_ids: list[str] = []

    async def store(self, company_id, att_id, content, mime):
        self.store_ids.append(str(company_id))
        return await super().store(company_id, att_id, content, mime)

    async def delete_company(self, company_id) -> None:
        self.delete_ids.append(str(company_id))
        await super().delete_company(company_id)


def _r_cloud(monkeypatch) -> _RCloud:
    from celerp.services import attachments
    cloud = _RCloud()
    monkeypatch.setattr(attachments, "_backend", cloud)
    return cloud


async def _r_cloud_files(engine, cloud: _RCloud, cid) -> dict[str, bytes]:
    """Two cloud-stored attachments referenced by the company's projection."""
    files = {cloud.url(cid, "photo.png"): b"alpha-photo", cloud.url(cid, "scan.png"): b"alpha-scan"}
    cloud.files.update(files)
    await _r_set_data(engine, cid, {
        "name": _R_MARKER,
        "attachments": [{"url": url, "name": url.rsplit("/", 1)[1], "mime": "image/png"} for url in files]})
    return files


def _r_no_local_files(tmp_path) -> None:
    root = tmp_path / "static" / "attachments"
    assert not root.exists() or not any(p.is_file() for p in root.rglob("*"))


async def _r_failing_restore(engine, client, tok, data: bytes, cloud: _RCloud, kind: str, foreign_location: str,
                             mode: str = "settings"):
    """Restore ``data`` with a failure induced at one commit gate. Returns the response and
    whether any company insert was attempted."""
    if kind == "validation":
        data = _r_tampered(data)
    elif kind == "compatibility":
        data = _r_newer_column(data)
    elif kind == "references":
        data = _r_with_rows(data, "ledger", lambda row: {**row, "location_id": foreign_location})
    elif kind == "attachments":
        cloud.fail_store_after = 1
    async with AsyncExitStack() as stack:
        reached = await stack.enter_async_context(_r_company_insert_probe(engine))
        if kind == "rows_inserted":
            await stack.enter_async_context(_r_reject_rows(engine))
        if kind == "read_back":
            await stack.enter_async_context(_r_alter_rows(engine))
        r = await restore(client, tok, data, mode)
        return r, await reached()


def _r_spy(monkeypatch) -> list[str]:
    """Record the mode of every call into the canonical importer, then run it."""
    import celerp.routers.company_backup as router_module
    import celerp.services.company_backup as cb
    modes: list[str] = []
    real = cb.restore_company

    async def spy(*args, **kwargs):
        modes.append(kwargs.get("mode"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(cb, "restore_company", spy)
    if hasattr(router_module, "restore_company"):
        monkeypatch.setattr(router_module, "restore_company", spy)
    return modes


async def _r_fresh(engine, client) -> bytes:
    """A backup of Alpha Trading, then an installation with no users or companies left."""
    _, _, tok = await _r_source(engine)
    data = await download(client, tok)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))
    return data


async def _r_bread(client, data: bytes, code: str | None):
    headers = {"X-Setup-Code": code} if code is not None else {}
    return await client.post("/company-backups/bootstrap/read", files={"file": ("books.celerp-company", data)},
                             headers=headers)


async def _r_brestore(client, upload_token: str, code: str | None, *, email: str = "first@example.com",
                      name: str = "First Owner", password: str = "firstownerpw1"):
    headers = {"X-Setup-Code": code} if code is not None else {}
    return await client.post("/company-backups/bootstrap/restore", headers=headers, json={
        "upload_token": upload_token, "name": name, "email": email, "password": password})


async def _r_bootstrap(client, data: bytes, code: str, **account):
    r = await _r_bread(client, data, code)
    assert r.status_code == 200, r.text
    return await _r_brestore(client, r.json()["upload_token"], code, **account)


async def _r_me(client, access_token: str) -> dict:
    r = await client.get("/companies/me", headers=auth(access_token))
    assert r.status_code == 200, r.text
    return r.json()


# ── Settings, add-company and source safety ──────────────────────────────────

async def test_settings_restore(real_engine, real_client, tmp_path, monkeypatch):
    """Settings restore creates a verified company carrying the backup's records through the canonical importer."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    modes = _r_spy(monkeypatch)
    body = _r_created(await restore(real_client, tok, data, "settings"))
    assert modes == ["settings"]
    new = uuid.UUID(body["company_id"])
    assert new != cid
    assert body["company_name"] == await _r_scalar(real_engine, "SELECT name FROM companies WHERE id = :c", c=new)
    assert body["backup_created_at"] == manifest(data)["created_at"]
    for table in ("locations", "ledger", "projections"):
        assert await count(real_engine, table, "company_id = :c", c=new) == 1, table
    assert _R_MARKER in json.dumps(await _r_rows(real_engine, "projections", new))


async def test_add_company_restore(real_engine, real_client, tmp_path, monkeypatch):
    """Add-company restore creates one new company from the backup through the canonical importer."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    modes = _r_spy(monkeypatch)
    body = _r_created(await restore(real_client, tok, data, "new_company"))
    assert modes == ["new_company"]
    new = uuid.UUID(body["company_id"])
    assert new != cid and await count(real_engine, "companies") == 2
    for table in ("locations", "ledger", "projections"):
        assert await count(real_engine, table, "company_id = :c", c=new) == 1, table
    assert (str(user), "owner", True) in await _r_memberships(real_engine, new)


async def test_add_company_restore_only_initiator_membership(real_engine, real_client, tmp_path, monkeypatch):
    """Add-company restore gives membership only to the initiating owner, even from the source company."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    clerk = await owner(real_engine, "clerk@example.com", "Clerk")
    await member(real_engine, clerk, cid, "manager")
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, "new_company"))
    assert await _r_memberships(real_engine, body["company_id"]) == {(str(user), "owner", True)}


async def test_add_company_restore_other_companies_untouched(real_engine, real_client, tmp_path, monkeypatch):
    """Add-company restore from another company's context changes no row of any existing company."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    other = await company(real_engine, user, "Beta Trading", "beta-marker")
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    body = _r_created(await restore(real_client, await token(real_engine, user, other), data, "new_company"))
    after = await snapshot(real_engine)
    unchanged_except(_r_without_sessions(before), _r_without_sessions(after), body["company_id"])
    assert await count(real_engine, "companies") == 3


async def test_restore_creates_new_company_source_untouched(real_engine, real_client, tmp_path, monkeypatch):
    """A restore adds one company with its own copy of every record and never alters the source."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    body = _r_created(await restore(real_client, tok, data))
    new = body["company_id"]
    assert new != str(cid)
    unchanged_except(_r_without_sessions(before), _r_without_sessions(await snapshot(real_engine)), new)
    assert await count(real_engine, "companies") == 2
    for table in ("locations", "ledger", "projections"):
        assert await count(real_engine, table, "company_id = :c", c=uuid.UUID(new)) == \
            await count(real_engine, table, "company_id = :c", c=cid), table
    assert (await _r_me(real_client, tok))["id"] == str(cid)


async def test_restore_same_backup_into_source_installation_no_collision(real_engine, real_client, tmp_path,
                                                                           monkeypatch):
    """Restoring a backup into the installation that made it uses fresh ids and collides with nothing."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await member(real_engine, await owner(real_engine, "clerk@example.com", "Clerk"), cid, "viewer")
    data = await download(real_client, tok)
    source_rows = {t: await _r_rows(real_engine, t, cid) for t in ("locations", "ledger", "projections")}
    body = _r_created(await restore(real_client, tok, data))
    new = uuid.UUID(body["company_id"])
    old_locations = {row["id"] for row in source_rows["locations"]}
    new_locations = {row["id"] for row in await _r_rows(real_engine, "locations", new)}
    assert new_locations and not new_locations & old_locations
    for table, rows in source_rows.items():
        assert await _r_rows(real_engine, table, cid) == rows, table
    assert {row["entity_id"] for row in await _r_rows(real_engine, "projections", new)} == \
        {row["entity_id"] for row in source_rows["projections"]}


async def test_restore_switches_initiating_user(real_engine, real_client, tmp_path, monkeypatch):
    """The restore response signs the initiating owner into the restored company."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, "settings"))
    assert body["access_token"] and body["refresh_token"]
    me = await _r_me(real_client, body["access_token"])
    assert me["id"] == body["company_id"] and me["id"] != str(cid)
    assert me["current_role"] == "owner"
    r = await real_client.post("/auth/token/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200, r.text
    assert (await _r_me(real_client, r.json()["access_token"]))["id"] == body["company_id"]
    assert (await _r_me(real_client, tok))["id"] == str(cid)


async def test_restored_company_records_backup_provenance(real_engine, real_client, tmp_path, monkeypatch):
    """The restored company records which backup it came from and when that backup was made."""
    _r_env(tmp_path, monkeypatch)
    _, _, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data))
    settings = await _r_scalar(real_engine, "SELECT settings::text FROM companies WHERE id = :c",
                               c=uuid.UUID(body["company_id"]))
    info = json.loads(settings)["restored_backup"]
    meta = manifest(data)
    assert info["backup_id"] == meta["backup_id"]
    assert info["created_at"] == meta["created_at"]
    assert info["source_company_name"] == "Alpha Trading"
    assert info["restored_at"]
    assert "company_copy" not in json.loads(settings)


# ── Memberships ──────────────────────────────────────────────────────────────

async def test_same_lineage_memberships_carried(real_engine, real_client, tmp_path, monkeypatch):
    """A Settings restore of the same company's backup carries the installation's current team."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    clerk = await owner(real_engine, "clerk@example.com", "Clerk")
    await member(real_engine, clerk, cid, "viewer")
    data = await download(real_client, tok)
    late = await owner(real_engine, "late@example.com", "Late")
    await member(real_engine, late, cid, "admin")
    body = _r_created(await restore(real_client, tok, data, "settings"))
    assert await _r_memberships(real_engine, body["company_id"]) == {
        (str(user), "owner", True), (str(clerk), "viewer", True), (str(late), "admin", True)}
    assert "user_companies" not in manifest(data)["tables"]


async def test_same_lineage_carries_active_memberships_and_roles_only(real_engine, real_client, tmp_path,
                                                                       monkeypatch):
    """Only active memberships are carried, each with its current role."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    manager = await owner(real_engine, "manager@example.com", "Manager")
    operator = await owner(real_engine, "operator@example.com", "Operator")
    gone = await owner(real_engine, "gone@example.com", "Gone")
    await member(real_engine, manager, cid, "manager")
    await member(real_engine, operator, cid, "operator")
    await member(real_engine, gone, cid, "admin", active=False)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, "settings"))
    assert await _r_memberships(real_engine, body["company_id"]) == {
        (str(user), "owner", True), (str(manager), "manager", True), (str(operator), "operator", True)}


async def test_same_lineage_carries_current_role_permissions(real_engine, real_client, tmp_path, monkeypatch):
    """The carried team keeps what its roles may do in the current company, not the defaults."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    admin = await owner(real_engine, "admin@example.com", "Admin")
    await member(real_engine, admin, cid, "admin")
    data = await download(real_client, tok)
    r = await real_client.patch("/companies/me/role-permissions",
                                json={"perm_key": "manage_users", "role_key": "admin", "granted": False}, headers=auth(tok))
    assert r.status_code == 200, r.text
    body = _r_created(await restore(real_client, tok, data, "settings"))
    grants = "SELECT settings::jsonb -> 'role_grants' FROM companies WHERE id = :c"
    assert await _r_scalar(real_engine, grants, c=uuid.UUID(body["company_id"])) == \
        await _r_scalar(real_engine, grants, c=cid)
    new = {"email": "new@example.com", "name": "New", "password": "password1234", "role": "viewer"}
    r = await real_client.post("/companies/me/users", json=new,
                               headers=auth(await token(real_engine, admin, body["company_id"], "admin")))
    assert r.status_code == 403, r.text


async def test_external_restore_carries_no_memberships(real_engine, real_client, tmp_path, monkeypatch):
    """A Settings restore of another company's backup gives membership only to the initiating owner."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    await member(real_engine, await owner(real_engine, "clerk@example.com", "Clerk"), cid, "viewer")
    other = await company(real_engine, user, "Beta Trading", "beta-marker")
    await member(real_engine, await owner(real_engine, "beta@example.com", "Beta"), other, "manager")
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, await token(real_engine, user, other), data, "settings"))
    assert await _r_memberships(real_engine, body["company_id"]) == {(str(user), "owner", True)}


async def test_memberships_copied_after_verification(real_engine, real_client, tmp_path, monkeypatch):
    """When read-back verification fails, no membership for the new company remains."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    await member(real_engine, await owner(real_engine, "clerk@example.com", "Clerk"), cid, "viewer")
    data = await download(real_client, tok)
    before = await _r_scalar(real_engine, "SELECT count(*) FROM user_companies")
    r, _ = await _r_failing_restore(real_engine, real_client, tok, data, _r_cloud(monkeypatch), "read_back", "")
    _r_refused(r)
    assert await _r_scalar(real_engine, "SELECT count(*) FROM user_companies") == before
    assert await count(real_engine, "user_companies", "company_id <> :c", c=cid) == 0


# ── Idempotency and concurrency ──────────────────────────────────────────────

async def test_retry_after_success_returns_same_company(real_engine, real_client, tmp_path, monkeypatch):
    """Retrying a successful restore returns the same company; a non-member is refused and nothing is created."""
    _r_env(tmp_path, monkeypatch)
    _, _, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    r = await read(real_client, tok, data)
    assert r.status_code == 200, r.text
    upload = confirm(r)
    first = _r_created(await real_client.post("/company-backups/restore", json=upload, headers=auth(tok)))
    companies = await count(real_engine, "companies")

    again = await real_client.post("/company-backups/restore", json=upload, headers=auth(tok))
    assert again.status_code == 200, again.text
    assert again.json()["company_id"] == first["company_id"] and again.json()["outcome"] == "opened_existing"
    reupload = await restore(real_client, tok, data)
    assert reupload.status_code == 200, reupload.text
    assert reupload.json()["company_id"] == first["company_id"] and reupload.json()["outcome"] == "opened_existing"
    assert await count(real_engine, "companies") == companies

    stranger = await owner(real_engine, "stranger@example.com", "Stranger")
    theirs = await company(real_engine, stranger, "Gamma Trading", "gamma-marker")
    companies = await count(real_engine, "companies")
    r = await restore(real_client, await token(real_engine, stranger, theirs), data)
    assert r.status_code == 409, r.text
    assert await count(real_engine, "companies") == companies


async def test_reopening_same_backup_does_not_clone(real_engine, real_client, tmp_path, monkeypatch):
    """Opening the same backup again, from any entry point, returns the company already restored."""
    _r_env(tmp_path, monkeypatch)
    user, _, tok = await _r_source(real_engine)
    other = await company(real_engine, user, "Beta Trading", "beta-marker")
    data = await download(real_client, tok)
    first = _r_created(await restore(real_client, tok, data, "settings"))
    companies = await count(real_engine, "companies")
    for tk, mode in ((tok, "new_company"), (await token(real_engine, user, other), "settings"),
                     (await token(real_engine, user, other), "new_company")):
        r = await restore(real_client, tk, data, mode)
        assert r.status_code == 200, r.text
        assert r.json()["company_id"] == first["company_id"] and r.json()["outcome"] == "opened_existing"
    assert await count(real_engine, "companies") == companies
    assert await count(real_engine, "projections", "company_id = :c", c=uuid.UUID(first["company_id"])) == 1


async def test_concurrent_restore_creates_one_company(real_engine, real_client, tmp_path, monkeypatch):
    """Two simultaneous restores of the same backup create exactly one company."""
    _r_env(tmp_path, monkeypatch)
    _, _, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    previews = []
    for _ in range(2):
        r = await read(real_client, tok, data)
        assert r.status_code == 200, r.text
        previews.append(confirm(r))
    results = await asyncio.gather(*(
        real_client.post("/company-backups/restore", json=p, headers=auth(tok)) for p in previews))
    assert sorted(r.status_code for r in results) == [200, 201], [r.text for r in results]
    assert len({r.json()["company_id"] for r in results}) == 1
    assert await count(real_engine, "companies") == 2
    assert await count(real_engine, "projections", "company_id = :c", c=uuid.UUID(results[0].json()["company_id"])) == 1


# ── Id remapping and reference policy ────────────────────────────────────────

async def test_exact_value_id_remap(real_engine, real_client, tmp_path, monkeypatch):
    """Every whole-string source id, in columns and nested JSON, is replaced by the restored row's new id."""
    from celerp.services.company_backup import remap
    old, new_id = str(uuid.uuid4()), str(uuid.uuid4())
    value = {"a": old, "b": [old, {"c": old}, f"x{old}"], old: "key", "n": 3}
    assert remap(value, {old: new_id}) == {"a": new_id, "b": [new_id, {"c": new_id}, f"x{old}"], old: "key", "n": 3}
    assert value["a"] == old

    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    loc = await _r_location(real_engine, cid)
    wc = str(uuid.uuid4())
    async with real_engine.begin() as conn:
        await conn.execute(text("INSERT INTO work_centers (id, company_id, name, wip_location_id, is_default, created_at) "
                                "VALUES (:i, :c, 'Bench', :l, false, now())"), {"i": wc, "c": cid, "l": loc})
    nested = {"name": _R_MARKER, "location_id": loc, "lines": [{"location_id": loc}, loc], "work_center": wc}
    await _r_set_data(real_engine, cid, nested)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data))
    new = uuid.UUID(body["company_id"])
    new_loc = await _r_location(real_engine, new)
    (new_wc,) = [row for row in await _r_rows(real_engine, "work_centers", new) if row["name"] == "Bench"]
    assert new_loc != loc and new_wc["id"] != wc and new_wc["wip_location_id"] == new_loc
    expected = {"name": _R_MARKER, "location_id": new_loc, "lines": [{"location_id": new_loc}, new_loc],
                "work_center": new_wc["id"]}
    (proj,) = await _r_rows(real_engine, "projections", new)
    (event,) = await _r_rows(real_engine, "ledger", new)
    assert proj["state"] == replayed(expected) and event["data"] == expected
    assert proj["location_id"] == new_loc and event["location_id"] == new_loc

    source_ids = {str(cid), loc, wc}
    for table in await _r_company_tables(real_engine):
        if table == "user_companies" or table in _R_SESSION_TABLES:
            continue
        dumped = json.dumps(await _r_rows(real_engine, table, new))
        assert not [i for i in source_ids if i in dumped], table


async def test_uuid_substrings_in_text_not_remapped(real_engine, real_client, tmp_path, monkeypatch):
    """Text that merely contains a copied id, such as a note or a URL, is restored unchanged."""
    from celerp.services.company_backup import remap
    old = str(uuid.uuid4())
    note = f"see order {old} and http://x/{old}"
    assert remap({"note": note, "ids": [f"{old} "]}, {old: str(uuid.uuid4())}) == {"note": note, "ids": [f"{old} "]}

    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    loc = await _r_location(real_engine, cid)
    note = f"see order {loc} and http://x/{loc}"
    await _r_set_data(real_engine, cid, {"name": _R_MARKER, "location_id": loc, "note": note, "memo": note})
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data))
    new = uuid.UUID(body["company_id"])
    (proj,) = await _r_rows(real_engine, "projections", new)
    (event,) = await _r_rows(real_engine, "ledger", new)
    assert proj["state"] == replayed(event["data"])
    assert event["data"]["note"] == note and event["data"]["memo"] == note
    assert proj["state"]["location_id"] == await _r_location(real_engine, new)


async def test_installation_independent_reference_permitted(real_engine, real_client, tmp_path, monkeypatch):
    """A uuid in JSON that names no row of the backup is an external value: kept as is, not refused."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    external = str(uuid.uuid4())
    await _r_set_data(real_engine, cid, {"name": _R_MARKER, "external_ref": external, "payment_ref": external})
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data))
    new = uuid.UUID(body["company_id"])
    (proj,) = await _r_rows(real_engine, "projections", new)
    (event,) = await _r_rows(real_engine, "ledger", new)
    assert proj["state"] == replayed(event["data"])
    assert event["data"]["external_ref"] == external and event["data"]["payment_ref"] == external


async def test_cross_company_reference_refused(real_engine, real_client, tmp_path, monkeypatch):
    """A row pointing at another company's record is refused before any company is created."""
    _r_env(tmp_path, monkeypatch)
    user, _, tok = await _r_source(real_engine)
    foreign = await _r_location(real_engine, await company(real_engine, user, "Beta Trading", "beta-marker"))
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    r, reached = await _r_failing_restore(real_engine, real_client, tok, data, _r_cloud(monkeypatch), "references",
                                          foreign)
    _r_refused(r)
    assert not reached
    assert await snapshot(real_engine) == before


def _r_with_settings(data: bytes, key: str, value) -> bytes:
    parts = members(data)
    meta = json.loads(parts["manifest.json"])
    meta["company"]["settings"][key] = value
    parts["manifest.json"] = json.dumps(meta).encode()
    return rezip(parts)


@pytest.mark.parametrize("place", ["state", "event", "settings"])
@pytest.mark.parametrize("target", ["record", "company"])
async def test_id_of_another_company_outside_foreign_keys_refused(real_engine, real_client, tmp_path, monkeypatch,
                                                                  place, target):
    """Another company's id, or the id of one of its records, held in JSON or in company
    settings is refused before any company is created."""
    _r_env(tmp_path, monkeypatch)
    user, _, tok = await _r_source(real_engine)
    other = await company(real_engine, user, "Beta Trading", "beta-marker")
    foreign = await _r_location(real_engine, other) if target == "record" else str(other)
    data = await download(real_client, tok)
    if place == "state":
        changed = _r_with_rows(data, "projections", lambda row: {**row, "state": {**row["state"], "bin": foreign}})
    elif place == "event":
        changed = _r_with_rows(data, "ledger", lambda row: {**row, "data": {**row["data"], "lines": [foreign]}})
    else:
        changed = _r_with_settings(data, "default_bin", foreign)
    before = await snapshot(real_engine)
    async with _r_company_insert_probe(real_engine) as reached:
        r = await restore(real_client, tok, changed)
        _r_refused(r)
        assert "another company" in r.json()["detail"], r.text
        assert not await reached()
    assert await snapshot(real_engine) == before


async def test_unresolved_reference_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch):
    """A foreign key naming a row that exists nowhere is refused before any company is created."""
    _r_env(tmp_path, monkeypatch)
    _, _, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    missing = str(uuid.uuid4())
    for table in ("ledger", "projections"):
        async with _r_company_insert_probe(real_engine) as reached:
            changed = _r_with_rows(data, table, lambda row: {**row, "location_id": missing})
            r = await restore(real_client, tok, changed)
            _r_refused(r)
            assert not await reached(), table
    assert await snapshot(real_engine) == before


async def test_installation_user_references_policy(real_engine, real_client, tmp_path, monkeypatch):
    """Columns naming installation users, such as ledger.actor_id, are empty in a company
    restored from another company's backup. (A Settings restore of the same company links
    history back to its exact authors: test_same_company_restore_relinks_exact_authors.)"""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, mode="new_company"))
    new = uuid.UUID(body["company_id"])
    (event,) = await _r_rows(real_engine, "ledger", new)
    assert event["actor_id"] is None
    assert (await _r_rows(real_engine, "ledger", cid))[0]["actor_id"] == str(user)
    async with real_engine.connect() as conn:
        user_fks = (await conn.execute(text(
            "SELECT kcu.table_name, kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name "
            "AND kcu.table_schema = tc.table_schema JOIN information_schema.constraint_column_usage ccu "
            "ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = current_schema() "
            "AND ccu.table_name = 'users'"))).all()
    company_tables = set(await _r_company_tables(real_engine)) - {"user_companies"} - _R_SESSION_TABLES
    for table, column in user_fks:
        if table in company_tables:
            assert all(row[column] is None for row in await _r_rows(real_engine, table, new)), (table, column)


async def test_ledger_generated_ids_policy(real_engine, real_client, tmp_path, monkeypatch):
    """Restored ledger rows get fresh ids from the database sequence, one per source row."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    async with real_engine.begin() as conn:
        for n in range(3):
            await conn.execute(text(
                "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, idempotency_key) "
                "VALUES (:c, :e, 'item', 'item.created', CAST(:d AS json), 'api', :k)"),
                {"c": cid, "e": f"item:{n + 2}", "d": json.dumps({"name": _R_MARKER}), "k": f"extra-{n}"})
    await settle(real_engine, cid)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data))
    new = uuid.UUID(body["company_id"])
    old_ids = {row["id"] for row in await _r_rows(real_engine, "ledger", cid)}
    new_ids = {row["id"] for row in await _r_rows(real_engine, "ledger", new)}
    assert len(new_ids) == len(old_ids) == 4
    assert min(new_ids) > max(old_ids)
    last = await _r_scalar(real_engine, "SELECT last_value FROM " +
                           await _r_scalar(real_engine, "SELECT pg_get_serial_sequence('ledger', 'id')"))
    assert last >= max(new_ids)


async def test_generated_identity_columns_regenerated(real_engine, real_client, tmp_path, monkeypatch):
    """Every database-generated identity or serial column is regenerated rather than copied."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data))
    new = uuid.UUID(body["company_id"])
    company_tables = set(await _r_company_tables(real_engine))
    async with real_engine.connect() as conn:
        generated = (await conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND (is_identity = 'YES' OR left(column_default, 8) = 'nextval(')"))).all()
    checked = []
    for table, column in generated:
        if table not in company_tables:
            continue
        old_values = {row[column] for row in await _r_rows(real_engine, table, cid)}
        new_values = {row[column] for row in await _r_rows(real_engine, table, new)}
        if old_values and new_values:
            assert len(new_values) == len(old_values), (table, column)
            assert not old_values & new_values, (table, column)
            checked.append((table, column))
    assert ("ledger", "id") in checked


# ── Failure atomicity ────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate", _R_GATES)
async def test_restore_failure_at_each_commit_gate_rolls_back(real_engine, real_client, tmp_path, monkeypatch, gate):
    """A failure at any gate before commit leaves every row and every stored file as it was."""
    _r_env(tmp_path, monkeypatch)
    cloud = _r_cloud(monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    foreign = await _r_location(real_engine, await company(real_engine, user, "Beta Trading", "beta-marker"))
    source_files = await _r_cloud_files(real_engine, cloud, cid)
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    r, reached = await _r_failing_restore(real_engine, real_client, tok, data, cloud, gate, foreign)
    _r_refused(r)
    assert await snapshot(real_engine) == before
    assert cloud.files == source_files
    _r_no_local_files(tmp_path)
    assert set(cloud.store_ids) - {str(cid)} <= set(cloud.delete_ids)
    if gate in _R_EARLY_GATES:
        assert not reached
    if gate == "attachments":
        assert cloud.store_ids


@pytest.mark.parametrize("kind, mode", [
    ("validation", "settings"), ("references", "new_company"), ("attachments", "settings"),
    ("attachments", "new_company"), ("read_back", "settings"), ("read_back", "new_company"),
])
async def test_existing_company_survives_every_restore_failure(real_engine, real_client, tmp_path, monkeypatch,
                                                               kind, mode):
    """After any failed restore the existing company is unchanged and still works."""
    _r_env(tmp_path, monkeypatch)
    cloud = _r_cloud(monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    foreign = await _r_location(real_engine, await company(real_engine, user, "Beta Trading", "beta-marker"))
    source_files = await _r_cloud_files(real_engine, cloud, cid)
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    r, _ = await _r_failing_restore(real_engine, real_client, tok, data, cloud, kind, foreign, mode)
    _r_refused(r)
    assert await snapshot(real_engine) == before
    assert cloud.company_files(cid) == source_files
    me = await _r_me(real_client, tok)
    assert me["id"] == str(cid) and me["name"] == "Alpha Trading"
    assert await count(real_engine, "companies") == 2


async def test_database_rollback_on_verification_failure(real_engine, real_client, tmp_path, monkeypatch):
    """When the restored rows do not read back as the backup, every new row is rolled back."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    folder = tmp_path / "static" / "attachments" / str(cid)
    folder.mkdir(parents=True)
    (folder / "photo.png").write_bytes(b"alpha-photo")
    await _r_set_data(real_engine, cid, {
        "name": _R_MARKER, "attachments": [{"url": f"/static/attachments/{cid}/photo.png", "mime": "image/png"}]})
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    async with _r_alter_rows(real_engine):
        r = await restore(real_client, tok, data)
    _r_refused(r)
    assert await snapshot(real_engine) == before
    assert await count(real_engine, "companies") == 1
    assert sorted(p.name for p in (tmp_path / "static" / "attachments").iterdir()) == [str(cid)]
    assert (folder / "photo.png").read_bytes() == b"alpha-photo"


async def test_failed_attachment_storage_cleaned(real_engine, real_client, tmp_path, monkeypatch):
    """Files stored before an attachment failure are deleted under the new company id and no rows are kept."""
    _r_env(tmp_path, monkeypatch)
    cloud = _r_cloud(monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    source_files = await _r_cloud_files(real_engine, cloud, cid)
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    cloud.fail_store_after = 1
    r = await restore(real_client, tok, data)
    _r_refused(r)
    new_ids = set(cloud.store_ids) - {str(cid)}
    assert len(new_ids) == 1 and cloud.stored == 1
    assert new_ids <= set(cloud.delete_ids)
    assert cloud.files == source_files
    assert await snapshot(real_engine) == before
    _r_no_local_files(tmp_path)


# ── Fresh installation ───────────────────────────────────────────────────────

async def test_bootstrap_restore(real_engine, real_client, tmp_path, monkeypatch, code_config):
    """A fresh installation restores a backup as its first company with the new first owner."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    body = _r_created(await _r_bootstrap(real_client, data, code_config))
    new = uuid.UUID(body["company_id"])
    assert body["company_name"] == "Alpha Trading"
    assert await count(real_engine, "users") == 1 and await count(real_engine, "companies") == 1
    first = await _r_scalar(real_engine, "SELECT id::text FROM users WHERE email = 'first@example.com' "
                                         "AND is_install_owner")
    assert first
    assert await _r_memberships(real_engine, new) == {(first, "owner", True)}
    for table in ("locations", "ledger", "projections"):
        assert await count(real_engine, table, "company_id = :c", c=new) == 1, table


async def test_bootstrap_restore_enters_restored_company(real_engine, real_client, tmp_path, monkeypatch,
                                                         code_config):
    """The first owner's session opens on the restored company."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    body = _r_created(await _r_bootstrap(real_client, data, code_config))
    me = await _r_me(real_client, body["access_token"])
    assert me["id"] == body["company_id"] and me["name"] == "Alpha Trading" and me["current_role"] == "owner"


async def test_bootstrap_restore_issues_session(real_engine, real_client, tmp_path, monkeypatch, code_config):
    """A successful fresh-install restore issues a working access and refresh token pair."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    body = _r_created(await _r_bootstrap(real_client, data, code_config))
    assert body["access_token"] and body["refresh_token"]
    await _r_me(real_client, body["access_token"])
    r = await real_client.post("/auth/token/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200, r.text
    assert (await _r_me(real_client, r.json()["access_token"]))["id"] == body["company_id"]


async def test_bootstrap_restore_uses_canonical_importer(real_engine, real_client, tmp_path, monkeypatch,
                                                         code_config):
    """The fresh-install restore runs the canonical company-backup importer in bootstrap mode."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    modes = _r_spy(monkeypatch)
    _r_created(await _r_bootstrap(real_client, data, code_config))
    assert modes == ["bootstrap"]


async def test_bootstrap_preview_shows_summary_before_creation(real_engine, real_client, tmp_path, monkeypatch,
                                                               code_config):
    """The fresh-install preview names the company, backup date and record counts and writes nothing."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    before = await snapshot(real_engine)
    r = await _r_bread(real_client, data, code_config)
    assert r.status_code == 200, r.text
    preview, meta = r.json(), manifest(data)
    assert preview["upload_token"]
    assert preview["company_name"] == "Alpha Trading"
    assert preview["backup_id"] == meta["backup_id"] and preview["created_at"] == meta["created_at"]
    assert {t: preview["tables"][t] for t in ("locations", "ledger", "projections")} == \
        {"locations": 1, "ledger": 1, "projections": 1}
    assert preview["records"] == sum(preview["tables"].values())
    assert preview["attachments"] == 0
    assert await snapshot(real_engine) == before


async def test_bootstrap_restore_refused_when_bootstrapped(real_engine, real_client, tmp_path, monkeypatch,
                                                           code_config):
    """Once any user exists the fresh-install restore is closed and creates nothing."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    r = await _r_bread(real_client, data, code_config)
    assert r.status_code == 200, r.text
    upload_token = r.json()["upload_token"]
    await owner(real_engine)
    r = await _r_brestore(real_client, upload_token, code_config)
    assert r.status_code == 409, r.text
    assert (await _r_bread(real_client, data, code_config)).status_code == 409
    assert await count(real_engine, "companies") == 0 and await count(real_engine, "users") == 1


async def test_bootstrap_restore_requires_setup_code(real_engine, real_client, tmp_path, monkeypatch, code_config):
    """A configured setup code gates both the fresh-install preview and the restore."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    for code in (None, "wrong"):
        assert (await _r_bread(real_client, data, code)).status_code == 403
    r = await _r_bread(real_client, data, code_config)
    assert r.status_code == 200, r.text
    upload_token = r.json()["upload_token"]
    for code in (None, "wrong"):
        assert (await _r_brestore(real_client, upload_token, code)).status_code == 403
    assert await count(real_engine, "users") == 0 and await count(real_engine, "companies") == 0
    _r_created(await _r_brestore(real_client, upload_token, code_config))


async def test_bootstrap_restore_failure_creates_no_owner(real_engine, real_client, tmp_path, monkeypatch,
                                                          code_config):
    """A failed fresh-install restore leaves no user and no company, and the installation stays open."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    r = await _r_bread(real_client, data, code_config)
    assert r.status_code == 200, r.text
    async with _r_alter_rows(real_engine):
        r = await _r_brestore(real_client, r.json()["upload_token"], code_config)
    _r_refused(r)
    assert await count(real_engine, "users") == 0 and await count(real_engine, "companies") == 0
    assert await count(real_engine, "user_companies") == 0
    assert (await _r_bread(real_client, data, code_config)).status_code == 200


async def test_bootstrap_retry_after_success_returns_same_company(real_engine, real_client, tmp_path, monkeypatch,
                                                                  code_config):
    """Retrying a successful fresh-install restore returns the same company; other credentials are refused."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    r = await _r_bread(real_client, data, code_config)
    assert r.status_code == 200, r.text
    upload_token = r.json()["upload_token"]
    first = _r_created(await _r_brestore(real_client, upload_token, code_config))
    again = await _r_brestore(real_client, upload_token, code_config)
    assert again.status_code == 200, again.text
    assert again.json()["company_id"] == first["company_id"] and again.json()["outcome"] == "opened_existing"
    assert again.json()["access_token"]
    other = await _r_brestore(real_client, upload_token, code_config, email="other@example.com")
    assert other.status_code == 409, other.text
    assert await count(real_engine, "users") == 1 and await count(real_engine, "companies") == 1


async def test_concurrent_bootstrap_restore_one_owner(real_engine, real_client, tmp_path, monkeypatch, code_config):
    """Two simultaneous fresh-install restores create exactly one owner and one company."""
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    tokens = []
    for _ in range(2):
        r = await _r_bread(real_client, data, code_config)
        assert r.status_code == 200, r.text
        tokens.append(r.json()["upload_token"])
    results = await asyncio.gather(
        _r_brestore(real_client, tokens[0], code_config, email="first@example.com"),
        _r_brestore(real_client, tokens[1], code_config, email="second@example.com"))
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    assert await count(real_engine, "users") == 1 and await count(real_engine, "companies") == 1
    assert await count(real_engine, "user_companies") == 1


@pytest.mark.parametrize("limit", ["MAX_UPLOAD_BYTES", "MAX_MEMBERS", "MAX_MEMBER_BYTES", "MAX_TOTAL_BYTES"])
async def test_bootstrap_upload_limits_refused(real_engine, real_client, tmp_path, monkeypatch, code_config, limit):
    """The unauthenticated fresh-install upload enforces every upload and archive limit and writes nothing."""
    import celerp.services.company_backup as cb
    _r_env(tmp_path, monkeypatch)
    data = await _r_fresh(real_engine, real_client)
    parts = members(data)
    value = {"MAX_UPLOAD_BYTES": len(data) - 1, "MAX_MEMBERS": len(parts) - 1,
             "MAX_MEMBER_BYTES": max(len(b) for b in parts.values()) - 1,
             "MAX_TOTAL_BYTES": sum(len(b) for b in parts.values()) - 1}[limit]
    monkeypatch.setattr(cb, limit, value)
    before = await snapshot(real_engine)
    r = await _r_bread(real_client, data, code_config)
    assert r.status_code == (413 if limit == "MAX_UPLOAD_BYTES" else 422), r.text
    assert "too large" in r.json()["detail"]
    assert await snapshot(real_engine) == before


async def test_restored_backup_record_not_writable_through_settings(real_engine, real_client, tmp_path, monkeypatch):
    """Company settings cannot claim a backup was restored as this company, so restoring it
    still creates a new company."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    claim = {"settings": {"restored_backup": {"backup_id": manifest(data)["backup_id"]}}}
    r = await real_client.patch("/companies/me", json=claim, headers=auth(tok))
    assert r.status_code == 422, r.text
    assert "restoring a company backup" in r.json()["detail"]
    assert "restored_backup" not in await _bk_settings(real_engine, cid)
    body = _r_created(await restore(real_client, tok, data, "new_company"))
    assert body["company_id"] != str(cid)


async def test_deactivated_restored_company_not_reused(real_engine, real_client, tmp_path, monkeypatch):
    """A deactivated company still counts as the restoration of its backup: restoring the
    backup again creates no copy and offers the owner its reactivation instead."""
    _r_env(tmp_path, monkeypatch)
    _, _, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    first = _r_created(await restore(real_client, tok, data, "new_company"))
    await _bk_sql(real_engine, "UPDATE companies SET is_active = false WHERE id = CAST(:c AS uuid)",
                  c=first["company_id"])
    companies = await count(real_engine, "companies")
    r = await read(real_client, tok, data, mode="new_company")
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "offer_reactivate"
    assert r.json()["destination_id"] == first["company_id"]
    again = await real_client.post("/company-backups/restore", json=confirm(r, "new_company"), headers=auth(tok))
    assert again.status_code == 409, again.text
    assert await count(real_engine, "companies") == companies
    assert await _r_scalar(real_engine, "SELECT is_active FROM companies WHERE id = CAST(:c AS uuid)",
                           c=first["company_id"]) is False


def _bk_with_attachment(data: bytes, name: str, url: str, body: bytes) -> bytes:
    """The backup with one more attachment file under ``name`` for ``url``, hashes rewritten."""
    parts = members(data)
    m = json.loads(parts.pop("manifest.json"))
    parts[f"attachments/{name}"] = body
    m["attachments"].append({"url": url, "name": name, "size": len(body), "sha256": sha256(body)})
    return rezip({**parts, "manifest.json": json.dumps(m).encode()})


@pytest.mark.parametrize("name", ["evil.html", "evil.svg", "evil.png.html", "evil", ".png"])
async def test_restore_refuses_attachment_of_unstored_type(real_engine, real_client, tmp_path, monkeypatch, name):
    """An attachment file whose name does not carry the stored extension of an allowed type
    is refused before anything is written, so a backup cannot plant a page that runs in the app."""
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    body = b"<html><body><script>document.title = 'x'</script></body></html>"
    data = _bk_with_attachment(await download(real_client, tok), name, f"/static/attachments/{cid}/{name}", body)
    folders = set((tmp_path / "static" / "attachments").glob("*"))
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "a type Celerp does not store")
    assert set((tmp_path / "static" / "attachments").glob("*")) == folders


async def test_restore_refuses_attachment_url_of_another_company(real_engine, real_client, tmp_path, monkeypatch):
    """An attachment listed under a URL that is not a file of the backed-up company is refused."""
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)
    url = f"/static/attachments/{uuid.uuid4()}/photo.png"
    data = _bk_with_attachment(await download(real_client, tok), "photo.png", url, b"photo")
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "damaged")


async def test_attachment_named_before_type_extensions_round_trips(real_engine, real_client, tmp_path, monkeypatch):
    """A file stored under the name it was uploaded with travels under its recorded type's
    extension and restores; one with no allowed type stops the backup."""
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    url = _bk_local_file(tmp_path, cid, "scan.html", b"png-bytes")
    state = {"attachments": [{"url": url, "mime": "image/png", "filename": "scan.html"}]}
    await _bk_set_data(real_engine, cid, state)
    data = await download(real_client, tok)
    [entry] = manifest(data)["attachments"]
    assert (entry["url"], entry["name"]) == (url, "scan.png")
    assert members(data)["attachments/scan.png"] == b"png-bytes"
    new = await _bk_restore_new(real_client, tok, data)
    assert (tmp_path / "static" / "attachments" / new / "scan.png").read_bytes() == b"png-bytes"
    assert not (tmp_path / "static" / "attachments" / new / "scan.html").exists()
    restored = json.loads(await _bk_scalar(real_engine, "SELECT state::text FROM projections WHERE company_id = :c",
                                           c=uuid.UUID(new)))
    assert restored["attachments"][0]["url"] == f"/static/attachments/{new}/scan.png"

    state["attachments"][0]["mime"] = "text/html"
    await _bk_set_data(real_engine, cid, state)
    r = await real_client.get("/company-backups/download", headers=auth(tok))
    assert r.status_code == 409, r.text
    assert "a type Celerp does not store" in r.json()["detail"] and r.json()["detail"].endswith("Nothing was backed up.")


async def test_team_count_stated_in_preview_and_result(real_engine, real_client, tmp_path, monkeypatch):
    """A same-company Settings restore states before and after how many team members get access; others state none."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    await member(real_engine, await owner(real_engine, "clerk@example.com", "Clerk"), cid, "viewer")
    await member(real_engine, await owner(real_engine, "buyer@example.com", "Buyer"), cid, "manager")
    await member(real_engine, await owner(real_engine, "gone@example.com", "Gone"), cid, "admin", active=False)
    data = await download(real_client, tok)
    r = await read(real_client, tok, data)
    assert r.status_code == 200, r.text
    assert r.json()["team_members"] == 2
    body = _r_created(await restore(real_client, tok, data, "settings"))
    assert body["team_members"] == 2
    again = await restore(real_client, tok, data, "settings")
    assert again.status_code == 200 and again.json()["team_members"] == 0

    other = await company(real_engine, user, "Beta Trading", "beta-marker")
    other_tok = await token(real_engine, user, other)
    r = await read(real_client, other_tok, data)
    assert r.status_code == 200, r.text
    assert r.json()["team_members"] == 0


async def test_enabled_module_without_version_refused_before_writes(real_engine, real_client, tmp_path, monkeypatch):
    """A backup naming an enabled module that is not installed here is refused, naming it, even with no version recorded."""
    _bk_local(monkeypatch, tmp_path)
    user, _, tok = await _bk_setup(real_engine)

    def change(m):
        m["modules"]["enabled"].append("zz-absent")
        m["modules"]["versions"].pop("zz-absent", None)
    data = _bk_edit_manifest(await download(real_client, tok), change)
    await _bk_modules_required(real_engine, real_client, tok, user, tmp_path, data, "zz-absent", "missing")


@pytest.mark.parametrize("need", ["enabled", "data"])
async def test_installed_module_not_running_refused(real_engine, real_client, tmp_path, monkeypatch, need):
    """A module the backup needs that is installed here but not running, so its tables may not
    be in place, is refused with what to do, and nothing is written."""
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch)
    enabled = [_BK_MODULE] if need == "enabled" else []
    user, cid, tok = await _bk_setup(real_engine, settings={"enabled_modules": enabled})
    await _bk_sql(real_engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade)")
    try:
        if need == "data":
            await _bk_sql(real_engine, "INSERT INTO zz_widgets (id, company_id) VALUES (:i, :c)",
                          i=uuid.uuid4(), c=cid)
        data = await download(real_client, tok)
        assert _BK_MODULE in manifest(data)["modules"]["versions"]
        _bk_not_running(monkeypatch, _BK_MODULE)
        await _bk_modules_required(real_engine, real_client, tok, user, tmp_path, data, _BK_MODULE, "enable_required")
    finally:
        await _bk_drop(real_engine, "zz_widgets")


async def test_enabled_but_uninstalled_module_not_a_backup_requirement(real_engine, real_client, tmp_path, monkeypatch):
    """A module left enabled in settings but not installed is not recorded as needed, so the backup still restores here."""
    _bk_local(monkeypatch, tmp_path)
    _, _, tok = await _bk_setup(real_engine, settings={"enabled_modules": ["celerp-labels", "zz-absent"]})
    data = await download(real_client, tok)
    assert manifest(data)["modules"]["enabled"] == ["celerp-labels"]
    await _bk_restore_new(real_client, tok, data)


# ── Restore lineage: team carry, deactivated destinations and reactivation ───

_LN_SLUG_SUFFIX = re.compile(r"-deactivated-\d+$")


async def _ln_team(engine, cid, *roles: str, active: bool = True) -> list:
    """One new user per role, each a member of the company with that role; returns their ids."""
    users = []
    for role in roles:
        uid = await owner(engine, f"team-{uuid.uuid4().hex[:8]}@example.com", "Team Member")
        await member(engine, uid, cid, role, active=active)
        users.append(uid)
    return users


async def _ln_add_company(client, tok: str, data: bytes) -> str:
    """Restore the backup through Add Company; returns the destination company id."""
    return _r_created(await restore(client, tok, data, "new_company"))["company_id"]


async def _ln_preview(client, tok: str, data: bytes, mode: str = "settings"):
    r = await read(client, tok, data, mode=mode)
    assert r.status_code == 200, r.text
    return r


async def _ln_commit(client, tok: str, preview, mode: str = "settings"):
    return await client.post("/company-backups/restore", json=confirm(preview, mode), headers=auth(tok))


async def _ln_reactivate(client, tok: str, preview, mode: str = "new_company"):
    return await client.post("/company-backups/reactivate", json=confirm(preview, mode), headers=auth(tok))


async def _ln_deactivate(engine, client, user, company_id) -> None:
    r = await client.delete("/companies/me", headers=auth(await token(engine, user, company_id)))
    assert r.status_code == 200, r.text


async def _ln_company(engine, company_id) -> dict:
    """The company's active flag, slug and settings."""
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT is_active, slug, settings::text FROM companies WHERE id = CAST(:c AS uuid)"),
            {"c": str(company_id)})).one()
    return {"is_active": row[0], "slug": row[1], "settings": json.loads(row[2] or "{}")}


async def _ln_grants(engine, company_id):
    return (await _ln_company(engine, company_id))["settings"].get("role_grants")


async def _ln_set_grant(client, tok: str, perm: str, role: str, granted: bool) -> None:
    r = await client.patch("/companies/me/role-permissions",
                           json={"perm_key": perm, "role_key": role, "granted": granted}, headers=auth(tok))
    assert r.status_code == 200, r.text


async def _ln_stage(tmp_path, user_id, data: bytes) -> str:
    """Stage an upload for the user as the read step would; returns its upload token."""
    tok = uuid.uuid4().hex
    folder = tmp_path / "company_backups" / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{user_id}-{tok}.upload").write_bytes(data)
    return tok


async def _ln_inactive_destination(engine, client, tmp_path, monkeypatch):
    """A source company, its backup restored through Add Company, and that company deactivated."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(engine)
    data = await download(client, tok)
    dest = await _ln_add_company(client, tok, data)
    await _ln_deactivate(engine, client, user, dest)
    return user, cid, tok, data, dest


async def test_plan_existing_restore_resolves_every_action(real_engine, real_client, tmp_path, monkeypatch):
    """The planner resolves one action for every destination state and caller, reads without
    writing, and names the destination only to a caller entitled to it."""
    cb = _bk_cb()
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    [clerk] = await _ln_team(real_engine, cid, "viewer")
    data = await download(real_client, tok)
    backup = cb.read_backup(_bk_file(tmp_path, data))

    async def plan(mode, who, current):
        async with maker(real_engine)() as s:
            return await cb.plan_existing_restore(s, backup, mode, who, current)

    before = await snapshot(real_engine)
    first = await plan("settings", user, cid)
    assert (first.action, first.destination_id, first.destination_name) == ("create", None, "Alpha Trading (Restored)")
    assert list(first.team_to_add) == [(str(clerk), "viewer")]
    assert first.fingerprint == (await plan("settings", user, cid)).fingerprint
    added = await plan("new_company", user, cid)
    assert (added.action, list(added.team_to_add)) == ("create", [])
    assert await snapshot(real_engine) == before

    dest = await _ln_add_company(real_client, tok, data)
    second = await owner(real_engine, "second@example.com", "Second")
    await member(real_engine, second, cid, "owner")
    await member(real_engine, second, dest, "manager")
    stranger = await owner(real_engine, "stranger@example.com", "Stranger")
    theirs = await company(real_engine, stranger, "Gamma Trading", "gamma-marker")

    carry = await plan("settings", user, cid)
    assert (carry.action, carry.destination_id, carry.destination_name) == (
        "return_existing_and_add_team", dest, "Alpha Trading (Restored)")
    assert list(carry.team_to_add) == [(str(clerk), "viewer")] and carry.team_blocked == 0
    existing = await plan("new_company", user, cid)
    assert (existing.action, existing.destination_id, list(existing.team_to_add)) == ("return_existing", dest, [])
    blocked = await plan("settings", second, cid)
    assert (blocked.action, blocked.destination_id, list(blocked.team_to_add), blocked.team_blocked) == (
        "return_existing", dest, [], 1)
    refused = await plan("settings", stranger, theirs)
    assert (refused.action, refused.destination_id, refused.destination_name) == ("refuse", None, None)

    await _ln_deactivate(real_engine, real_client, user, dest)
    offer = await plan("new_company", user, cid)
    assert (offer.action, offer.destination_id, offer.destination_name) == ("offer_reactivate", dest, "Alpha Trading (Restored)")
    for who, current in ((second, cid), (stranger, theirs)):
        denied = await plan("settings", who, current)
        assert (denied.action, denied.destination_id, denied.destination_name) == ("refuse", None, None)
    assert len({p.fingerprint for p in (first, added, carry, existing, blocked, refused, offer)}) == 7


async def test_restore_commit_refuses_stale_preview(real_engine, real_client, tmp_path, monkeypatch):
    """A restore confirming a plan that no longer holds writes nothing and returns the fresh plan;
    confirming the fresh plan applies it, and a restore confirming no plan is refused."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "viewer")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    preview = await _ln_preview(real_client, tok, data)
    assert (preview.json()["action"], preview.json()["team_members"]) == ("return_existing_and_add_team", 1)
    await _ln_team(real_engine, cid, "manager")

    before = await snapshot(real_engine)
    stale = await _ln_commit(real_client, tok, preview)
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["code"] == "stale_preview" and body["detail"]
    assert (body["plan"]["action"], body["plan"]["team_members"]) == ("return_existing_and_add_team", 2)
    assert body["plan"]["plan_fingerprint"] != preview.json()["plan_fingerprint"]
    assert await snapshot(real_engine) == before

    unconfirmed = await real_client.post("/company-backups/restore", headers=auth(tok),
                                         json={"upload_token": preview.json()["upload_token"], "mode": "settings"})
    assert unconfirmed.status_code == 422, unconfirmed.text
    assert await snapshot(real_engine) == before

    fresh = {**confirm(preview), "plan_fingerprint": body["plan"]["plan_fingerprint"]}
    done = await real_client.post("/company-backups/restore", json=fresh, headers=auth(tok))
    assert done.status_code == 200, done.text
    assert (done.json()["company_id"], done.json()["team_members"]) == (dest, 2)
    assert len(await _r_memberships(real_engine, dest)) == 3


async def test_add_company_then_settings_restore_adds_missing_team(real_engine, real_client, tmp_path, monkeypatch):
    """A backup first restored through Add Company, then restored again from the source company's
    Settings, gives the missing team access to the existing company with their source roles."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    viewer, manager = await _ln_team(real_engine, cid, "viewer", "manager")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    assert await _r_memberships(real_engine, dest) == {(str(user), "owner", True)}
    companies = await count(real_engine, "companies")

    preview = await _ln_preview(real_client, tok, data)
    assert preview.json()["action"] == "return_existing_and_add_team"
    assert (preview.json()["destination_id"], preview.json()["team_members"]) == (dest, 2)
    r = await _ln_commit(real_client, tok, preview)
    assert r.status_code == 200, r.text
    assert (r.json()["company_id"], r.json()["outcome"], r.json()["team_members"]) == (dest, "opened_existing_team_added", 2)
    assert await _r_memberships(real_engine, dest) == {
        (str(user), "owner", True), (str(viewer), "viewer", True), (str(manager), "manager", True)}
    assert await count(real_engine, "companies") == companies


async def test_team_carry_retry_adds_no_members(real_engine, real_client, tmp_path, monkeypatch):
    """Once the team is carried, repeating the restore, with the same or a new preview, adds nobody."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "viewer", "manager")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    preview = await _ln_preview(real_client, tok, data)
    first = await _ln_commit(real_client, tok, preview)
    assert first.status_code == 200 and first.json()["team_members"] == 2, first.text
    after = await _r_memberships(real_engine, dest)

    again = await _ln_commit(real_client, tok, preview)
    assert again.status_code == 200, again.text
    assert (again.json()["company_id"], again.json()["team_members"]) == (dest, 0)
    later = await _ln_preview(real_client, tok, data)
    assert (later.json()["action"], later.json()["team_members"]) == ("return_existing", 0)
    r = await _ln_commit(real_client, tok, later)
    assert r.status_code == 200 and r.json()["team_members"] == 0, r.text
    assert await _r_memberships(real_engine, dest) == after


async def test_team_carry_keeps_existing_member_role(real_engine, real_client, tmp_path, monkeypatch):
    """A team member who already has a role in the existing company keeps that role."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    clerk, buyer = await _ln_team(real_engine, cid, "viewer", "manager")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    await member(real_engine, clerk, dest, "admin")
    preview = await _ln_preview(real_client, tok, data)
    assert preview.json()["team_members"] == 1
    r = await _ln_commit(real_client, tok, preview)
    assert r.status_code == 200 and r.json()["team_members"] == 1, r.text
    assert await _r_memberships(real_engine, dest) == {
        (str(user), "owner", True), (str(clerk), "admin", True), (str(buyer), "manager", True)}


async def test_team_carry_keeps_inactive_member_inactive(real_engine, real_client, tmp_path, monkeypatch):
    """A team member whose access to the existing company was removed stays without access."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    clerk, buyer = await _ln_team(real_engine, cid, "viewer", "manager")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    await member(real_engine, clerk, dest, "operator", active=False)
    preview = await _ln_preview(real_client, tok, data)
    assert preview.json()["team_members"] == 1
    r = await _ln_commit(real_client, tok, preview)
    assert r.status_code == 200 and r.json()["team_members"] == 1, r.text
    assert await _r_memberships(real_engine, dest) == {
        (str(user), "owner", True), (str(clerk), "operator", False), (str(buyer), "manager", True)}


async def test_team_carry_refused_for_destination_non_owner(real_engine, real_client, tmp_path, monkeypatch):
    """A restoring owner who is not an owner of the existing company opens it but adds nobody,
    and the preview says how many were not added."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "viewer")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    second = await owner(real_engine, "second@example.com", "Second")
    await member(real_engine, second, cid, "owner")
    await member(real_engine, second, dest, "manager")
    second_tok = await token(real_engine, second, cid)
    before = await _r_memberships(real_engine, dest)

    preview = await _ln_preview(real_client, second_tok, data)
    body = preview.json()
    assert (body["action"], body["destination_id"], body["team_members"], body["team_blocked"]) == (
        "return_existing", dest, 0, 1)
    r = await _ln_commit(real_client, second_tok, preview)
    assert r.status_code == 200, r.text
    assert (r.json()["company_id"], r.json()["team_members"]) == (dest, 0)
    assert await _r_memberships(real_engine, dest) == before


async def test_team_carry_returns_actual_count(real_engine, real_client, tmp_path, monkeypatch):
    """The restore reports exactly how many memberships it added, skipping members who
    already have a membership row in the existing company, active or not."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    present, removed, _ = await _ln_team(real_engine, cid, "viewer", "manager", "admin")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    await member(real_engine, present, dest, "viewer")
    await member(real_engine, removed, dest, "manager", active=False)
    rows = await count(real_engine, "user_companies", "company_id = CAST(:c AS uuid)", c=dest)
    r = await _ln_commit(real_client, tok, await _ln_preview(real_client, tok, data))
    assert r.status_code == 200, r.text
    added = await count(real_engine, "user_companies", "company_id = CAST(:c AS uuid)", c=dest) - rows
    assert r.json()["team_members"] == added == 1


async def test_team_carry_preview_count_equals_commit_count(real_engine, real_client, tmp_path, monkeypatch):
    """The preview's team count is the count the restore adds, for a new and for an existing company."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "viewer", "manager")

    data = await download(real_client, tok)
    preview = await _ln_preview(real_client, tok, data)
    assert preview.json()["action"] == "create"
    created = _r_created(await _ln_commit(real_client, tok, preview))
    assert preview.json()["team_members"] == created["team_members"] == 2
    assert len(await _r_memberships(real_engine, created["company_id"])) == 3

    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    preview = await _ln_preview(real_client, tok, data)
    r = await _ln_commit(real_client, tok, preview)
    assert r.status_code == 200, r.text
    assert preview.json()["team_members"] == r.json()["team_members"] == 2
    assert len(await _r_memberships(real_engine, dest)) == 3


async def test_team_carry_copies_source_role_grants_once(real_engine, real_client, tmp_path, monkeypatch):
    """The first team carry into a company restored through Add Company brings the source's role
    permissions; a later carry never writes them again."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "admin")
    await _ln_set_grant(real_client, tok, "manage_users", "admin", False)
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    assert await _ln_grants(real_engine, dest) is None
    assert (await _ln_company(real_engine, dest))["settings"]["restored_backup"]["team_policy_carried"] is False

    preview = await _ln_preview(real_client, tok, data)
    assert preview.json()["scope"]["role_permissions"] == "source"
    r = await _ln_commit(real_client, tok, preview)
    assert r.status_code == 200, r.text
    carried = await _ln_grants(real_engine, dest)
    assert carried == await _ln_grants(real_engine, cid) and carried
    assert (await _ln_company(real_engine, dest))["settings"]["restored_backup"]["team_policy_carried"] is True

    await _ln_set_grant(real_client, tok, "manage_integrations", "admin", False)
    await _ln_team(real_engine, cid, "viewer")
    preview = await _ln_preview(real_client, tok, data)
    assert preview.json()["action"] == "return_existing_and_add_team"
    assert preview.json()["scope"]["role_permissions"] == "destination"
    r = await _ln_commit(real_client, tok, preview)
    assert r.status_code == 200 and r.json()["team_members"] == 1, r.text
    assert await _ln_grants(real_engine, dest) == carried != await _ln_grants(real_engine, cid)


async def test_team_carry_never_overwrites_destination_role_grants(real_engine, real_client, tmp_path, monkeypatch):
    """Role permissions the existing company set for itself are never replaced by the source's."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "admin")
    await _ln_set_grant(real_client, tok, "manage_users", "admin", False)
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    await _ln_set_grant(real_client, await token(real_engine, user, dest), "manage_integrations", "admin", False)
    own = await _ln_grants(real_engine, dest)
    assert own and own != await _ln_grants(real_engine, cid)

    preview = await _ln_preview(real_client, tok, data)
    assert preview.json()["scope"]["role_permissions"] == "destination"
    r = await _ln_commit(real_client, tok, preview)
    assert r.status_code == 200 and r.json()["team_members"] == 1, r.text
    assert await _ln_grants(real_engine, dest) == own


@pytest.mark.parametrize("mode, carried", [("settings", True), ("new_company", False)])
async def test_same_lineage_new_destination_records_policy_carried(real_engine, real_client, tmp_path, monkeypatch,
                                                                   mode, carried):
    """A company created from its own company's Settings records that the team and its role
    permissions were carried; one created through Add Company records that they were not."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "admin")
    await _ln_set_grant(real_client, tok, "manage_users", "admin", False)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, mode))
    restored = (await _ln_company(real_engine, body["company_id"]))["settings"]
    assert restored["restored_backup"]["team_policy_carried"] is carried
    assert (restored.get("role_grants") == await _ln_grants(real_engine, cid)) is carried


async def test_team_carry_preview_states_destination_permission_policy(real_engine, real_client, tmp_path, monkeypatch):
    """The preview says whose role permissions the added team works under: the source's when
    they will be carried, the existing company's when it has its own."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "admin")
    await _ln_set_grant(real_client, tok, "manage_users", "admin", False)

    data = await download(real_client, tok)
    await _ln_add_company(real_client, tok, data)
    body = (await _ln_preview(real_client, tok, data)).json()
    assert (body["action"], body["scope"]["role_permissions"]) == (
        "return_existing_and_add_team", "source")

    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    await _ln_set_grant(real_client, await token(real_engine, user, dest), "manage_integrations", "admin", False)
    body = (await _ln_preview(real_client, tok, data)).json()
    assert (body["action"], body["scope"]["role_permissions"]) == (
        "return_existing_and_add_team", "destination")


async def test_team_carry_concurrent_membership_change_revalidated(real_engine, real_client, tmp_path, monkeypatch):
    """The restore rechecks the caller's ownership when it commits: a demotion after the
    preview refuses the stale plan and adds nobody, and a demotion racing the commit waits for it."""
    from test_company_settings_race_pg import _hold_first_call, _until_blocked_or_done
    cb = _bk_cb()
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    [clerk] = await _ln_team(real_engine, cid, "viewer")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    demote = ("UPDATE user_companies SET role = :r WHERE user_id = :u AND company_id = CAST(:c AS uuid)")

    preview = await _ln_preview(real_client, tok, data)
    await _bk_sql(real_engine, demote, r="manager", u=user, c=dest)
    stale = await _ln_commit(real_client, tok, preview)
    assert stale.status_code == 409, stale.text
    plan = stale.json()["plan"]
    assert (plan["action"], plan["team_members"], plan["team_blocked"]) == ("return_existing", 0, 1)
    assert await _r_memberships(real_engine, dest) == {(str(user), "manager", True)}

    await _bk_sql(real_engine, demote, r="owner", u=user, c=dest)
    preview = await _ln_preview(real_client, tok, data)
    paused, release = _hold_first_call(monkeypatch, cb, "_add_team")
    committing = asyncio.create_task(_ln_commit(real_client, tok, preview))
    await asyncio.wait_for(paused.wait(), timeout=10)
    demoting = asyncio.create_task(_bk_sql(real_engine, demote, r="manager", u=user, c=dest))
    await _until_blocked_or_done(real_engine, demoting)
    assert not demoting.done()
    release.set()
    r = await committing
    await demoting
    assert r.status_code == 200 and r.json()["team_members"] == 1, r.text
    assert await _r_memberships(real_engine, dest) == {(str(user), "manager", True), (str(clerk), "viewer", True)}


async def test_concurrent_same_backup_restore_with_existing_destination(real_engine, real_client, tmp_path,
                                                                        monkeypatch):
    """Two simultaneous Settings restores onto an existing company add the missing team once."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "viewer", "manager")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    companies = await count(real_engine, "companies")
    previews = [await _ln_preview(real_client, tok, data) for _ in range(2)]
    results = await asyncio.gather(*(_ln_commit(real_client, tok, p) for p in previews))
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert {r.json()["company_id"] for r in results} == {dest}
    assert sorted(r.json()["team_members"] for r in results) == [0, 2]
    assert len(await _r_memberships(real_engine, dest)) == 3
    assert await count(real_engine, "companies") == companies


async def test_inactive_prior_restore_detected(real_engine, real_client, tmp_path, monkeypatch):
    """A backup already restored as a company that was later deactivated is recognised as
    restored, from Settings and from Add Company."""
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    for mode in ("new_company", "settings"):
        body = (await _ln_preview(real_client, tok, data, mode)).json()
        assert (body["action"], body["destination_id"], body["destination_name"]) == (
            "offer_reactivate", dest, "Alpha Trading (Restored)"), mode


async def test_inactive_prior_restore_creates_no_duplicate(real_engine, real_client, tmp_path, monkeypatch):
    """Confirming a restore of a backup whose company is deactivated creates no second company."""
    cb = _bk_cb()
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    companies = await count(real_engine, "companies")
    for mode in ("new_company", "settings"):
        preview = await _ln_preview(real_client, tok, data, mode)
        r = await _ln_commit(real_client, tok, preview, mode)
        assert r.status_code == 409, r.text
        with pytest.raises(cb.BackupError) as err:
            await cb.restore_company(_bk_file(tmp_path, data), mode=mode, user_id=user, current_company_id=cid,
                                     plan_fingerprint=preview.json()["plan_fingerprint"])
        assert err.value.status_code == 409
    assert await count(real_engine, "companies") == companies


async def test_inactive_prior_restore_offers_owner_reactivation(real_engine, real_client, tmp_path, monkeypatch):
    """The owner of the deactivated company is offered its reactivation, and taking it brings
    that company back instead of a copy."""
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    companies = await count(real_engine, "companies")
    preview = await _ln_preview(real_client, tok, data, "new_company")
    assert preview.json()["action"] == "offer_reactivate"
    r = await _ln_reactivate(real_client, tok, preview)
    assert r.status_code == 200, r.text
    assert (r.json()["company_id"], r.json()["outcome"]) == (dest, "reactivated")
    assert (await _ln_company(real_engine, dest))["is_active"] is True
    assert await count(real_engine, "companies") == companies


async def test_inactive_prior_restore_refuses_member_and_non_member(real_engine, real_client, tmp_path, monkeypatch):
    """A member who is not its owner and a stranger get the same refusal as for any company they
    may not open, from preview, restore and reactivation alike, and nothing changes."""
    cb = _bk_cb()
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    manager = await owner(real_engine, "manager@example.com", "Manager")
    await member(real_engine, manager, dest, "manager")
    stranger = await owner(real_engine, "stranger@example.com", "Stranger")
    callers = []
    for who, name in ((manager, "Manager Co"), (stranger, "Stranger Co")):
        own = await company(real_engine, who, name, f"{name.lower().replace(' ', '-')}-marker")
        callers.append((who, await token(real_engine, who, own)))

    before = await snapshot(real_engine)
    refusals = []
    for who, who_tok in callers:
        refusals.append(await read(real_client, who_tok, data, mode="new_company"))
        for route in ("restore", "reactivate"):
            upload = await _ln_stage(tmp_path, who, data)
            refusals.append(await real_client.post(f"/company-backups/{route}", headers=auth(who_tok), json={
                "upload_token": upload, "mode": "new_company", "plan_fingerprint": "0" * 64}))
    assert [r.status_code for r in refusals] == [409] * 6, [r.text for r in refusals]
    assert refusals[0].json() == {"detail": cb.NOT_A_MEMBER}
    assert len({r.content for r in refusals}) == 1
    assert await snapshot(real_engine) == before

    r = await _ln_reactivate(real_client, tok, await _ln_preview(real_client, tok, data, "new_company"))
    assert r.status_code == 200, r.text
    active = await read(real_client, callers[1][1], data, mode="new_company")
    assert (active.status_code, active.content) == (409, refusals[0].content), active.text


async def test_restore_never_reactivates_company(real_engine, real_client, tmp_path, monkeypatch):
    """Confirming a restore, even as the owner and with a current preview, leaves a deactivated
    company deactivated; only the explicit reactivation brings it back."""
    cb = _bk_cb()
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    slug = (await _ln_company(real_engine, dest))["slug"]
    for mode in ("new_company", "settings"):
        preview = await _ln_preview(real_client, tok, data, mode)
        assert (await _ln_commit(real_client, tok, preview, mode)).status_code == 409
        with pytest.raises(cb.BackupError):
            await cb.restore_company(_bk_file(tmp_path, data), mode=mode, user_id=user, current_company_id=cid,
                                     plan_fingerprint=preview.json()["plan_fingerprint"])
    assert (await _ln_company(real_engine, dest))["is_active"] is False
    assert (await _ln_company(real_engine, dest))["slug"] == slug


async def test_me_reactivate_uses_canonical_reactivation(real_engine, real_client, tmp_path, monkeypatch):
    """Reactivating the session's company and reactivating a restored company both go through the
    one reactivation operation."""
    import celerp.routers.companies as companies_router
    from celerp.services import company_lifecycle
    calls = []
    real = company_lifecycle.reactivate_company

    async def spy(session, company_id, user_id):
        calls.append((str(company_id), str(user_id)))
        return await real(session, company_id, user_id)

    monkeypatch.setattr(company_lifecycle, "reactivate_company", spy)
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    await _ln_deactivate(real_engine, real_client, user, cid)
    r = await real_client.post("/companies/me/reactivate", headers=auth(tok))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "company_id": str(cid), "is_active": True, "connectors_to_reconnect": []}
    r = await _ln_reactivate(real_client, tok, await _ln_preview(real_client, tok, data, "new_company"))
    assert r.status_code == 200, r.text
    assert calls == [(str(cid), str(user)), (dest, str(user))]
    assert "is_active = True" not in inspect.getsource(companies_router.reactivate_company)


async def test_reactivate_existing_company_switches_session(real_engine, real_client, tmp_path, monkeypatch):
    """Reactivating a restored company signs the owner into it; the old session stays where it was."""
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    r = await _ln_reactivate(real_client, tok, await _ln_preview(real_client, tok, data, "new_company"))
    assert r.status_code == 200, r.text
    body = r.json()
    me = await _r_me(real_client, body["access_token"])
    assert (me["id"], me["current_role"]) == (dest, "owner")
    refreshed = await real_client.post("/auth/token/refresh", json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    assert (await _r_me(real_client, refreshed.json()["access_token"]))["id"] == dest
    assert (await _r_me(real_client, tok))["id"] == str(cid)


async def test_reactivate_existing_company_with_reused_slug(real_engine, real_client, tmp_path, monkeypatch):
    """A company whose web address was taken while it was deactivated comes back under a free one,
    through both reactivation routes."""
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    base = _LN_SLUG_SUFFIX.sub("", (await _ln_company(real_engine, dest))["slug"])
    taker = await company(real_engine, user, "Beta Trading", "beta-marker")
    await _bk_sql(real_engine, "UPDATE companies SET slug = :s WHERE id = :c", s=base, c=taker)
    r = await _ln_reactivate(real_client, tok, await _ln_preview(real_client, tok, data, "new_company"))
    assert r.status_code == 200, r.text
    slug = (await _ln_company(real_engine, dest))["slug"]
    assert slug != base and slug.startswith(base) and not _LN_SLUG_SUFFIX.search(slug)

    await _ln_deactivate(real_engine, real_client, user, dest)
    other = await company(real_engine, user, "Delta Trading", "delta-marker")
    await _bk_sql(real_engine, "UPDATE companies SET slug = :s WHERE id = :c", s=slug, c=other)
    r = await real_client.post("/companies/me/reactivate", headers=auth(await token(real_engine, user, dest)))
    assert r.status_code == 200, r.text
    again = await _ln_company(real_engine, dest)
    assert again["is_active"] is True and again["slug"] not in (base, slug)
    assert not _LN_SLUG_SUFFIX.search(again["slug"])


async def test_reactivate_existing_company_retry_is_idempotent(real_engine, real_client, tmp_path, monkeypatch):
    """Repeating a reactivation returns the same company, already active, and changes nothing."""
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    preview = await _ln_preview(real_client, tok, data, "new_company")
    first = await _ln_reactivate(real_client, tok, preview)
    assert first.status_code == 200 and first.json()["outcome"] == "reactivated", first.text
    state = await _ln_company(real_engine, dest)
    companies = await count(real_engine, "companies")
    for again in (await _ln_reactivate(real_client, tok, preview),
                  await _ln_reactivate(real_client, tok, await _ln_preview(real_client, tok, data, "new_company"))):
        assert again.status_code == 200, again.text
        assert (again.json()["company_id"], again.json()["outcome"]) == (dest, "opened_existing")
    assert await _ln_company(real_engine, dest) == state
    assert await count(real_engine, "companies") == companies


async def test_reactivate_existing_company_connectors_await_reconnect(real_engine, real_client, tmp_path, monkeypatch):
    """Reactivating a restored company names the connectors its deactivation disconnected and
    reconnects none of them."""
    from unittest.mock import AsyncMock, patch

    from celerp.connectors.ownership import connectors_awaiting_reconnect
    from celerp.models.connector_config import ConnectorConfig
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)
    async with maker(real_engine)() as s:
        s.add(ConnectorConfig(company_id=dest, connector="woocommerce"))
        await s.commit()
    with patch("celerp.connectors.remote_state.revoke_connector_remote_state", AsyncMock()):
        await _ln_deactivate(real_engine, real_client, user, dest)

    r = await _ln_reactivate(real_client, tok, await _ln_preview(real_client, tok, data, "new_company"))
    assert r.status_code == 200, r.text
    assert r.json()["connectors_to_reconnect"] == ["woocommerce"]
    assert await count(real_engine, "connector_configs", "company_id = :c", c=dest) == 0
    async with maker(real_engine)() as s:
        assert await connectors_awaiting_reconnect(s, uuid.UUID(dest)) == ["woocommerce"]


async def test_concurrent_reactivation_reactivates_once(real_engine, real_client, tmp_path, monkeypatch):
    """Two simultaneous reactivations of the same restored company reactivate it once and both
    return it."""
    from test_company_settings_race_pg import _hold_first_call, _until_blocked_or_done

    from celerp.connectors import ownership
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    previews = [await _ln_preview(real_client, tok, data, "new_company") for _ in range(2)]
    paused, release = _hold_first_call(monkeypatch, ownership, "connectors_awaiting_reconnect")
    first = asyncio.create_task(_ln_reactivate(real_client, tok, previews[0]))
    await asyncio.wait_for(paused.wait(), timeout=10)
    second = asyncio.create_task(_ln_reactivate(real_client, tok, previews[1]))
    await _until_blocked_or_done(real_engine, second)
    release.set()
    results = [await first, await second]
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert [r.json()["outcome"] for r in results] == ["reactivated", "opened_existing"]
    assert {r.json()["company_id"] for r in results} == {dest}


async def test_reactivation_failure_issues_no_session(real_engine, real_client, tmp_path, monkeypatch):
    """A reactivation that fails leaves the company deactivated and signs nobody in."""
    from celerp.connectors import ownership
    user, cid, tok, data, dest = await _ln_inactive_destination(real_engine, real_client, tmp_path, monkeypatch)
    preview = await _ln_preview(real_client, tok, data, "new_company")
    state = await _ln_company(real_engine, dest)
    sessions = await count(real_engine, "session_registry")

    async def broken(*args, **kwargs):
        raise RuntimeError("connector state unavailable")

    monkeypatch.setattr(ownership, "connectors_awaiting_reconnect", broken)
    try:
        status = (await _ln_reactivate(real_client, tok, preview)).status_code
    except RuntimeError:
        status = 500
    assert status == 500
    assert await _ln_company(real_engine, dest) == state
    assert await count(real_engine, "session_registry") == sessions


async def test_restore_refuses_attachment_missing_from_archive(real_engine, real_client, tmp_path, monkeypatch):
    """A backup whose records point at an attachment file of its company that it does not
    carry is refused before anything is written."""
    _bk_local(monkeypatch, tmp_path)
    user, cid, tok = await _bk_setup(real_engine)
    await _bk_point_at(real_engine, cid, _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo"))
    parts = members(await download(real_client, tok))
    m = json.loads(parts.pop("manifest.json"))
    [entry] = m["attachments"]
    del parts[f"attachments/{entry['name']}"]
    m["attachments"] = []
    data = rezip({**parts, "manifest.json": json.dumps(m).encode()})
    await _bk_refused(real_engine, real_client, tok, user, tmp_path, data, "does not carry")


async def test_reactivate_without_prior_restore_is_stale_preview(real_engine, real_client, tmp_path, monkeypatch):
    """Reactivating a backup that was never restored answers with the current plan, which is to
    create a company, and changes nothing."""
    _r_env(tmp_path, monkeypatch)
    _user, _cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    preview = await _ln_preview(real_client, tok, data, "new_company")
    assert preview.json()["action"] == "create"
    before = await snapshot(real_engine)
    r = await _ln_reactivate(real_client, tok, preview)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "stale_preview" and r.json()["plan"]["action"] == "create"
    assert await snapshot(real_engine) == before


@pytest.mark.parametrize("existing", [False, True])
async def test_team_carry_source_permissions_changed_after_preview_is_stale(real_engine, real_client, tmp_path,
                                                                            monkeypatch, existing):
    """The role permissions a team carry copies are the ones its preview showed: a change to
    the source company's permissions after the preview refuses the stale plan and writes nothing."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _ln_team(real_engine, cid, "admin")
    await _ln_set_grant(real_client, tok, "manage_users", "admin", False)
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data) if existing else None
    companies = await count(real_engine, "companies")

    preview = await _ln_preview(real_client, tok, data)
    assert preview.json()["scope"]["role_permissions"] == "source"
    await _ln_set_grant(real_client, tok, "manage_integrations", "admin", False)
    stale = await _ln_commit(real_client, tok, preview)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_preview", stale.text
    assert await count(real_engine, "companies") == companies
    if existing:
        assert await _ln_grants(real_engine, dest) is None

    r = await _ln_commit(real_client, tok, await _ln_preview(real_client, tok, data))
    assert r.status_code in (200, 201), r.text
    assert await _ln_grants(real_engine, r.json()["company_id"]) == await _ln_grants(real_engine, cid)


async def test_source_membership_change_waits_for_team_carry(real_engine, real_client, tmp_path, monkeypatch):
    """A role change on the source company waits for a team carry in progress, so the carry
    copies the team as its preview showed it and the change lands after."""
    from test_company_settings_race_pg import _hold_first_call, _until_blocked_or_done
    cb = _bk_cb()
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    [clerk] = await _ln_team(real_engine, cid, "viewer")
    data = await download(real_client, tok)
    dest = await _ln_add_company(real_client, tok, data)

    preview = await _ln_preview(real_client, tok, data)
    paused, release = _hold_first_call(monkeypatch, cb, "_add_team")
    committing = asyncio.create_task(_ln_commit(real_client, tok, preview))
    await asyncio.wait_for(paused.wait(), timeout=10)
    promoting = asyncio.create_task(real_client.patch(f"/companies/me/users/{clerk}", json={"role": "manager"},
                                                      headers=auth(tok)))
    await _until_blocked_or_done(real_engine, promoting)
    assert not promoting.done()
    release.set()
    r = await committing
    assert r.status_code == 200 and r.json()["team_members"] == 1, r.text
    assert (await promoting).status_code == 200
    assert (str(clerk), "viewer", True) in await _r_memberships(real_engine, dest)
    assert (str(clerk), "manager", True) in await _r_memberships(real_engine, cid)


async def test_module_tables_travel_only_as_their_manifest_declares(real_engine, real_client, tmp_path, monkeypatch):
    """A module table travels with a company backup only when the module's manifest includes
    it: an excluded table (installation state such as credentials) is never carried, and a
    table the manifest does not name stops the export and is not restored."""
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch, backup={"zz_widgets": "include", "zz_tokens": "exclude"})
    user, cid, tok = await _bk_setup(real_engine, settings={"enabled_modules": [_BK_MODULE]})
    await _bk_sql(real_engine, "CREATE TABLE zz_widgets (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade)")
    await _bk_sql(real_engine, "CREATE TABLE zz_tokens (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, secret text)")
    try:
        await _bk_sql(real_engine, "INSERT INTO zz_widgets (id, company_id) VALUES (:i, :c)", i=uuid.uuid4(), c=cid)
        await _bk_sql(real_engine, "INSERT INTO zz_tokens (id, company_id, secret) VALUES (:i, :c, 'tok-marker')",
                      i=uuid.uuid4(), c=cid)
        data = await download(real_client, tok)
        assert "zz_widgets" in manifest(data)["tables"] and "zz_tokens" not in manifest(data)["tables"]
        assert not any(b"tok-marker" in body for body in members(data).values())

        _bk_fake_module(tmp_path, monkeypatch, backup={"zz_tokens": "exclude"})
        r = await real_client.get("/company-backups/download", headers=auth(tok))
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "Widgets" in detail and "zz_" not in detail and detail.endswith("Nothing was backed up.")
        await _bk_refused(real_engine, real_client, tok, user, tmp_path, data)
    finally:
        await _bk_drop(real_engine, "zz_tokens", "zz_widgets")


async def test_attachments_of_a_restore_that_stopped_are_reconciled(real_engine, real_client, tmp_path, monkeypatch):
    """Attachment files stored by a restore that stopped before it committed, with no chance to
    clean up, are removed when restores are reconciled; the files of a restore still in
    progress, and of one that committed, stay."""
    from test_company_settings_race_pg import _hold_first_call
    cb = _bk_cb()
    _bk_local(monkeypatch, tmp_path)
    _, cid, tok = await _bk_setup(real_engine)
    await _bk_point_at(real_engine, cid, _bk_local_file(tmp_path, cid, "photo.png", b"alpha-photo"))
    data = await download(real_client, tok)
    stored = tmp_path / "static" / "attachments"

    async def cleanup_never_runs(company_id):
        raise OSError("stopped before cleaning up")

    async def stops(*args, **kwargs):
        raise cb.BackupError(422, "stopped")
    real_delete, real_verify = cb.attachments.delete_company_files, cb._verify
    monkeypatch.setattr(cb.attachments, "delete_company_files", cleanup_never_runs)
    monkeypatch.setattr(cb, "_verify", stops)
    assert (await restore(real_client, tok, data, "new_company")).status_code == 422
    monkeypatch.setattr(cb.attachments, "delete_company_files", real_delete)
    monkeypatch.setattr(cb, "_verify", real_verify)
    [orphan] = [p.name for p in stored.iterdir() if p.name != str(cid)]
    assert await count(real_engine, "companies") == 1

    paused, release = _hold_first_call(monkeypatch, cb, "_insert")
    committing = asyncio.create_task(restore(real_client, tok, data, "new_company"))
    await asyncio.wait_for(paused.wait(), timeout=10)
    await cb.reconcile_landings()
    [landing] = [p.name for p in stored.iterdir() if p.name not in (str(cid), orphan)] or [None]
    assert not (stored / orphan).exists()
    assert landing is not None and (stored / landing / "photo.png").is_file()
    release.set()
    new = _r_created(await committing)["company_id"]
    assert new == landing and (stored / new / "photo.png").read_bytes() == b"alpha-photo"

    cb.attachments.mark_landing(new)
    await cb.reconcile_landings()
    assert (stored / new / "photo.png").is_file() and cb.attachments.landing_companies() == []
    assert (stored / str(cid) / "photo.png").read_bytes() == b"alpha-photo"

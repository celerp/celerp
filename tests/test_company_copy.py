# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Independent company copies: made for one company, opened as a new company,
checked record for record, and refused whole when anything is wrong."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import uuid
import zipfile

import pytest
from sqlalchemy import text

from migration_support import OWNER_EMAIL, auth, code_config, count, maker, real_client, real_engine  # noqa: F401
from test_helpers import make_authed_token

pytestmark = pytest.mark.asyncio

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SECRET = "whsec-connector-secret-7f3a"
SHARE = "share-token-9c1d0e"


async def _owner(engine, email: str = OWNER_EMAIL):
    from celerp.models.company import User
    from celerp.services.provisioning import create_install_owner
    async with maker(engine)() as s:
        if email == OWNER_EMAIL:
            user = await create_install_owner(s, name="Owner", email=email, password="ownerpw123")
        else:
            user = User(email=email, name="Clerk")
            s.add(user)
        await s.commit()
        return user.id


async def _company(engine, user_id, name: str, marker: str, *, settings: dict | None = None):
    """A company with one location, one ledger event and one projection carrying ``marker``."""
    from celerp.models.company import User
    from celerp.services.provisioning import provision_migration_company
    async with maker(engine)() as s:
        user = await s.get(User, user_id)
        company = await provision_migration_company(s, owner=user, company_name=name)
        company.is_active, company.is_migration_staged = True, False
        cid, loc = company.id, uuid.uuid4()
        await s.execute(text("UPDATE companies SET settings = CAST(:s AS json) WHERE id = :c"),
                        {"s": json.dumps(settings or {}), "c": company.id})
        await s.execute(text("INSERT INTO locations (id, company_id, name, type, is_default, created_at) "
                             "VALUES (:id, :c, :n, 'warehouse', true, now())"), {"id": loc, "c": cid, "n": f"{marker} store"})
        await s.execute(text(
            "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, actor_id, location_id, "
            "source, idempotency_key) VALUES (:c, 'item:1', 'item', 'item.created', CAST(:d AS json), :u, :l, 'api', :k)"),
            {"c": cid, "d": json.dumps({"name": marker, "location_id": str(loc)}), "u": user_id, "l": loc,
             "k": f"k-{marker}"})
        await s.execute(text(
            "INSERT INTO projections (company_id, entity_id, entity_type, state, version, location_id, updated_at) "
            "VALUES (:c, 'item:1', 'item', CAST(:d AS json), 1, :l, now())"),
            {"c": cid, "d": json.dumps({"name": marker, "location_id": str(loc)}), "l": loc})
        await s.commit()
        return cid


async def _token(engine, user_id, company_id, role: str = "owner") -> str:
    async with maker(engine)() as s:
        return await make_authed_token(s, str(user_id), str(company_id), role)


async def _make_copy(client, token: str, prepared_by: str = "Example Accounting") -> tuple[bytes, dict]:
    r = await client.post("/company-copies", json={"prepared_by": prepared_by}, headers=auth(token))
    assert r.status_code == 201, r.text
    made = r.json()
    r = await client.get(f"/company-copies/{made['copy_id']}/download", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.content, made


async def _open(client, token: str, data: bytes):
    r = await client.post("/company-copies/read", files={"file": ("books.celerp-company", data)}, headers=auth(token))
    if r.status_code != 200:
        return r
    return await client.post("/company-copies/open", json={"upload_token": r.json()["upload_token"]},
                             headers=auth(token))


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _rezip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


async def _snapshot(engine) -> dict:
    """Every row of every table, for proving an operation changed nothing."""
    async with engine.connect() as conn:
        tables = (await conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"))).scalars().all()
        return {t: sorted((await conn.execute(text(f'SELECT to_jsonb(x)::text FROM "{t}" x'))).scalars().all())
                for t in tables}


# ── Classification ───────────────────────────────────────────────────────────

async def test_every_company_table_classified(real_engine):
    """Every table holding company data is either copied or excluded with a reason."""
    from celerp.services import company_copy as cc
    from celerp.services.migrations import company_tables

    async with maker(real_engine)() as s:
        tables = set(await company_tables(s))
    assert not set(cc.COPY_TABLES) & set(cc.EXCLUDED_TABLES)
    assert tables == set(cc.COPY_TABLES) | set(cc.EXCLUDED_TABLES)
    assert all(cc.EXCLUDED_TABLES.values())


# ── What a copy carries ──────────────────────────────────────────────────────

async def test_copy_isolation(real_engine, real_client, tmp_path, monkeypatch):
    """A copy of one company carries none of another company's records, settings or files."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    a = await _company(real_engine, user, "Alpha Trading", "alpha-marker", settings={"currency": "THB"})
    b = await _company(real_engine, user, "Beta Trading", "beta-marker", settings={"currency": "USD-beta"})
    (tmp_path / "static" / "attachments" / str(b)).mkdir(parents=True)
    (tmp_path / "static" / "attachments" / str(b) / "beta.png").write_bytes(b"beta-file")

    data, _ = await _make_copy(real_client, await _token(real_engine, user, a))
    body = b"".join(_members(data).values())
    assert b"alpha-marker" in body and b"Alpha Trading" in body
    for foreign in (b"beta-marker", b"Beta Trading", b"USD-beta", b"beta-file", str(b).encode()):
        assert foreign not in body


async def test_copy_has_no_secrets(real_engine, real_client, tmp_path, monkeypatch):
    """No sign-in data, people, connector credentials, share links or role grants leave with a copy."""
    from celerp.config import settings
    from celerp.models.company import User
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    a = await _company(real_engine, user, "Alpha Trading", "alpha-marker",
                       settings={"currency": "THB", "role_grants": {"clerk": ["secret-grant"]},
                                 "reorder_alert_email": "alerts@example.com"})
    async with maker(real_engine)() as s:
        await s.execute(text("INSERT INTO connector_configs (company_id, connector, direction, sync_frequency, "
                             "daily_sync_hour, webhook_secret) VALUES (:c, 'shopify', 'both', 'realtime', 2, :w)"),
                        {"c": str(a), "w": SECRET})
        await s.execute(text("INSERT INTO doc_share_tokens (id, token, company_id, entity_id, created_at) "
                             "VALUES (:i, :t, :c, 'item:1', now())"), {"i": uuid.uuid4(), "t": SHARE, "c": a})
        owner = await s.get(User, user)
        auth_hash = owner.auth_hash
        await s.commit()

    data, _ = await _make_copy(real_client, await _token(real_engine, user, a))
    body = b"".join(_members(data).values())
    for secret in (SECRET, SHARE, "secret-grant", "alerts@example.com", OWNER_EMAIL, auth_hash, str(user)):
        assert secret.encode() not in body, secret
    manifest = json.loads(_members(data)["manifest.json"])
    assert manifest["company"]["settings"] == {"currency": "THB"}
    assert manifest["prepared_by"] == "Example Accounting"


async def test_attachments_round_trip(real_engine, real_client, tmp_path, monkeypatch):
    """Attachment files go with their company and point at the new company's folder once opened."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    a = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    folder = tmp_path / "static" / "attachments" / str(a)
    folder.mkdir(parents=True)
    (folder / "photo.png").write_bytes(b"alpha-photo")
    async with maker(real_engine)() as s:
        await s.execute(text("UPDATE projections SET state = CAST(:d AS json) WHERE company_id = :c"),
                        {"c": a, "d": json.dumps({"attachments": [{"url": f"/static/attachments/{a}/photo.png"}]})})
        await s.commit()
    token = await _token(real_engine, user, a)
    data, _ = await _make_copy(real_client, token)
    r = await _open(real_client, token, data)
    assert r.status_code == 201, r.text
    new = r.json()["company_id"]
    assert (tmp_path / "static" / "attachments" / new / "photo.png").read_bytes() == b"alpha-photo"
    assert (folder / "photo.png").read_bytes() == b"alpha-photo"
    async with real_engine.connect() as conn:
        state = (await conn.execute(text("SELECT state::text FROM projections WHERE company_id = :c"), {"c": new})).scalar_one()
    assert f"/static/attachments/{new}/photo.png" in state and str(a) not in state


# ── Opening a copy ───────────────────────────────────────────────────────────

async def test_import_creates_only_new_company(real_engine, real_client, tmp_path, monkeypatch):
    """Opening a copy adds one company, owned by the caller, and changes nothing else."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    a = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    token = await _token(real_engine, user, a)
    data, _ = await _make_copy(real_client, token)
    before = await _snapshot(real_engine)

    r = await _open(real_client, token, data)
    assert r.status_code == 201, r.text
    new = r.json()["company_id"]
    after = await _snapshot(real_engine)
    for table in before:
        kept = [row for row in after[table] if new not in row]
        assert kept == before[table], table
    assert await count(real_engine, "companies") == 2
    assert await count(real_engine, "user_companies", "company_id = :c AND user_id = :u AND role = 'owner'",
                       c=uuid.UUID(new), u=user) == 1
    for table in ("locations", "ledger", "projections"):
        assert await count(real_engine, table, "company_id = :c", c=uuid.UUID(new)) == 1
    async with real_engine.connect() as conn:
        actor = (await conn.execute(text("SELECT actor_id FROM ledger WHERE company_id = :c"), {"c": new})).scalar_one()
        loc = (await conn.execute(text("SELECT id FROM locations WHERE company_id = :c"), {"c": new})).scalar_one()
        ledger_loc = (await conn.execute(text("SELECT location_id FROM ledger WHERE company_id = :c"), {"c": new})).scalar_one()
    assert actor is None and ledger_loc == loc


async def test_open_same_upload_twice_refused(real_engine, real_client, tmp_path, monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    token = await _token(real_engine, user, await _company(real_engine, user, "Alpha Trading", "alpha-marker"))
    data, _ = await _make_copy(real_client, token)
    r = await real_client.post("/company-copies/read", files={"file": ("a.celerp-company", data)}, headers=auth(token))
    upload = {"upload_token": r.json()["upload_token"]}
    assert (await real_client.post("/company-copies/open", json=upload, headers=auth(token))).status_code == 201
    r = await real_client.post("/company-copies/open", json=upload, headers=auth(token))
    assert r.status_code == 409 and "Choose the file again" in r.json()["detail"]
    assert await count(real_engine, "companies") == 2


async def test_failed_verification_keeps_nothing(real_engine, real_client, tmp_path, monkeypatch):
    """When the opened company does not read back exactly as the copy, nothing is kept."""
    from celerp.config import settings
    from celerp.services import company_copy as cc
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    a = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    (tmp_path / "static" / "attachments" / str(a)).mkdir(parents=True)
    (tmp_path / "static" / "attachments" / str(a) / "photo.png").write_bytes(b"alpha-photo")
    token = await _token(real_engine, user, a)
    data, _ = await _make_copy(real_client, token)
    real_insert = cc._insert

    async def dropping(session, table, columns, lines):
        if table != "projections":
            await real_insert(session, table, columns, lines)
    monkeypatch.setattr(cc, "_insert", dropping)
    before = await _snapshot(real_engine)
    r = await _open(real_client, token, data)
    assert r.status_code == 500 and "Nothing was kept" in r.json()["detail"]
    assert await _snapshot(real_engine) == before
    assert sorted(p.name for p in (tmp_path / "static" / "attachments").iterdir()) == [str(a)]


async def test_round_trip_reconciles(real_engine, real_client, monkeypatch, tmp_path):
    """A migrated sample company, copied and opened, reports the same trial balance,
    receivables, payables, bank and stock, document counts and statuses."""
    from celerp.importers.sample import SAMPLE_ARTIFACT
    from celerp.services import migrations
    from test_migration_e2e import migrate

    run, _ = await migrate(real_engine, SAMPLE_ARTIFACT.read_bytes(), SAMPLE_ARTIFACT.name,
                           {"mode": "full_history"}, monkeypatch, tmp_path)
    async with maker(real_engine)() as s:
        from celerp.models.migration import MigrationRun
        await migrations.finalize(s, await s.get(MigrationRun, run.id))
    source, user = run.company_id, run.created_by_user_id
    token = await _token(real_engine, user, source)
    data, _ = await _make_copy(real_client, token)
    r = await _open(real_client, token, data)
    assert r.status_code == 201, r.text
    new = r.json()["company_id"]
    new_token = await _token(real_engine, user, new)

    for path in ("/accounting/trial-balance", "/reports/ar-aging", "/reports/ap-aging",
                 "/accounting/balance-sheet", "/accounting/pnl"):
        left = await real_client.get(path, headers=auth(token))
        right = await real_client.get(path, headers=auth(new_token))
        assert left.status_code == right.status_code == 200, (path, left.text)
        assert _UUID.sub("<id>", right.text) == _UUID.sub("<id>", left.text), path
    measures = ("SELECT entity_type, count(*), coalesce(state->>'status', ''), "
                "coalesce(sum((state->>'quantity')::numeric), 0) FROM projections WHERE company_id = :c "
                "GROUP BY 1, 3 ORDER BY 1, 3")
    async with real_engine.connect() as conn:
        mine = (await conn.execute(text(measures), {"c": source})).all()
        theirs = (await conn.execute(text(measures), {"c": new})).all()
        ledgers = [(await conn.execute(text("SELECT count(*) FROM ledger WHERE company_id = :c"), {"c": c})).scalar_one()
                   for c in (source, new)]
    assert mine and mine == theirs
    assert ledgers[0] > 0 and ledgers[0] == ledgers[1]


# ── Refusals ─────────────────────────────────────────────────────────────────

async def test_export_fails_closed(real_engine, real_client, tmp_path, monkeypatch):
    """Data the copy cannot carry is named and nothing is written: a company table the copy
    does not know, and attachments kept in cloud storage."""
    from celerp.config import settings
    from celerp.services import company_copy as cc
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    a = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    token = await _token(real_engine, user, a)

    monkeypatch.setattr(cc, "COPY_TABLES", tuple(t for t in cc.COPY_TABLES if t != "locations"))
    r = await real_client.post("/company-copies", json={}, headers=auth(token))
    assert r.status_code == 409 and "locations" in r.json()["detail"] and "Nothing was copied" in r.json()["detail"]
    monkeypatch.undo()
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    class Cloud:
        pass
    monkeypatch.setattr(cc, "get_backend", lambda: Cloud())
    async with maker(real_engine)() as s:
        await s.execute(text("UPDATE projections SET state = CAST(:d AS json) WHERE company_id = :c"),
                        {"c": a, "d": json.dumps({"url": "https://bucket.example/attachments/x.png"})})
        await s.commit()
    r = await real_client.post("/company-copies", json={}, headers=auth(token))
    assert r.status_code == 409 and "cloud storage" in r.json()["detail"]
    copies = tmp_path / "company_copies"
    assert not copies.exists() or not any(p.is_file() for p in copies.rglob("*"))


def _tampered(data: bytes) -> bytes:
    m = _members(data)
    m["tables/projections.jsonl"] = m["tables/projections.jsonl"].replace(b"alpha-marker", b"alpha-markex")
    return _rezip(m)


def _newer_format(data: bytes) -> bytes:
    m = _members(data)
    manifest = json.loads(m["manifest.json"])
    manifest["format_version"] = 2
    m["manifest.json"] = json.dumps(manifest).encode()
    return _rezip(m)


def _newer_column(data: bytes) -> bytes:
    m = _members(data)
    manifest = json.loads(m["manifest.json"])
    manifest["tables"]["locations"]["columns"].append("added_later")
    m["manifest.json"] = json.dumps(manifest).encode()
    return _rezip(m)


def _full_backup(_: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("meta.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    return buf.getvalue()


def _with_manifest(data: bytes, **changes) -> bytes:
    """The copy with manifest fields changed; a value of None removes the field."""
    m = _members(data)
    manifest = json.loads(m["manifest.json"])
    for key, value in changes.items():
        if value is None:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    m["manifest.json"] = json.dumps(manifest).encode()
    return _rezip(m)


def _with_settings(data: bytes, settings) -> bytes:
    manifest = json.loads(_members(data)["manifest.json"])
    return _with_manifest(data, company={**manifest["company"], "settings": settings})


def _with_table(data: bytes, table: str, lines: list[bytes]) -> bytes:
    """The copy with one table's rows replaced and its manifest hash made to match."""
    m = _members(data)
    manifest = json.loads(m["manifest.json"])
    m[f"tables/{table}.jsonl"] = b"\n".join(lines)
    text_lines = sorted(line.decode("utf-8", "replace") for line in lines)
    manifest["tables"][table].update(rows=len(lines), sha256=hashlib.sha256("\n".join(text_lines).encode()).hexdigest())
    m["manifest.json"] = json.dumps(manifest).encode()
    return _rezip(m)


def _duplicated_row(data: bytes) -> bytes:
    row = _members(data)["tables/locations.jsonl"].split(b"\n")[0]
    return _with_table(data, "locations", [row, row])


def _missing_column(data: bytes) -> bytes:
    m = _members(data)
    manifest = json.loads(m["manifest.json"])
    manifest["tables"]["locations"]["columns"].remove("company_id")
    m["manifest.json"] = json.dumps(manifest).encode()
    return _rezip(m)


@pytest.mark.parametrize("change, message", [
    (_tampered, "damaged or was changed"),
    (lambda d: _with_manifest(d, created_at=None), "damaged or was changed"),
    (lambda d: _with_manifest(d, handoff_id="not-a-uuid"), "damaged or was changed"),
    (lambda d: _with_manifest(d, prepared_by="Line one\nLine two"), "damaged or was changed"),
    (lambda d: _with_settings(d, "not settings"), "damaged or was changed"),
    (lambda d: _with_table(d, "locations", [b"\xff\xfe"]), "damaged or was changed"),
    (lambda d: _with_table(d, "locations", [b"[1, 2]"]), "damaged or was changed"),
    (lambda d: _with_table(d, "locations", [b"not json"]), "damaged or was changed"),
    (_duplicated_row, "damaged or was changed"),
    (_missing_column, "damaged or was changed"),
    (lambda d: b"not a copy", "not a Celerp company copy"),
    (lambda d: _rezip({"manifest.json": b'{"format": "other"}'}), "not a Celerp company copy"),
    (_newer_format, "newer version of Celerp"),
    (_newer_column, "newer version of Celerp"),
    (_full_backup, "This is a full Celerp backup. Use Restore a Celerp backup instead."),
])
async def test_open_rejects_bad_file(real_engine, real_client, tmp_path, monkeypatch, change, message):
    """A damaged, foreign, newer or full-backup file is refused with its reason and creates nothing."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    token = await _token(real_engine, user, await _company(real_engine, user, "Alpha Trading", "alpha-marker"))
    data, _ = await _make_copy(real_client, token)
    before = await _snapshot(real_engine)
    r = await _open(real_client, token, change(data))
    assert r.status_code == 422 and message in r.json()["detail"], r.text
    assert await _snapshot(real_engine) == before
    assert not any((tmp_path / "company_copies" / "uploads").iterdir())


async def test_open_rejects_foreign_reference(real_engine, real_client, tmp_path, monkeypatch):
    """A row pointing at a record outside the copy (another company's location) is refused
    before anything is written."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    a = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    b = await _company(real_engine, user, "Beta Trading", "beta-marker")
    token = await _token(real_engine, user, a)
    data, _ = await _make_copy(real_client, token)
    async with real_engine.connect() as conn:
        own, foreign = [(await conn.execute(text("SELECT id::text FROM locations WHERE company_id = :c"),
                                            {"c": c})).scalar_one() for c in (a, b)]
    m = _members(data)
    for table in ("ledger", "projections"):
        lines = m[f"tables/{table}.jsonl"].split(b"\n")
        data = _with_table(data, table, [line.replace(own.encode(), foreign.encode()) for line in lines])
    before = await _snapshot(real_engine)
    r = await _open(real_client, token, data)
    assert r.status_code == 422 and "damaged or was changed" in r.json()["detail"], r.text
    assert await _snapshot(real_engine) == before


async def test_bootstrap_read_takes_large_files(real_engine, real_client, tmp_path, monkeypatch):
    """A copy file larger than the request body cap still reaches the fresh-installation read."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    big = b"\0" * (11 * 1024 * 1024)
    r = await real_client.post("/company-copies/bootstrap/read", files={"file": ("a.celerp-company", big)})
    assert r.status_code == 422 and "not a Celerp company copy" in r.json()["detail"], r.text


async def test_read_refuses_file_over_cap(real_engine, real_client, tmp_path, monkeypatch):
    """A copy file over the upload cap is refused and nothing is kept."""
    from celerp.config import settings
    from celerp.services import migration_scan_store
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    token = await _token(real_engine, user, await _company(real_engine, user, "Alpha Trading", "alpha-marker"))
    data, _ = await _make_copy(real_client, token)
    monkeypatch.setattr(migration_scan_store, "MAX_AGGREGATE_BYTES", len(data) - 1)
    r = await real_client.post("/company-copies/read", files={"file": ("a.celerp-company", data)}, headers=auth(token))
    assert r.status_code == 413 and "larger" in r.json()["detail"], r.text
    assert not any((tmp_path / "company_copies" / "uploads").iterdir())


async def test_purge_tolerates_vanished_file(tmp_path, monkeypatch):
    """A file removed by another request while old uploads are cleared is skipped."""
    import os
    from pathlib import Path

    from celerp.routers.company_copy import _purge
    old = tmp_path / "old.celerp-company"
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    real_is_file = Path.is_file

    def vanishing(self):
        found = real_is_file(self)
        self.unlink(missing_ok=True)
        return found
    monkeypatch.setattr(Path, "is_file", vanishing)
    _purge(tmp_path)
    assert not old.exists()


async def test_open_rejects_unauthorized(real_engine, real_client, tmp_path, monkeypatch):
    """Only an owner copies or opens; a copy downloads only from its own company."""
    from celerp.config import settings
    from celerp.models.accounting import UserCompany
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    a = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    b = await _company(real_engine, user, "Beta Trading", "beta-marker")
    token = await _token(real_engine, user, a)
    data, made = await _make_copy(real_client, token)
    r = await real_client.get(f"/company-copies/{made['copy_id']}/download", headers=auth(await _token(real_engine, user, b)))
    assert r.status_code == 404

    clerk = await _owner(real_engine, "clerk@example.com")
    async with maker(real_engine)() as s:
        s.add(UserCompany(id=uuid.uuid4(), user_id=clerk, company_id=a, role="viewer"))
        await s.commit()
    viewer = await _token(real_engine, clerk, a, "viewer")
    assert (await real_client.post("/company-copies", json={}, headers=auth(viewer))).status_code == 403
    r = await real_client.post("/company-copies/read", files={"file": ("a.celerp-company", data)}, headers=auth(viewer))
    assert r.status_code == 403
    assert (await real_client.post("/company-copies", json={})).status_code == 401


# ── Fresh installation ───────────────────────────────────────────────────────

async def test_bootstrap_open_copy(real_engine, real_client, tmp_path, monkeypatch, code_config):
    """A fresh installation opens a copy without the source: the setup code gates it, the
    first owner is created and signed in, and the route closes once a user exists."""
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    token = await _token(real_engine, user, await _company(real_engine, user, "Alpha Trading", "alpha-marker"))
    data, _ = await _make_copy(real_client, token)
    async with real_engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))

    files = {"file": ("a.celerp-company", data)}
    r = await real_client.post("/company-copies/bootstrap/read", files=files, headers={"X-Setup-Code": "wrong"})
    assert r.status_code == 403
    r = await real_client.post("/company-copies/bootstrap/read", files=files, headers={"X-Setup-Code": code_config})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["company_name"] == "Alpha Trading" and preview["prepared_by"] == "Example Accounting"
    assert await count(real_engine, "companies") == 0

    body = {"upload_token": preview["upload_token"], "name": "New Owner", "email": "new@example.com",
            "password": "newownerpw1"}
    r = await real_client.post("/company-copies/bootstrap/open", json=body, headers={"X-Setup-Code": code_config})
    assert r.status_code == 201, r.text
    opened = r.json()
    assert opened["access_token"] and opened["company_name"] == "Alpha Trading"
    assert await count(real_engine, "users") == 1
    assert await count(real_engine, "projections", "company_id = :c", c=uuid.UUID(opened["company_id"])) == 1
    r = await real_client.post("/company-copies/bootstrap/read", files=files, headers={"X-Setup-Code": code_config})
    assert r.status_code == 409


# ── Activation report ────────────────────────────────────────────────────────

async def test_activation_handoff_id(real_engine, real_client, tmp_path, monkeypatch):
    """The activation report carries the hand-off id only once a copy has been opened here."""
    from celerp.config import settings
    from celerp.gateway.state import activate_payload
    from celerp.services.company_copy import latest_handoff_id
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    user = await _owner(real_engine)
    token = await _token(real_engine, user, await _company(real_engine, user, "Alpha Trading", "alpha-marker"))
    data, made = await _make_copy(real_client, token)
    async with maker(real_engine)() as s:
        assert await latest_handoff_id(s) is None
    assert "handoff_id" not in activate_payload("iid", handoff_id=None)

    assert (await _open(real_client, token, data)).status_code == 201
    async with maker(real_engine)() as s:
        handoff = await latest_handoff_id(s)
    assert handoff == made["handoff_id"]
    assert activate_payload("iid", handoff_id=handoff)["handoff_id"] == handoff

    # The startup check-in reports it.
    from contextlib import asynccontextmanager

    import celerp.config
    import celerp.db
    import celerp.gateway.state as state
    from celerp.main import _try_auto_activate

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
    monkeypatch.setattr(settings, "activation_verifier", "")
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    await _try_auto_activate()
    assert sent == [("https://relay.test/auth/checkin", sent[0][1])]
    assert sent[0][1]["handoff_id"] == handoff

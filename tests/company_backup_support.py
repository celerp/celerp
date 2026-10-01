# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared helpers for the company backup tests: owners, companies, tokens, backup files
and a whole-database snapshot for proving an operation changed nothing."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile

from sqlalchemy import text

from migration_support import OWNER_EMAIL, auth, maker
from test_helpers import make_authed_token


async def owner(engine, email: str = OWNER_EMAIL, name: str = "Owner"):
    """The installation owner for the default email, otherwise an ordinary user."""
    from celerp.models.company import User
    from celerp.services.auth import hash_password
    from celerp.services.provisioning import create_install_owner
    async with maker(engine)() as s:
        if email == OWNER_EMAIL:
            user = await create_install_owner(s, name=name, email=email, password="ownerpw123")
        else:
            user = User(email=email, name=name, auth_hash=hash_password("userpw1234"))
            s.add(user)
        await s.commit()
        return user.id


async def company(engine, user_id, name: str, marker: str, *, settings: dict | None = None):
    """A company owned by ``user_id`` with one location, one ledger event and one projection
    carrying ``marker``."""
    from celerp.models.company import User
    from celerp.services.provisioning import provision_migration_company
    async with maker(engine)() as s:
        user = await s.get(User, user_id)
        made = await provision_migration_company(s, owner=user, company_name=name)
        made.is_active, made.is_migration_staged = True, False
        cid, loc = made.id, uuid.uuid4()
        await s.execute(text("UPDATE companies SET settings = CAST(:s AS json) WHERE id = :c"),
                        {"s": json.dumps(settings or {}), "c": cid})
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


async def member(engine, user_id, company_id, role: str = "viewer", *, active: bool = True):
    from celerp.models.accounting import UserCompany
    async with maker(engine)() as s:
        s.add(UserCompany(id=uuid.uuid4(), user_id=user_id, company_id=company_id, role=role, is_active=active))
        await s.commit()


async def token(engine, user_id, company_id, role: str = "owner") -> str:
    async with maker(engine)() as s:
        return await make_authed_token(s, str(user_id), str(company_id), role)


async def download(client, tok: str, **params) -> bytes:
    """Download the current company's backup through the API."""
    r = await client.get("/company-backups/download", params=params or None, headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.content


async def read(client, tok: str, data: bytes, name: str = "books.celerp-company", mode: str = "settings"):
    """Upload a backup for the preview of restoring it in ``mode``."""
    return await client.post("/company-backups/read", files={"file": (name, data)}, data={"mode": mode},
                             headers=auth(tok))


def confirm(preview, mode: str = "settings") -> dict:
    """The restore request confirming a preview response as shown."""
    body = preview.json()
    return {"upload_token": body["upload_token"], "mode": mode, "plan_fingerprint": body.get("plan_fingerprint")}


async def restore(client, tok: str, data: bytes, mode: str = "settings"):
    """Upload and restore a backup as previewed; returns the restore response (or the refused read response)."""
    r = await read(client, tok, data, mode=mode)
    if r.status_code != 200:
        return r
    return await client.post("/company-backups/restore", json=confirm(r, mode), headers=auth(tok))


def members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def rezip(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in parts.items():
            zf.writestr(name, body)
    return buf.getvalue()


def manifest(data: bytes) -> dict:
    return json.loads(members(data)["manifest.json"])


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def snapshot(engine) -> dict:
    """Every row of every table."""
    async with engine.connect() as conn:
        tables = (await conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_type = 'BASE TABLE' ORDER BY table_name"))).scalars().all()
        return {t: sorted((await conn.execute(text(f'SELECT to_jsonb(x)::text FROM "{t}" x'))).scalars().all())
                for t in tables}


def unchanged_except(before: dict, after: dict, company_id: str) -> None:
    """Every row that existed before is still there, unchanged; new rows belong to ``company_id``."""
    for table, rows in before.items():
        assert set(rows) <= set(after[table]), table
        added = set(after[table]) - set(rows)
        assert all(str(company_id) in row for row in added), table


class FakeCloud:
    """An in-memory cloud attachment backend with the StorageBackend protocol."""

    base = "https://cloud.test/bucket/attachments"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.fail_store_after: int | None = None
        self.stored = 0

    def url(self, company_id, name: str) -> str:
        return f"{self.base}/{company_id}/{name}"

    async def store(self, company_id: str, att_id: str, content: bytes, mime: str) -> str:
        from celerp.services.attachments import _stored_extension
        if self.fail_store_after is not None and self.stored >= self.fail_store_after:
            raise OSError("cloud storage unavailable")
        self.stored += 1
        url = self.url(company_id, f"{att_id}{_stored_extension(mime)}")
        self.files[url] = content
        return url

    async def read(self, company_id: str, url: str, max_bytes: int) -> bytes | None:
        if not url.startswith(f"{self.base}/{company_id}/"):
            return None
        body = self.files.get(url)
        return body if body is not None and len(body) <= max_bytes else None

    async def read_stored(self, company_id: str, stored_id: str, mime: str, max_bytes: int) -> bytes | None:
        from celerp.services.attachments import _stored_extension
        return await self.read(company_id, self.url(company_id, stored_id + _stored_extension(mime)), max_bytes)

    async def delete_company(self, company_id: str) -> None:
        for url in [u for u in self.files if u.startswith(f"{self.base}/{company_id}/")]:
            del self.files[url]

    def company_files(self, company_id) -> dict[str, bytes]:
        return {u: b for u, b in self.files.items() if u.startswith(f"{self.base}/{company_id}/")}

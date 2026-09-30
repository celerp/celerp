# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company backups.

A company backup is one company's records, settings and attachment files in a single
``.celerp-company`` file, opened elsewhere as a new, independent company. It never
carries users, sign-in data, connector credentials, share links or anything else of
the installation it came from.

The file is a zip holding ``manifest.json``, one ``tables/<table>.jsonl`` per copied
table (one row per line, as Postgres renders it with ``to_jsonb``) and
``attachments/<file>``. Every table and file carries its sha256 in the manifest.

Opening a copy creates a new company with fresh ids for the company and every copied
row, inserts the rows, then reads them back, maps the ids back and checks every table
against the manifest before committing. Any difference rolls everything back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company, User
from celerp.services.attachments import LocalBackend, is_plain_name, company_attachment_dir, get_backend
from celerp.services.migrations import COMPANY_NAME_MAX, company_tables
from celerp.services.provisioning import provision_restored_company

logger = logging.getLogger(__name__)

FORMAT = "celerp-company-backup"
FORMAT_VERSION = 1
EXTENSION = ".celerp-company"

# The company's own records, in insert order (a table follows every table it references).
COPY_TABLES = (
    "locations", "work_centers", "accounts", "bank_accounts", "reconciliation_sessions",
    "reconciliation_rules", "bank_statement_lines", "label_templates", "ledger", "projections",
)

# Company tables that stay with the installation, and why.
EXCLUDED_TABLES = {
    "user_companies": "people and their roles belong to the installation",
    "connector_configs": "connector credentials",
    "connector_sources": "connector links to outside services",
    "marketplace_configs": "marketplace connection settings and credentials",
    "sync_runs": "connector sync history",
    "outbound_queue": "messages waiting to leave this installation",
    "doc_share_tokens": "share links issued by this installation",
    "notifications": "per-user notices",
    "ai_conversations": "per-user assistant history",
    "ai_batch_jobs": "assistant job state",
    "import_batches": "import job state",
    "migration_runs": "migration run state",
    "migration_cleanup_tasks": "migration cleanup state",
}

# Settings that describe this installation or its people, not the business.
DROPPED_SETTINGS = frozenset({
    "role_grants", "ai_memory", "lock_date_set_by", "reorder_alert_email", "column_prefs",
    "pay_tip_shown", "reorder_last_scan_at", "restored_backup",
})

# Columns that point at a user of the source installation; a copy carries none.
USER_COLUMNS = {"ledger": ("actor_id",)}

# The ledger's integer ids are reassigned in order on open, so they are not copied.
DROPPED_COLUMNS = {"ledger": ("id",)}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_BATCH = 1000
_MAX_MEMBER = 2 * 1024 ** 3

FULL_BACKUP = "This is a full Celerp backup. Use Restore a Celerp backup instead."
NOT_A_BACKUP = "This file is not a Celerp company backup."
DAMAGED = "This company backup is damaged or was changed after it was made. Ask for a new copy."
NEWER = "This company backup was made by a newer version of Celerp. Update Celerp, then open it again."
TOO_LARGE = "This file is larger than a company backup upload allows."


class BackupError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class BackupFile:
    """A copy file whose manifest and contents have been checked."""
    path: Path
    manifest: dict

    @property
    def company_name(self) -> str:
        return self.manifest["company"]["name"]

    def summary(self) -> dict:
        m = self.manifest
        return {
            "company_name": self.company_name,
            "created_at": m["created_at"],
            "records": sum(t["rows"] for t in m["tables"].values()),
            "attachments": len(m["attachments"]),
        }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _table_hash(lines: list[str]) -> str:
    return _sha("\n".join(sorted(lines)).encode())


async def _columns(session: AsyncSession, table: str) -> list[str]:
    return list((await session.scalars(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :t ORDER BY ordinal_position"), {"t": table})).all())


async def _rows(session: AsyncSession, table: str, company_id: uuid.UUID) -> list[str]:
    """The company's rows of one table, as canonical JSON text, the ledger in id order."""
    expr = "to_jsonb(t)"
    for col in DROPPED_COLUMNS.get(table, ()):
        expr = f"({expr} - '{col}')"
    for col in USER_COLUMNS.get(table, ()):
        expr = f"({expr} || jsonb_build_object('{col}', NULL))"
    order = " ORDER BY t.id" if table == "ledger" else ""
    return list((await session.scalars(text(
        f'SELECT ({expr})::text FROM "{table}" t WHERE t.company_id = :c{order}'), {"c": company_id})).all())


def _copied_settings(settings: dict | None) -> dict:
    return {k: v for k, v in (settings or {}).items() if k not in DROPPED_SETTINGS}


# ── Export ───────────────────────────────────────────────────────────────────

async def export_company(session: AsyncSession, company_id: uuid.UUID, dest: Path, *,
                         provenance: dict | None = None) -> dict:
    """Write a copy of one company to ``dest``; returns its manifest.

    Refused, with nothing written, when the company holds data in a table this copy
    does not know, or attachment files kept in cloud storage."""
    from celerp import __version__

    company = await session.get(Company, company_id)
    if company is None:
        raise BackupError(404, "Company not found.")
    for table in await company_tables(session):
        if table in COPY_TABLES or table in EXCLUDED_TABLES:
            continue
        if await session.scalar(text(f'SELECT 1 FROM "{table}" WHERE company_id = :c LIMIT 1'), {"c": company_id}):
            raise BackupError(409, f"This company has data in {table} that a company backup cannot carry yet. "
                                 "Nothing was copied.")
    if not isinstance(get_backend(), LocalBackend) and await session.scalar(text(
            "SELECT 1 FROM projections WHERE company_id = :c AND state::text LIKE '%attachments/%' LIMIT 1"),
            {"c": company_id}):
        raise BackupError(409, "This company has attachments kept in cloud storage, which a company backup "
                             "cannot carry yet. Nothing was copied.")

    manifest: dict = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "scope": "company",
        "app_version": __version__, "created_at": datetime.now(timezone.utc).isoformat(),
        "backup_id": str(uuid.uuid4()), **({"provenance": provenance} if provenance else {}),
        "company": {"id": str(company.id), "name": company.name, "settings": _copied_settings(company.settings)},
        "tables": {}, "attachments": {},
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".partial")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for table in COPY_TABLES:
                lines = await _rows(session, table, company_id)
                dropped = set(DROPPED_COLUMNS.get(table, ()))
                manifest["tables"][table] = {
                    "rows": len(lines), "sha256": _table_hash(lines),
                    "columns": [c for c in await _columns(session, table) if c not in dropped],
                }
                zf.writestr(f"tables/{table}.jsonl", "\n".join(lines))
            folder = company_attachment_dir(str(company_id))
            if folder.is_dir():
                for f in sorted(folder.iterdir()):
                    if f.is_file() and is_plain_name(f.name):
                        data = f.read_bytes()
                        manifest["attachments"][f.name] = _sha(data)
                        zf.writestr(f"attachments/{f.name}", data)
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        partial.replace(dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return manifest


# ── Reading a copy file ──────────────────────────────────────────────────────

def _looks_like_backup(path: Path) -> bool:
    """A full backup is a gzip-compressed tar archive."""
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def read_backup(path: Path) -> BackupFile:
    """Check a copy file: its format, and every table and attachment against its hash."""
    if not zipfile.is_zipfile(path):
        raise BackupError(422, FULL_BACKUP if _looks_like_backup(path) else NOT_A_BACKUP)
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except (KeyError, ValueError):
                raise BackupError(422, NOT_A_BACKUP) from None
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("scope") != "company":
                raise BackupError(422, NOT_A_BACKUP)
            if not isinstance(manifest.get("format_version"), int) or manifest["format_version"] > FORMAT_VERSION:
                raise BackupError(422, NEWER)
            tables = manifest.get("tables")
            if not isinstance(tables, dict) or not isinstance(manifest.get("attachments"), dict):
                raise BackupError(422, DAMAGED)
            if set(tables) - set(COPY_TABLES):
                raise BackupError(422, NEWER)
            _check_manifest(manifest)
            names = set(zf.namelist())
            for table, meta in tables.items():
                lines = _member_lines(zf, f"tables/{table}.jsonl", names)
                if len(lines) != meta.get("rows") or _table_hash(lines) != meta.get("sha256"):
                    raise BackupError(422, DAMAGED)
            for name, digest in manifest["attachments"].items():
                if not is_plain_name(name) or f"attachments/{name}" not in names:
                    raise BackupError(422, DAMAGED)
                if _sha(_member(zf, f"attachments/{name}")) != digest:
                    raise BackupError(422, DAMAGED)
    except (zipfile.BadZipFile, OSError, KeyError, TypeError, AttributeError, ValueError):
        raise BackupError(422, DAMAGED) from None
    return BackupFile(path=path, manifest=manifest)


def _check_manifest(manifest: dict) -> None:
    """The manifest fields a copy is opened from, each of the type the export writes."""
    company = manifest.get("company")
    if not (isinstance(manifest.get("created_at"), str)
            and isinstance(company, dict) and isinstance(company.get("name"), str)
            and company["name"].strip() and len(company["name"]) <= COMPANY_NAME_MAX
            and isinstance(company.get("id"), str) and _UUID.fullmatch(company["id"])
            and isinstance(company.get("settings"), dict)
            and all(isinstance(meta, dict) and isinstance(meta.get("columns"), list)
                    and all(isinstance(c, str) for c in meta["columns"]) for meta in manifest["tables"].values())
            and not _has_nul(manifest)):
        raise BackupError(422, DAMAGED)


def _has_nul(value) -> bool:
    """Whether any text in a manifest value holds a NUL character, which the database cannot store."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


def _member(zf: zipfile.ZipFile, name: str) -> bytes:
    if zf.getinfo(name).file_size > _MAX_MEMBER:
        raise BackupError(422, DAMAGED)
    return zf.read(name)


def _member_lines(zf: zipfile.ZipFile, name: str, names: set[str]) -> list[str]:
    if name not in names:
        raise BackupError(422, DAMAGED)
    body = _member(zf, name).decode()
    return body.split("\n") if body else []


async def check_schema(session: AsyncSession, copy: BackupFile) -> None:
    """Refuse a copy holding a column this installation does not have (a newer Celerp)."""
    for table, meta in copy.manifest["tables"].items():
        if not set(meta.get("columns") or []) <= set(await _columns(session, table)):
            raise BackupError(422, NEWER)


# ── Opening a copy ───────────────────────────────────────────────────────────

# Columns every reader expects to hold a JSON object.
_OBJECT_COLUMNS = {"ledger": "data", "projections": "state"}


def _objects(lines: list[str]) -> list[dict]:
    """The rows of one table; a row that is not a JSON object means the file is damaged."""
    try:
        rows = [json.loads(line) for line in lines]
    except ValueError:
        raise BackupError(422, DAMAGED) from None
    if not all(isinstance(row, dict) for row in rows):
        raise BackupError(422, DAMAGED)
    return rows


def _row_ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if isinstance(row.get("id"), str) and _UUID.fullmatch(row["id"])}


def _id_map(copy: BackupFile, tables: dict[str, list[dict]]) -> dict[str, str]:
    """Fresh ids for the source company and every copied row keyed by a uuid."""
    ids = {copy.manifest["company"]["id"]}
    for rows in tables.values():
        ids |= _row_ids(rows)
    return {old: str(uuid.uuid4()) for old in ids}


async def _foreign_keys(session: AsyncSession, table: str) -> list[tuple[str, str]]:
    """(column, referenced table) for every foreign key of one table, from the database itself."""
    return [tuple(r) for r in (await session.execute(text(
        "SELECT kcu.column_name, ccu.table_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
        "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = current_schema() AND tc.table_name = :t"),
        {"t": table})).all()]


async def _check_references(session: AsyncSession, copy: BackupFile, tables: dict[str, list[dict]]) -> None:
    """Refuse a copy whose rows point outside it: every foreign key is empty or names the
    copy's own company or a row the copy itself carries in the referenced table. The ids
    are checked before they are remapped; the remap maps exactly these ids, so the check
    holds for the rows as inserted."""
    owned = {table: _row_ids(rows) for table, rows in tables.items()}
    owned["companies"] = {copy.manifest["company"]["id"]}
    for table, rows in tables.items():
        for column, target in await _foreign_keys(session, table):
            allowed = owned.get(target, set())
            for row in rows:
                value = row.get(column)
                if value is not None and not (isinstance(value, str) and value in allowed):
                    raise BackupError(422, DAMAGED)


def _remap(line: str, mapping: dict[str, str]) -> str:
    return _UUID.sub(lambda m: mapping.get(m.group(0), m.group(0)), line)


async def _insert(session: AsyncSession, table: str, columns: list[str], lines: list[str]) -> None:
    cols = ", ".join(f'"{c}"' for c in columns)
    picked = ", ".join(f'r."{c}"' for c in columns)
    for start in range(0, len(lines), _BATCH):
        batch = "[" + ",".join(lines[start:start + _BATCH]) + "]"
        await session.execute(text(
            f'INSERT INTO "{table}" ({cols}) SELECT {picked} '
            f"FROM jsonb_array_elements(CAST(:rows AS jsonb)) WITH ORDINALITY AS e(v, n), "
            f'jsonb_populate_record(NULL::"{table}", e.v) AS r ORDER BY e.n'), {"rows": batch})


async def restore_backup(session: AsyncSession, copy: BackupFile, *, owner: User) -> Company:
    """Create a new company from a checked copy, owned by ``owner``, and commit it.

    The new company's rows are read back and checked against the copy before the
    commit; on any failure nothing is kept, including attachment files."""
    manifest = copy.manifest
    await check_schema(session, copy)
    with zipfile.ZipFile(copy.path) as zf:
        names = set(zf.namelist())
        source = {t: _member_lines(zf, f"tables/{t}.jsonl", names) for t in manifest["tables"]}
        files = {name: _member(zf, f"attachments/{name}") for name in manifest["attachments"]}
    rows = {table: _objects(lines) for table, lines in source.items()}
    if not all(isinstance(row.get(column), dict) for table, column in _OBJECT_COLUMNS.items()
               for row in rows.get(table, [])):
        raise BackupError(422, DAMAGED)
    await _check_references(session, copy, rows)
    mapping = _id_map(copy, rows)
    back = {new: old for old, new in mapping.items()}
    settings = {
        **_copied_settings(manifest["company"].get("settings")),
        "restored_backup": {"backup_id": manifest.get("backup_id"), "created_at": manifest["created_at"],
                            "source_company_name": manifest["company"]["name"],
                            "restored_at": datetime.now(timezone.utc).isoformat()},
    }
    folder: Path | None = None
    try:
        company = await provision_restored_company(session, owner=owner, company_name=manifest["company"]["name"],
                                                 company_id=uuid.UUID(mapping[manifest["company"]["id"]]),
                                                 settings=settings)
        try:
            for table in COPY_TABLES:
                if source.get(table):
                    await _insert(session, table, manifest["tables"][table]["columns"],
                                  [_remap(line, mapping) for line in source[table]])
        except DBAPIError:
            raise BackupError(422, DAMAGED) from None
        for table, meta in manifest["tables"].items():
            lines = [_remap(line, back) for line in await _rows(session, table, company.id)]
            if len(lines) != meta["rows"] or _table_hash(lines) != meta["sha256"]:
                raise BackupError(422, f"The opened company did not match the copy ({table}). Nothing was kept.")
        if files:
            folder = company_attachment_dir(str(company.id))
            folder.mkdir(parents=True, exist_ok=True)
            for name, data in files.items():
                (folder / name).write_bytes(data)
        await session.commit()
    except BaseException:
        await session.rollback()
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    return company


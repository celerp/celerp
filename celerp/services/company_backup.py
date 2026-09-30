# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company backups.

A company backup is one company's records, settings and attachment files in a single
``.celerp-company`` file, restored elsewhere as a new, independent company. It never
carries users, sign-in data, connector credentials, share links or anything else of
the installation it came from.

The file is a zip holding ``manifest.json``, one ``tables/<table>.jsonl`` per backed-up
table (one row per line, as Postgres renders it with ``to_jsonb``) and
``attachments/<name>``. Every member carries its sha256 in the manifest.

Which tables are backed up is read from the database itself: every table with a
``company_id`` column, and every table named with an installed module's table prefix,
is either backed up or listed in ``EXCLUDED_TABLES``; anything else stops the export.
Tables are written and restored in foreign-key order, a batch at a time.

Restoring creates a new company with fresh ids for the company and every backed-up
row, remaps every value that exactly equals an old id or attachment URL, inserts the
rows, reads them back and checks every table against the backup before committing.
Any failure rolls everything back, including attachment files already stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from packaging.version import InvalidVersion, Version
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

import celerp.db
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.modules.importer import installed_table_prefixes
from celerp.modules.loader import module_search_path, read_manifest, resolve_module_path
from celerp.modules.registry import get_enabled, set_enabled
from celerp.services import attachments, bootstrap
from celerp.services.auth import verify_password
from celerp.services.migrations import COMPANY_NAME_MAX
from celerp.services.provisioning import create_install_owner, provision_restored_company

logger = logging.getLogger(__name__)

FORMAT = "celerp-company-backup"
FORMAT_VERSION = 1
EXTENSION = ".celerp-company"

# The core and bundled-module company tables a backup carries.
PORTABLE_TABLES = frozenset({
    "locations", "work_centers", "ledger", "projections", "accounts", "bank_accounts",
    "reconciliation_sessions", "reconciliation_rules", "bank_statement_lines", "label_templates",
})

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

# Columns every reader expects to hold a JSON object.
OBJECT_COLUMNS = {"ledger": "data", "projections": "state"}

MODES = frozenset({"settings", "new_company", "bootstrap"})

MAX_UPLOAD_BYTES = 2 * 1024 ** 3
MAX_MEMBERS = 200_000
MAX_MEMBER_BYTES = 1024 ** 3
MAX_TOTAL_BYTES = 8 * 1024 ** 3
BATCH_ROWS = 1000

_NOT_RESTORED = " Nothing was restored."
_NOT_BACKED_UP = " Nothing was backed up."
NOT_A_BACKUP = "This file is not a Celerp company backup." + _NOT_RESTORED
SYSTEM_BACKUP = "This is a whole-installation backup. Use System Recovery instead." + _NOT_RESTORED
DAMAGED = "This company backup is damaged or was changed after it was made." + _NOT_RESTORED
NEWER = ("This company backup was made by a newer version of Celerp. Update Celerp, then try again."
         + _NOT_RESTORED)
TOO_LARGE = "This company backup is too large to restore here." + _NOT_RESTORED
TOO_LARGE_UPLOAD = "This file is too large for a company backup upload." + _NOT_RESTORED
UNSAVABLE = "This company backup has records this Celerp cannot save." + _NOT_RESTORED
ATTACHMENT_FAILED = "Celerp could not save an attachment file from this backup." + _NOT_RESTORED
MISMATCH = "The restored company did not match the backup ({table})." + _NOT_RESTORED
ALREADY_SET_UP = "This Celerp is already set up." + _NOT_RESTORED
NOT_A_MEMBER = ("This backup was already restored here as a company you are not a member of."
                + _NOT_RESTORED)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_RAW_NUL = re.compile(rb"(?<!\\)(?:\\\\)*\\u0000")
_NUMBER = re.compile(r'"\\u0000([^"\\]*)\\u0000"')
_KEY_TYPES = frozenset({"uuid", "text", "varchar"})
_CHUNK = 1024 * 1024


class BackupError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ── Schema ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Column:
    udt: str
    notnull: bool
    generated: bool


@dataclass
class _Table:
    name: str
    columns: dict[str, _Column] = field(default_factory=dict)
    pk: tuple[str, ...] = ()
    fks: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = field(default_factory=list)

    @property
    def insertable(self) -> list[str]:
        return [c for c, col in self.columns.items() if not col.generated]


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def _schema(session: AsyncSession) -> dict[str, _Table]:
    tables: dict[str, _Table] = {}
    for rel, att, udt, notnull, generated in (await session.execute(text(
            "SELECT c.relname::text, a.attname::text, t.typname::text, a.attnotnull, "
            "(a.attidentity <> '' OR a.attgenerated <> '' "
            " OR COALESCE(pg_get_expr(d.adbin, d.adrelid), '') LIKE 'nextval(%') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "JOIN pg_type t ON t.oid = a.atttypid "
            "LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
            "ORDER BY c.relname, a.attnum"))).all():
        tables.setdefault(rel, _Table(rel)).columns[att] = _Column(udt, notnull, generated)
    for rel, kind, cols, target, tcols in (await session.execute(text(
            "SELECT c.relname::text, k.contype::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY u(n, i) "
            "      JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n ORDER BY u.i), "
            "f.relname::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY u(n, i) "
            "      JOIN pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.n ORDER BY u.i) "
            "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_class f ON f.oid = k.confrelid "
            "WHERE n.nspname = current_schema() AND k.contype IN ('p', 'f') "
            "ORDER BY c.relname, k.conname"))).all():
        table = tables.get(rel)
        if table is None:
            continue
        if kind == "p":
            table.pk = tuple(cols)
        else:
            table.fks.append((tuple(cols), target, tuple(tcols)))
    return tables


@dataclass
class _Plan:
    order: list[str]
    schema: dict[str, _Table]
    owners: dict[str, str]

    def outside_fks(self, table: str) -> list[tuple[str, ...]]:
        """Foreign keys of ``table`` pointing at a table the backup does not carry."""
        carried = set(self.order)
        return [cols for cols, target, _ in self.schema[table].fks
                if target != "companies" and target not in carried]


def _owner(table: str, prefixes: dict[str, str]) -> str | None:
    """The installed module whose table prefix names ``table``; the longest prefix wins."""
    hits = [(len(prefix), module) for module, prefix in prefixes.items() if table.startswith(prefix)]
    return max(hits)[1] if hits else None


def _module_shape_ok(table: _Table) -> bool:
    if "company_id" not in table.columns:
        return False
    if len(table.pk) == 1:
        col = table.columns[table.pk[0]]
        return col.udt in _KEY_TYPES and not col.generated
    if len(table.pk) == 2 and table.pk[0] == "company_id":
        return table.columns[table.pk[1]].udt in ("text", "varchar")
    return False


def _refusal(table: str, owners: dict[str, str]) -> BackupError:
    if table in owners:
        return BackupError(409, f"The {owners[table]} module keeps data in {table} in a form Celerp "
                                f"cannot back up yet." + _NOT_BACKED_UP)
    return BackupError(409, f"This company has data Celerp cannot back up yet: {table}." + _NOT_BACKED_UP)


async def _classify(session: AsyncSession, *, strict: bool) -> _Plan:
    """The tables a backup carries, parents first. Strict (export) refuses any company
    table it cannot carry; otherwise (restore) such tables are simply not carried."""
    schema = await _schema(session)
    prefixes = installed_table_prefixes("")
    owners: dict[str, str] = {}
    carried: list[str] = []
    for name in sorted(schema):
        table = schema[name]
        owner = _owner(name, prefixes)
        if name in EXCLUDED_TABLES or (owner is None and "company_id" not in table.columns):
            continue
        if owner is not None and name not in PORTABLE_TABLES:
            owners[name] = owner
            ok = _module_shape_ok(table)
        else:
            ok = name in PORTABLE_TABLES and bool(table.pk)
        if ok:
            carried.append(name)
        elif strict:
            raise _refusal(name, owners)
    changed = True
    while changed:
        changed = False
        keep = set(carried)
        for name in list(carried):
            if any(schema[name].columns[c].notnull for cols, target, _ in schema[name].fks
                   if target != "companies" and target not in keep for c in cols):
                if strict:
                    raise _refusal(name, owners)
                carried.remove(name)
                changed = True
    return _Plan(order=_fk_order(carried, schema), schema=schema, owners=owners)


def _fk_order(tables: list[str], schema: dict[str, _Table]) -> list[str]:
    """Tables ordered so each follows every other listed table it references."""
    listed = set(tables)
    parents = {t: {target for _, target, _ in schema[t].fks if target in listed and target != t} for t in tables}
    order: list[str] = []
    while parents:
        ready = sorted(t for t, p in parents.items() if not p)
        if not ready:
            order.extend(sorted(parents))  # a reference cycle: its rows are checked, not ordered
            break
        order.extend(ready)
        for t in ready:
            del parents[t]
        for p in parents.values():
            p.difference_update(ready)
    return order


async def classify(session: AsyncSession) -> list[str]:
    """The tables a backup of this database carries, in foreign-key insertion order."""
    return (await _classify(session, strict=True)).order


# ── Values ───────────────────────────────────────────────────────────────────

def remap(value, id_map: dict[str, str]):
    """A copy of a parsed JSON value with every string that exactly equals a key of
    ``id_map`` replaced by its value. Dict keys and substrings are never replaced."""
    if isinstance(value, str):
        return id_map.get(value, value)
    if isinstance(value, dict):
        return {k: remap(v, id_map) for k, v in value.items()}
    if isinstance(value, list):
        return [remap(v, id_map) for v in value]
    return value


def _refuse_constant(name: str):
    raise ValueError(name)


def _parse_row(line: bytes | str):
    """One row, with every non-integer number kept as its exact text so it is written
    back digit for digit."""
    return json.loads(line, parse_float=lambda s: "\x00" + s + "\x00", parse_constant=_refuse_constant)


def _dump_rows(rows: list[dict]) -> str:
    return _NUMBER.sub(r"\1", json.dumps(rows))


def _row_digest(row: dict) -> int:
    return int(hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(), 16)


def _has_nul(value) -> bool:
    """Whether any text in a value holds a NUL character, which the database cannot store."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


def _kept_settings(settings: dict | None) -> dict:
    return {k: v for k, v in (settings or {}).items() if k not in DROPPED_SETTINGS}


async def _batches(session: AsyncSession, table: _Table, company_id, expr: str):
    """The company's rows of one table as JSON text, in primary-key order, BATCH_ROWS at a time."""
    q = _ident(table.name)
    udt = {c: table.columns[c].udt for c in table.pk}
    keys = ", ".join(f"t.{_ident(c)}::text" for c in table.pk)
    order = ", ".join(f"t.{_ident(c)}" for c in table.pk)
    company = f"CAST(CAST(:c AS text) AS {_ident(table.columns['company_id'].udt)})"
    after = (f" AND ({order}) > ("
             + ", ".join(f"CAST(CAST(:k{i} AS text) AS {_ident(udt[c])})" for i, c in enumerate(table.pk)) + ")")
    params: dict = {"c": str(company_id), "n": BATCH_ROWS}
    first = True
    while True:
        rows = (await session.execute(text(
            f"SELECT {keys}, ({expr})::text FROM {q} t WHERE t.company_id = {company}"
            f"{'' if first else after} ORDER BY {order} LIMIT :n"), params)).all()
        if rows:
            yield [r[-1] for r in rows]
        if len(rows) < params["n"]:
            return
        first = False
        params.update({f"k{i}": v for i, v in enumerate(rows[-1][:-1])})


def _without(columns: list[str]) -> str:
    expr = "to_jsonb(t)"
    if columns:
        expr = f"({expr} - ARRAY[{', '.join(_literal(c) for c in columns)}]::text[])"
    return expr


# ── Export ───────────────────────────────────────────────────────────────────

def _collect_urls(value, company_id, found: dict[str, str]) -> None:
    """Record every attachment URL stored for ``company_id`` in a value, by stored name."""
    if isinstance(value, str):
        name = attachments.company_file_name(company_id, value)
        if name is not None and found.setdefault(name, value) != value:
            raise BackupError(409, f"This company has two attachment files named {name}." + _NOT_BACKED_UP)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_urls(v, company_id, found)
    elif isinstance(value, list):
        for v in value:
            _collect_urls(v, company_id, found)


def _module_versions(names: set[str]) -> dict[str, str]:
    versions = {}
    for name in sorted(names):
        path = resolve_module_path(name, module_search_path())
        version = read_manifest(path).get("version") if path is not None else None
        if isinstance(version, str) and version:
            versions[name] = version
    return versions


async def export_company(session: AsyncSession, company_id, out: Path, *,
                         provenance: dict | None = None) -> dict:
    """Write a backup of one company to ``out``; returns its manifest.

    Refused, with nothing written, when the company holds data Celerp cannot back up."""
    company = await session.get(Company, company_id)
    if company is None:
        raise BackupError(404, "Company not found.")
    plan = await _classify(session, strict=True)
    await session.execute(text("SET LOCAL TimeZone = 'UTC'"))
    tables = []
    for name in plan.order:
        if name in plan.owners and not await session.scalar(text(
                f"SELECT 1 FROM {_ident(name)} WHERE company_id = "
                f"CAST(CAST(:c AS text) AS {_ident(plan.schema[name].columns['company_id'].udt)}) LIMIT 1"),
                {"c": str(company_id)}):
            continue
        tables.append(name)
    settings = _kept_settings(company.settings)
    found: dict[str, str] = {}
    _collect_urls(settings, company_id, found)
    enabled = get_enabled(settings)
    manifest: dict = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "backup_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "company": {"id": str(company.id), "name": company.name, "settings": settings},
        **({"provenance": provenance} if provenance else {}),
        "modules": {"enabled": sorted(enabled),
                    "versions": _module_versions(enabled | {plan.owners[t] for t in tables if t in plan.owners})},
        "tables": {}, "attachments": [],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in tables:
                table = plan.schema[name]
                expr = _without([c for c, col in table.columns.items() if col.generated])
                for cols in plan.outside_fks(name):
                    expr = f"({expr} || jsonb_build_object({', '.join(f'{_literal(c)}, NULL' for c in cols)}))"
                digest, rows = hashlib.sha256(), 0
                with zf.open(f"tables/{name}.jsonl", "w", force_zip64=True) as fh:
                    async for batch in _batches(session, table, company_id, expr):
                        for line in batch:
                            _collect_urls(json.loads(line), company_id, found)
                            body = line.encode() + b"\n"
                            digest.update(body)
                            fh.write(body)
                        rows += len(batch)
                manifest["tables"][name] = {"columns": table.insertable, "rows": rows, "sha256": digest.hexdigest()}
            for name in sorted(found):
                url = found[name]
                body = await attachments.read_company_file(company_id, url, MAX_MEMBER_BYTES)
                if body is None:
                    raise BackupError(409, f"This company has an attachment file Celerp cannot read: {url}."
                                      + _NOT_BACKED_UP)
                zf.writestr(f"attachments/{name}", body)
                manifest["attachments"].append({"url": url, "name": name, "size": len(body),
                                                "sha256": hashlib.sha256(body).hexdigest()})
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        partial.replace(out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return manifest


# ── Reading a backup file ────────────────────────────────────────────────────

@dataclass(frozen=True)
class BackupFile:
    """A backup file whose manifest and every member have been checked."""
    path: Path
    manifest: dict

    def summary(self) -> dict:
        m = self.manifest
        tables = {t: meta["rows"] for t, meta in m["tables"].items()}
        return {
            "backup_id": m["backup_id"], "company_name": m["company"]["name"], "created_at": m["created_at"],
            "records": sum(tables.values()), "tables": tables, "attachments": len(m["attachments"]),
            "modules": m["modules"]["enabled"], "provenance": m.get("provenance"),
        }


def _looks_like_system_backup(path: Path) -> bool:
    """A whole-installation backup is a gzip-compressed tar archive."""
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def _is_uuid(value) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_manifest(m) -> None:
    """Every manifest field a restore reads, of the type the export writes."""
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise BackupError(422, NOT_A_BACKUP)
    version = m.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BackupError(422, DAMAGED)
    if version > FORMAT_VERSION:
        raise BackupError(422, NEWER)
    company, modules, tables, files = m.get("company"), m.get("modules"), m.get("tables"), m.get("attachments")
    ok = (_is_uuid(m.get("backup_id")) and isinstance(m.get("created_at"), str)
          and isinstance(company, dict) and _is_uuid(company.get("id"))
          and isinstance(company.get("name"), str) and company["name"].strip()
          and len(company["name"]) <= COMPANY_NAME_MAX and isinstance(company.get("settings"), dict)
          and isinstance(m.get("provenance", {}), dict)
          and isinstance(modules, dict) and isinstance(modules.get("enabled"), list)
          and all(isinstance(n, str) for n in modules["enabled"])
          and isinstance(modules.get("versions"), dict)
          and all(isinstance(v, str) for v in modules["versions"].values())
          and isinstance(tables, dict) and isinstance(files, list) and not _has_nul(m))
    if ok:
        for name, meta in tables.items():
            ok = ok and (_TABLE_NAME.fullmatch(name) is not None and isinstance(meta, dict)
                         and isinstance(meta.get("columns"), list)
                         and all(isinstance(c, str) for c in meta["columns"])
                         and len(set(meta["columns"])) == len(meta["columns"])
                         and _is_count(meta.get("rows")) and isinstance(meta.get("sha256"), str))
        names = [f.get("name") if isinstance(f, dict) else None for f in files]
        ok = ok and len(set(names)) == len(names) and all(
            isinstance(f, dict) and isinstance(f.get("url"), str) and isinstance(f.get("name"), str)
            and attachments.is_plain_name(f["name"]) and _is_count(f.get("size"))
            and isinstance(f.get("sha256"), str) for f in files)
    if not ok:
        raise BackupError(422, DAMAGED)


class _Budget:
    """Uncompressed bytes read so far, against the per-member and total limits."""

    def __init__(self) -> None:
        self.total = 0

    def chunks(self, zf: zipfile.ZipFile, name: str):
        size = 0
        with zf.open(name) as fh:
            while chunk := fh.read(_CHUNK):
                size += len(chunk)
                self.total += len(chunk)
                if size > MAX_MEMBER_BYTES or self.total > MAX_TOTAL_BYTES:
                    raise BackupError(422, TOO_LARGE)
                yield chunk


def _check_member(budget: _Budget, zf: zipfile.ZipFile, name: str, sha256: str, *,
                  rows: int | None = None, size: int | None = None) -> None:
    digest, length, lines, pending = hashlib.sha256(), 0, 0, False
    for chunk in budget.chunks(zf, name):
        digest.update(chunk)
        length += len(chunk)
        if rows is not None:
            parts = chunk.split(b"\n")
            for i, part in enumerate(parts):
                pending = pending or bool(part.strip())
                if i < len(parts) - 1:
                    lines += pending
                    pending = False
    lines += pending
    if digest.hexdigest() != sha256 or (rows is not None and lines != rows) or (size is not None and length != size):
        raise BackupError(422, DAMAGED)


def read_backup(path: Path) -> BackupFile:
    """Check a backup file: its format, its size against the limits, and every member
    against its hash."""
    if not zipfile.is_zipfile(path):
        raise BackupError(422, SYSTEM_BACKUP if _looks_like_system_backup(path) else NOT_A_BACKUP)
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if (len(infos) > MAX_MEMBERS or any(i.file_size > MAX_MEMBER_BYTES for i in infos)
                    or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES):
                raise BackupError(422, TOO_LARGE)
            names = [i.filename for i in infos]
            if len(set(names)) != len(names):
                raise BackupError(422, DAMAGED)
            if "manifest.json" not in names:
                raise BackupError(422, NOT_A_BACKUP)
            budget = _Budget()
            try:
                manifest = json.loads(b"".join(budget.chunks(zf, "manifest.json")))
            except ValueError:
                raise BackupError(422, NOT_A_BACKUP) from None
            _check_manifest(manifest)
            expected = {"manifest.json", *(f"tables/{t}.jsonl" for t in manifest["tables"]),
                        *(f"attachments/{f['name']}" for f in manifest["attachments"])}
            if expected != set(names):
                raise BackupError(422, DAMAGED)
            for table, meta in manifest["tables"].items():
                _check_member(budget, zf, f"tables/{table}.jsonl", meta["sha256"], rows=meta["rows"])
            for f in manifest["attachments"]:
                _check_member(budget, zf, f"attachments/{f['name']}", f["sha256"], size=f["size"])
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, OSError, EOFError, ValueError, KeyError,
            RuntimeError, NotImplementedError):
        raise BackupError(422, DAMAGED) from None
    return BackupFile(path=path, manifest=manifest)


# ── Checking a backup against this installation ──────────────────────────────

@dataclass
class _Checked:
    plan: _Plan
    order: list[str]
    ids: set[str]
    digests: dict[str, int]


def _check_modules(manifest: dict) -> None:
    for name, needed in sorted(manifest["modules"]["versions"].items()):
        path = resolve_module_path(name, module_search_path())
        if path is None:
            raise BackupError(422, f"This company backup needs the {name} module, which is not installed here."
                              + _NOT_RESTORED)
        have = read_manifest(path).get("version")
        try:
            new_enough = Version(str(have)) >= Version(needed)
        except InvalidVersion:
            new_enough = have == needed
        if not new_enough:
            raise BackupError(422, f"This company backup needs the {name} module version {needed} or later."
                              + _NOT_RESTORED)


def _lines(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        for line in fh:
            if line.strip():
                yield line


def _scan_rows(backup: BackupFile, order: list[str], plan: _Plan) -> tuple[set[str], dict[str, int]]:
    """Check every row before anything is written: its shape, its keys, and that every
    reference points at the backup's own company, at a row the backup carries, or nowhere.
    Returns the ids to replace and each table's row digest."""
    m = backup.manifest
    source = m["company"]["id"]
    carried = set(order)
    ids: set[str] = set()
    digests: dict[str, int] = {}
    refs: dict[tuple[str, tuple[str, ...]], set[tuple]] = {}
    keys: dict[tuple[str, tuple[str, ...]], set[tuple]] = {}
    wanted = {(target, tcols) for t in order for cols, target, tcols in plan.schema[t].fks
              if target in carried and set(cols) <= set(m["tables"][t]["columns"])}
    with zipfile.ZipFile(backup.path) as zf:
        for name in order:
            table, columns = plan.schema[name], m["tables"][name]["columns"]
            present = set(columns)
            fks = [(cols, target, tcols) for cols, target, tcols in table.fks if set(cols) <= present]
            own_keys = [(tcols, keys.setdefault((name, tcols), set())) for target, tcols in wanted if target == name]
            if any(not set(tcols) <= present for tcols, _ in own_keys):
                raise BackupError(422, DAMAGED)
            single = table.pk[0] if len(table.pk) == 1 and not table.columns[table.pk[0]].generated else None
            total = 0
            for line in _lines(zf, f"tables/{name}.jsonl"):
                if _RAW_NUL.search(line):
                    raise BackupError(422, DAMAGED)
                row = _parse_row(line)
                if not isinstance(row, dict) or set(row) != present or row.get("company_id") != source:
                    raise BackupError(422, DAMAGED)
                if name in OBJECT_COLUMNS and not isinstance(row.get(OBJECT_COLUMNS[name]), dict):
                    raise BackupError(422, DAMAGED)
                if single is not None and single in present:
                    value = row[single]
                    if table.columns[single].udt == "uuid" and not _is_uuid(value):
                        raise BackupError(422, DAMAGED)
                    if _is_uuid(value):
                        ids.add(value)
                for tcols, bucket in own_keys:
                    bucket.add(tuple(row[c] for c in tcols))
                for cols, target, tcols in fks:
                    values = tuple(row[c] for c in cols)
                    if any(v is None for v in values):
                        continue
                    if target == "companies":
                        if values != (source,):
                            raise BackupError(422, DAMAGED)
                    elif target in carried:
                        refs.setdefault((target, tcols), set()).add(values)
                    else:
                        raise BackupError(422, DAMAGED)
                total += _row_digest(row)
            digests[name] = total % (1 << 256)
    for key, values in refs.items():
        if not values <= keys.get(key, set()):
            raise BackupError(422, DAMAGED)
    return ids, digests


async def check_backup(session: AsyncSession, backup: BackupFile) -> _Checked:
    """Refuse a backup this installation cannot restore exactly: a missing or older
    module, a table or column it does not have, or rows that do not hold together."""
    _check_modules(backup.manifest)
    plan = await _classify(session, strict=False)
    tables = backup.manifest["tables"]
    for name, meta in tables.items():
        if name not in plan.order or not set(meta["columns"]) <= set(plan.schema[name].insertable):
            raise BackupError(422, NEWER)
    order = [t for t in plan.order if t in tables]
    ids, digests = await asyncio.to_thread(_scan_rows, backup, order, plan)
    return _Checked(plan=plan, order=order, ids=ids, digests=digests)


# ── Restoring ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RestoreResult:
    company_id: str
    company_name: str
    created: bool
    backup_created_at: str
    user_id: str


def _lock_key(backup_id: str) -> int:
    return int.from_bytes(hashlib.sha256(backup_id.encode()).digest()[:8], "big", signed=True)


async def _lock(session: AsyncSession, backup_id: str, *, bootstrapping: bool) -> None:
    """Wait for any other restore of the same backup (and, when bootstrapping, any other
    first-owner setup) to finish."""
    await session.execute(text("SET LOCAL lock_timeout = 0"))
    await session.execute(text("SET LOCAL statement_timeout = 0"))
    if bootstrapping:
        await bootstrap.lock_bootstrap(session)
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(backup_id)})
    await session.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    await session.execute(text("SET LOCAL statement_timeout TO DEFAULT"))


async def _is_member(session: AsyncSession, user_id, company_id) -> bool:
    return await session.scalar(select(UserCompany.id).where(
        UserCompany.user_id == user_id, UserCompany.company_id == company_id,
        UserCompany.is_active.is_(True)).limit(1)) is not None


async def _existing(session: AsyncSession, backup_id: str, mode: str, user_id,
                    owner_account: dict | None) -> tuple[Company, User] | None:
    """The company this backup was already restored as, with the caller, when the caller
    may have it; refused when it exists but belongs to someone else."""
    company = await session.scalar(select(Company).where(
        text("companies.settings -> 'restored_backup' ->> 'backup_id' = :b").bindparams(b=backup_id)).limit(1))
    if company is None:
        return None
    if mode == "bootstrap":
        user = await session.scalar(select(User).where(User.email == owner_account["email"]).limit(1))
        if (user is None or not user.auth_hash or not verify_password(owner_account["password"], user.auth_hash)
                or not await _is_member(session, user.id, company.id)):
            raise BackupError(409, ALREADY_SET_UP)
        return company, user
    user = await session.get(User, uuid.UUID(str(user_id)))
    if user is None or not await _is_member(session, user.id, company.id):
        raise BackupError(409, NOT_A_MEMBER)
    return company, user


def _next_rows(it, limit: int) -> list:
    rows = []
    for line in it:
        rows.append(_parse_row(line))
        if len(rows) >= limit:
            break
    return rows


async def _insert(session: AsyncSession, zf: zipfile.ZipFile, table: str, columns: list[str],
                  id_map: dict[str, str]) -> None:
    cols = ", ".join(_ident(c) for c in columns)
    picked = ", ".join(f"r.{_ident(c)}" for c in columns)
    statement = text(
        f"INSERT INTO {_ident(table)} ({cols}) SELECT {picked} "
        f"FROM jsonb_array_elements(CAST(:rows AS jsonb)) WITH ORDINALITY AS e(v, n), "
        f"jsonb_populate_record(NULL::{_ident(table)}, e.v) AS r ORDER BY e.n")
    it = _lines(zf, f"tables/{table}.jsonl")
    while rows := await asyncio.to_thread(_next_rows, it, BATCH_ROWS):
        await session.execute(statement, {"rows": _dump_rows(remap(rows, id_map))})


async def _verify(session: AsyncSession, checked: _Checked, manifest: dict, company_id,
                  back: dict[str, str]) -> None:
    """Read every restored table back, map the ids back and compare it with the backup."""
    for name in checked.order:
        table, meta = checked.plan.schema[name], manifest["tables"][name]
        expr = _without([c for c in table.columns if c not in set(meta["columns"])])
        total, rows = 0, 0
        async for batch in _batches(session, table, company_id, expr):
            for line in batch:
                total += _row_digest(remap(_parse_row(line), back))
            rows += len(batch)
        if rows != meta["rows"] or total % (1 << 256) != checked.digests[name]:
            raise BackupError(422, MISMATCH.format(table=name))


async def restore_company(path: Path, *, mode: str, user_id=None, current_company_id=None,
                          owner_account: dict | None = None) -> RestoreResult:
    """Restore a backup file as a new company and commit it.

    ``settings`` and ``new_company`` restore it for the signed-in ``user_id``;
    ``bootstrap`` creates the installation's first owner from ``owner_account``
    ({name, email, password}) in the same transaction. Restoring a backup that was
    already restored here returns that company (``created`` False) to its members."""
    if mode not in MODES:
        raise ValueError(f"Unknown restore mode: {mode}")
    if mode == "bootstrap" and not (isinstance(owner_account, dict)
                                    and {"name", "email", "password"} <= set(owner_account)):
        raise ValueError("A bootstrap restore needs the first owner's name, email and password.")
    backup = await asyncio.to_thread(read_backup, path)
    m = backup.manifest
    backup_id, source = m["backup_id"], m["company"]["id"]
    new_id = uuid.uuid4()
    stored = False
    async with AsyncSession(bind=celerp.db.engine, expire_on_commit=False) as session:
        try:
            await session.execute(text("SET LOCAL TimeZone = 'UTC'"))
            checked = await check_backup(session, backup)
            await _lock(session, backup_id, bootstrapping=mode == "bootstrap")
            found = await _existing(session, backup_id, mode, user_id, owner_account)
            if found is not None:
                company, user = found
                result = RestoreResult(company_id=str(company.id), company_name=company.name, created=False,
                                       backup_created_at=m["created_at"], user_id=str(user.id))
                await session.rollback()
                return result
            if mode == "bootstrap":
                if await session.scalar(select(User.id).limit(1)) is not None:
                    raise BackupError(409, ALREADY_SET_UP)
                user = await create_install_owner(session, name=owner_account["name"],
                                                  email=owner_account["email"], password=owner_account["password"])
            else:
                user = await session.get(User, uuid.UUID(str(user_id)))
                if user is None:
                    raise ValueError("The restoring user does not exist.")
            url_map: dict[str, str] = {}
            with zipfile.ZipFile(path) as zf:
                stored = True
                for f in m["attachments"]:
                    content = await asyncio.to_thread(zf.read, f"attachments/{f['name']}")
                    try:
                        url_map[f["url"]] = await attachments.store_company_file(str(new_id), f["name"], content)
                    except Exception:
                        logger.warning("Storing an attachment from a company backup failed", exc_info=True)
                        raise BackupError(422, ATTACHMENT_FAILED) from None
                id_map = {source: str(new_id), **{old: str(uuid.uuid4()) for old in checked.ids}, **url_map}
                settings = remap(_kept_settings(m["company"]["settings"]), id_map)
                settings = set_enabled(settings, get_enabled(settings) | set(m["modules"]["enabled"]))
                settings["restored_backup"] = {
                    "backup_id": backup_id, "created_at": m["created_at"],
                    "source_company_name": m["company"]["name"],
                    "restored_at": datetime.now(timezone.utc).isoformat(),
                    **({"provenance": m["provenance"]} if m.get("provenance") else {}),
                }
                company = await provision_restored_company(session, owner=user, company_name=m["company"]["name"],
                                                           company_id=new_id, settings=settings)
                try:
                    for name in checked.order:
                        await _insert(session, zf, name, m["tables"][name]["columns"], id_map)
                except DBAPIError:
                    raise BackupError(422, UNSAVABLE) from None
            await _verify(session, checked, m, new_id, {new: old for old, new in id_map.items()})
            if mode == "settings" and current_company_id is not None and source == str(current_company_id):
                await session.execute(text(
                    "INSERT INTO user_companies (id, user_id, company_id, role, is_active) "
                    "SELECT gen_random_uuid(), user_id, CAST(:new AS uuid), role, true FROM user_companies "
                    "WHERE company_id = :src AND is_active AND user_id <> :me"),
                    {"new": new_id, "src": uuid.UUID(source), "me": user.id})
            await session.commit()
        except BaseException:
            await session.rollback()
            if stored:
                try:
                    await attachments.delete_company_files(str(new_id))
                except Exception:
                    logger.warning("Removing attachment files of a failed company restore failed", exc_info=True)
            raise
    return RestoreResult(company_id=str(company.id), company_name=company.name, created=True,
                         backup_created_at=m["created_at"], user_id=str(user.id))

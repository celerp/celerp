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
A module says in its manifest's ``company_backup`` which of its tables belong to the
company (``"include"``) and which to this installation (``"exclude"``, such as
credentials or caches); a module table it does not name stops the export.
Tables are written and restored in foreign-key order, a batch at a time.

Restoring creates a new company with fresh ids for the company and every backed-up
row, remaps every value that exactly equals an old id or attachment URL, inserts the
rows, reads them back and checks every table against the backup before committing.
Any failure rolls everything back, including attachment files already stored. A restore
that never finishes, because the process or host stopped, leaves a landing marker for its
new company; ``reconcile_landings`` removes the attachment files of every such company that
was never committed.
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
from celerp.modules.importer import valid_table_prefixes
from celerp.modules.loader import (
    is_core_folded, is_running, module_search_path, read_manifest, resolve_module_path, running_version,
)
from celerp.modules.registry import get_enabled, set_enabled
from celerp.services import attachments, bootstrap, company_lifecycle
from celerp.services.auth import HAS_COMPANY, hold_companyless_login, verify_password
from celerp.services.company_lock import hold_company, lock_company, locked_company
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
    "session_registry": "sign-in sessions issued by this installation",
}

# Settings that describe this installation or its people, not the business.
DROPPED_SETTINGS = frozenset({
    "role_grants", "ai_memory", "lock_date_set_by", "reorder_alert_email", "column_prefs",
    "pay_tip_shown", "reorder_last_scan_at", "restored_backup",
})

# Columns every reader expects to hold a JSON object.
OBJECT_COLUMNS = {"ledger": "data", "projections": "state"}

MODES = frozenset({"settings", "new_company", "start_company", "bootstrap"})

MAX_UPLOAD_BYTES = 2 * 1024 ** 3
MAX_MEMBERS = 200_000
# One table's rows as JSON lines, streamed. 512 MB holds about a million ledger events
# at their usual half a kilobyte each, well past the largest company a copy is made of.
MAX_MEMBER_BYTES = 512 * 1024 ** 2
MAX_TOTAL_BYTES = 8 * 1024 ** 3
# Members read whole: the manifest, one table row, one attachment (the ordinary attachment limit).
MAX_MANIFEST_BYTES = 64 * 1024 ** 2
# One row as JSON. Item and document state is a few kilobytes; the largest real rows are
# documents with thousands of lines and company settings, a few megabytes at most.
MAX_ROW_BYTES = 8 * 1024 ** 2
# One row's values (objects, arrays, keys and scalars). Parsed JSON takes about a hundred
# bytes per value (a row of empty objects is fifty times its text), and a string written
# with escapes about twenty times its text, so with the byte limit a row's memory stays
# under about 200 MB. A document line is about a dozen values, so a row holds a document
# of some 20,000 lines.
MAX_ROW_NODES = 250_000
# How deeply one row's values nest. Real rows nest a handful of levels, and a restore
# walks each row recursively.
MAX_ROW_DEPTH = 100
# Rows are read and written in batches that end at BATCH_ROWS rows or BATCH_BYTES of
# JSON, whichever comes first, so wide rows cannot make one batch large.
BATCH_ROWS = 1000
BATCH_BYTES = 1024 ** 2

_NOT_RESTORED = " Nothing was restored."
_NOT_BACKED_UP = " Nothing was backed up."
NOT_A_BACKUP = "This file is not a Celerp company backup." + _NOT_RESTORED
SYSTEM_BACKUP = "This is a whole-installation backup. Use System Recovery instead." + _NOT_RESTORED
DAMAGED = "This company backup is damaged or was changed after it was made." + _NOT_RESTORED
NEWER = ("This company backup was made by a newer version of Celerp. Update Celerp, then try again."
         + _NOT_RESTORED)
TOO_LARGE = "This company backup is too large to restore here." + _NOT_RESTORED
TOO_LARGE_UPLOAD = "This file is too large for a company backup upload." + _NOT_RESTORED
TOO_LARGE_TO_BACK_UP = "This company holds more data than a company backup can restore." + _NOT_BACKED_UP
ROW_TOO_LARGE_TO_BACK_UP = "One record in {table} is too large for a company backup to restore." + _NOT_BACKED_UP
UNSAVABLE = "This company backup has records this Celerp cannot save." + _NOT_RESTORED
ATTACHMENT_FAILED = "Celerp could not save an attachment file from this backup." + _NOT_RESTORED
ATTACHMENT_TYPE = "This company backup has an attachment file of a type Celerp does not store: {name}." + _NOT_RESTORED
MISMATCH = "The restored company did not match the backup ({table})." + _NOT_RESTORED
FOREIGN = "This company backup refers to records of another company." + _NOT_RESTORED
ALREADY_SET_UP = "This Celerp is already set up." + _NOT_RESTORED
NOT_A_MEMBER = ("This backup was already restored here as a company you are not a member of."
                + _NOT_RESTORED)
DEACTIVATED = ("This backup was already restored here as a company that is now deactivated. Reactivate it instead."
               + _NOT_RESTORED)
STALE_PREVIEW = ("Something changed since this preview. Check the updated preview before continuing."
                 + _NOT_RESTORED)
ATTACHMENT_MISSING = "This company backup refers to an attachment file it does not carry." + _NOT_RESTORED

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_RAW_NUL = re.compile(rb"(?<!\\)(?:\\\\)*\\u0000")
_JSON_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')
_BRACKET = re.compile(rb"[\[\]{}]")
_NUMBER = re.compile(r'"\\u0000([^"\\]*)\\u0000"')
_KEY_TYPES = frozenset({"uuid", "text", "varchar"})
_CHUNK = 1024 * 1024


class BackupError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class StalePreview(BackupError):
    """The restore confirmed a preview that no longer holds; ``plan`` is the current one."""

    def __init__(self, plan: RestorePlan) -> None:
        super().__init__(409, STALE_PREVIEW)
        self.plan = plan


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


INCLUDE, EXCLUDE = "include", "exclude"


def _declared(module: str) -> dict:
    """How the module's manifest says each of its tables travels with a company backup."""
    path = _installed(module)
    declared = (read_manifest(path) if path is not None else {}).get("company_backup")
    return declared if isinstance(declared, dict) else {}


def _refusal(table: str, owners: dict[str, str]) -> BackupError:
    if table in owners:
        return BackupError(409, f"The {owners[table]} module keeps data in {table} in a form Celerp "
                                f"cannot back up yet." + _NOT_BACKED_UP)
    return BackupError(409, f"This company has data Celerp cannot back up yet: {table}." + _NOT_BACKED_UP)


async def _classify(session: AsyncSession, *, strict: bool) -> _Plan:
    """The tables a backup carries, parents first. Strict (export) refuses any company
    table it cannot carry; otherwise (restore) such tables are simply not carried."""
    schema = await _schema(session)
    # Only prefixes that pass the install check attribute tables: a hand-copied
    # module claiming a core or another module's table must not take it over.
    prefixes = valid_table_prefixes()
    declarations = {module: _declared(module) for module in prefixes}
    owners: dict[str, str] = {}
    carried: list[str] = []
    for name in sorted(schema):
        table = schema[name]
        owner = _owner(name, prefixes)
        if name in EXCLUDED_TABLES or (owner is None and "company_id" not in table.columns):
            continue
        if owner is not None and name not in PORTABLE_TABLES:
            how = declarations[owner].get(name)
            if how == EXCLUDE:
                continue
            owners[name] = owner
            if how != INCLUDE:
                if strict:
                    raise BackupError(409, f"The {owner} module has not said whether {name} belongs in a company "
                                           f"backup." + _NOT_BACKED_UP)
                continue
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
        order, unordered = _fk_order(carried, schema)
        for name in list(carried):
            if name in unordered or any(schema[name].columns[c].notnull for cols, target, _ in schema[name].fks
                                        if target != "companies" and target not in keep for c in cols):
                if strict:
                    raise _refusal(name, owners)
                carried.remove(name)
                changed = True
    return _Plan(order=order, schema=schema, owners=owners)


def _fk_order(tables: list[str], schema: dict[str, _Table]) -> tuple[list[str], set[str]]:
    """Tables ordered so each follows every table it references, and the tables no such
    order exists for, which a restore could not insert: those referencing themselves or
    in a reference cycle."""
    listed = set(tables)
    refs = {t: {target for _, target, _ in schema[t].fks if target in listed} for t in tables}
    unordered = {t for t in tables if t in refs[t]}
    parents = {t: refs[t] - {t} for t in tables}
    order: list[str] = []
    while ready := sorted(t for t, p in parents.items() if not p):
        order.extend(ready)
        for t in ready:
            del parents[t]
        for p in parents.values():
            p.difference_update(ready)
    # What is left is in a cycle or references one; only the cycles are unordered.
    while behind := [t for t in parents if not any(t in p for p in parents.values())]:
        for t in behind:
            del parents[t]
    return order, unordered | set(parents)


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


def _uuids(value, found: set[str]) -> None:
    """Add every string in a parsed JSON value that remap() would treat as an id."""
    if isinstance(value, str):
        if _UUID.fullmatch(value):
            found.add(value)
    elif isinstance(value, dict):
        for v in value.values():
            _uuids(v, found)
    elif isinstance(value, list):
        for v in value:
            _uuids(v, found)


def _strings(value):
    """Every string in a parsed JSON value, dict keys excepted."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _refuse_constant(name: str):
    raise ValueError(name)


def _row_too_large(line: bytes) -> bool:
    """Whether a row of JSON would parse to more than MAX_ROW_NODES values or nest deeper
    than MAX_ROW_DEPTH. Every value past the first follows a comma, colon or opening
    bracket outside a string."""
    bare = _JSON_STRING.sub(b"", line)
    if 1 + sum(bare.count(c) for c in (b",", b":", b"[", b"{")) > MAX_ROW_NODES:
        return True
    depth = 0
    for bracket in _BRACKET.findall(bare):
        depth += 1 if bracket in b"[{" else -1
        if depth > MAX_ROW_DEPTH:
            return True
    return False


def _parse_row(line: bytes | str):
    """One row, with every non-integer number kept as its exact text so it is written
    back digit for digit. A row too large to parse (_row_too_large) is refused before it
    is parsed."""
    if _row_too_large(line.encode() if isinstance(line, str) else line):
        raise BackupError(422, TOO_LARGE)
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
    """The company's rows of one table as JSON text, in primary-key order, in batches of at
    most BATCH_ROWS rows and BATCH_BYTES of JSON (a batch always holds at least one row).

    The byte cut is made in the database, so rows past it are never sent. After a cut the
    next fetch asks for twice as many rows as fitted, growing back to BATCH_ROWS."""
    q = _ident(table.name)
    udt = {c: table.columns[c].udt for c in table.pk}
    keys = ", ".join(f"t.{_ident(c)}::text AS k{i}" for i, c in enumerate(table.pk))
    natives = ", ".join(f"t.{_ident(c)} AS o{i}" for i, c in enumerate(table.pk))
    picked = ", ".join(f"k{i}" for i in range(len(table.pk)))
    ordered = ", ".join(f"o{i}" for i in range(len(table.pk)))
    order = ", ".join(f"t.{_ident(c)}" for c in table.pk)
    company = f"CAST(CAST(:c AS text) AS {_ident(table.columns['company_id'].udt)})"
    after = (f" AND ({order}) > ("
             + ", ".join(f"CAST(CAST(:k{i} AS text) AS {_ident(udt[c])})" for i, c in enumerate(table.pk)) + ")")
    params: dict = {"c": str(company_id), "n": BATCH_ROWS, "b": BATCH_BYTES}
    first = True
    while True:
        rows = (await session.execute(text(
            f"SELECT {picked}, j, fetched FROM (SELECT s.*, count(*) OVER () AS fetched, "
            f"sum(octet_length(s.j)) OVER (ORDER BY {ordered} ROWS UNBOUNDED PRECEDING) "
            f"- octet_length(s.j) AS before FROM ("
            f"SELECT {keys}, {natives}, ({expr})::text AS j FROM {q} t WHERE t.company_id = {company}"
            f"{'' if first else after} ORDER BY {order} LIMIT :n) s) w "
            f"WHERE w.before < :b ORDER BY {ordered}"), params)).all()
        if not rows:
            return
        yield [r[-2] for r in rows]
        fetched, asked = rows[0][-1], params["n"]
        if fetched < asked and len(rows) == fetched:
            return
        params["n"] = min(BATCH_ROWS, 2 * (asked if len(rows) == fetched else len(rows)))
        first = False
        params.update({f"k{i}": v for i, v in enumerate(rows[-1][:-2])})


def _without(columns: list[str]) -> str:
    expr = "to_jsonb(t)"
    if columns:
        expr = f"({expr} - ARRAY[{', '.join(_literal(c) for c in columns)}]::text[])"
    return expr


# ── Export ───────────────────────────────────────────────────────────────────

def _collect_urls(value, company_id, found: dict[str, str], types: dict[str, str]) -> None:
    """Record every attachment URL stored for ``company_id`` in a value, by stored name, and
    the type recorded beside a URL in the same object."""
    if isinstance(value, str):
        name = attachments.company_file_name(company_id, value)
        if name is not None and found.setdefault(name, value) != value:
            raise BackupError(409, f"This company has two attachment files named {name}." + _NOT_BACKED_UP)
    elif isinstance(value, dict):
        if isinstance(value.get("url"), str) and isinstance(value.get("mime"), str):
            types.setdefault(value["url"], value["mime"])
        for v in value.values():
            _collect_urls(v, company_id, found, types)
    elif isinstance(value, list):
        for v in value:
            _collect_urls(v, company_id, found, types)


def _backup_name(name: str, url: str, types: dict[str, str]) -> str:
    """The name a stored file travels under: its own when it ends in its type's stored
    extension, otherwise renamed to the stored extension of the type recorded beside it
    (files stored before extensions were derived from the type). Refused when neither
    gives an allowed type, since a restore would refuse the file."""
    mime = attachments.stored_file_type(name) or types.get(url)
    backup_name = attachments.stored_file_name(name, mime) if mime else None
    if backup_name is None:
        raise BackupError(409, f"This company has an attachment file of a type Celerp does not store: {url}."
                          + _NOT_BACKED_UP)
    return backup_name


def _installed(name: str):
    return resolve_module_path(name, module_search_path())


def _module_versions(names: set[str]) -> dict[str, str]:
    versions = {}
    for name in sorted(names):
        path = _installed(name)
        version = read_manifest(path).get("version") if path is not None else None
        if isinstance(version, str) and version:
            versions[name] = version
    return versions


async def export_company_snapshot(company_id, out: Path, *, provenance: dict | None = None) -> dict:
    """Write a backup of one company to ``out``; returns its manifest.

    Everything the backup holds is read through its own session in one read-only
    repeatable-read transaction, so the company, its settings, every table and every
    attachment reference come from the same moment: a write committed meanwhile is either
    wholly in the backup or wholly absent from it. Writers are never blocked. SQLite has one
    writer at a time, so there one plain transaction reads the same moment.

    Refused, with nothing written, when the company holds data Celerp cannot back up or is
    deleted before its backup is finished."""
    partial = out.with_name(out.name + ".partial")
    try:
        async with AsyncSession(bind=celerp.db.engine, expire_on_commit=False) as session, session.begin():
            if session.get_bind().dialect.name != "sqlite":
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                await session.execute(text("SET LOCAL TimeZone = 'UTC'"))
            manifest = await _export_company(session, company_id, partial, provenance=provenance)
        # The snapshot does not hold the company, so it can be reset meanwhile. The file is
        # published only while the company is held: a reset that already committed leaves
        # nothing behind, and a later one deletes the published file with the company.
        async with AsyncSession(bind=celerp.db.engine) as session, session.begin():
            if not await hold_company(session, company_id):
                raise BackupError(404, "Company not found.")
            partial.replace(out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return manifest


async def _export_company(session: AsyncSession, company_id, partial: Path, *, provenance: dict | None) -> dict:
    """Write the backup to ``partial``; the caller publishes it or deletes it."""
    company = await session.get(Company, company_id)
    if company is None:
        raise BackupError(404, "Company not found.")
    plan = await _classify(session, strict=True)
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
    types: dict[str, str] = {}
    _collect_urls(settings, company_id, found, types)
    # A module enabled in settings but not installed here is not something this company's
    # data depends on, so it is not a requirement of the backup.
    enabled = {name for name in get_enabled(settings) if _installed(name) is not None}
    manifest: dict = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "backup_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "company": {"id": str(company.id), "name": company.name, "settings": settings},
        **({"provenance": provenance} if provenance else {}),
        "modules": {"enabled": sorted(enabled),
                    "versions": _module_versions(enabled | {plan.owners[t] for t in tables if t in plan.owners})},
        "tables": {}, "attachments": [],
    }
    partial.parent.mkdir(parents=True, exist_ok=True)
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
                        body = line.encode()
                        if len(body) > MAX_ROW_BYTES or _row_too_large(body):
                            raise BackupError(409, ROW_TOO_LARGE_TO_BACK_UP.format(table=name))
                        _collect_urls(json.loads(line), company_id, found, types)
                        body += b"\n"
                        digest.update(body)
                        fh.write(body)
                    rows += len(batch)
            manifest["tables"][name] = {"columns": table.insertable, "rows": rows, "sha256": digest.hexdigest()}
        names: dict[str, str] = {}
        for name in sorted(found):
            backup_name = _backup_name(name, found[name], types)
            if names.setdefault(backup_name, name) != name:
                raise BackupError(409, f"This company has two attachment files named {backup_name}."
                                  + _NOT_BACKED_UP)
        for backup_name, name in sorted(names.items()):
            url = found[name]
            try:
                body = await attachments.read_company_file(company_id, url,
                                                          _member_limit(f"attachments/{backup_name}"))
            except OSError:
                logger.warning("Reading an attachment for a company backup failed", exc_info=True)
                body = None
            if body is None:
                raise BackupError(409, f"This company has an attachment file Celerp cannot read: {url}."
                                  + _NOT_BACKED_UP)
            zf.writestr(f"attachments/{backup_name}", body)
            manifest["attachments"].append({"url": url, "name": backup_name, "size": len(body),
                                            "sha256": hashlib.sha256(body).hexdigest()})
        body = json.dumps(manifest, indent=1).encode()
        if len(body) > MAX_MANIFEST_BYTES:
            raise BackupError(409, TOO_LARGE_TO_BACK_UP)
        zf.writestr("manifest.json", body)
    # The same limits a restore applies, so no backup is made that restore would refuse.
    if not _within_limits(partial):
        raise BackupError(409, TOO_LARGE_TO_BACK_UP)
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
            and isinstance(f.get("sha256"), str)
            and attachments.company_file_name(company["id"], f["url"]) is not None for f in files)
    if not ok:
        raise BackupError(422, DAMAGED)
    for f in files:
        if attachments.stored_file_type(f["name"]) is None:
            raise BackupError(422, ATTACHMENT_TYPE.format(name=f["name"]))


def _member_limit(name: str) -> int:
    """Uncompressed size a member may have: members read whole into memory are held to
    their own smaller limits."""
    if name == "manifest.json":
        return min(MAX_MEMBER_BYTES, MAX_MANIFEST_BYTES)
    if name.startswith("attachments/"):
        return min(MAX_MEMBER_BYTES, attachments.MAX_FILE_BYTES)
    return MAX_MEMBER_BYTES


class _Budget:
    """Uncompressed bytes read so far, against the per-member and total limits."""

    def __init__(self) -> None:
        self.total = 0

    def chunks(self, zf: zipfile.ZipFile, name: str):
        size, limit = 0, _member_limit(name)
        with zf.open(name) as fh:
            while chunk := fh.read(_CHUNK):
                size += len(chunk)
                self.total += len(chunk)
                if size > limit or self.total > MAX_TOTAL_BYTES:
                    raise BackupError(422, TOO_LARGE)
                yield chunk


def _check_member(budget: _Budget, zf: zipfile.ZipFile, name: str, sha256: str, *,
                  rows: int | None = None, size: int | None = None) -> None:
    digest, length, lines, pending, row = hashlib.sha256(), 0, 0, False, 0
    for chunk in budget.chunks(zf, name):
        digest.update(chunk)
        length += len(chunk)
        if rows is not None:
            parts = chunk.split(b"\n")
            for i, part in enumerate(parts):
                pending = pending or bool(part.strip())
                row += len(part)
                if row > MAX_ROW_BYTES:
                    raise BackupError(422, TOO_LARGE)
                if i < len(parts) - 1:
                    lines += pending
                    pending, row = False, 0
    lines += pending
    if digest.hexdigest() != sha256 or (rows is not None and lines != rows) or (size is not None and length != size):
        raise BackupError(422, DAMAGED)


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    """A member read whole, within its limit."""
    return b"".join(_Budget().chunks(zf, name))


def _within_limits(path: Path) -> bool:
    """Whether a backup file fits every size limit a restore applies: the upload size,
    the member count, each member's uncompressed size and the total. Shared by export
    and read_backup so the two cannot disagree."""
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        return False
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
    return (len(infos) <= MAX_MEMBERS and all(i.file_size <= _member_limit(i.filename) for i in infos)
            and sum(i.file_size for i in infos) <= MAX_TOTAL_BYTES)


def read_backup(path: Path) -> BackupFile:
    """Check a backup file: its format, its size against the limits, and every member
    against its hash."""
    if not zipfile.is_zipfile(path):
        raise BackupError(422, SYSTEM_BACKUP if _looks_like_system_backup(path) else NOT_A_BACKUP)
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if not _within_limits(path):
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
    """Every module the backup needs, enabled or holding its data, is installed here, new
    enough, and running, so its tables are in place."""
    versions = manifest["modules"]["versions"]
    for name in sorted(set(manifest["modules"]["enabled"]) | set(versions)):
        path = _installed(name)
        if path is None:
            raise BackupError(422, f"This company backup needs the {name} module, which is not installed here."
                              + _NOT_RESTORED)
        if not is_running(name):
            raise BackupError(422, f"This company backup needs the {name} module, which is installed here but "
                                   f"not turned on. Turn it on in Modules, restart Celerp, then try again."
                              + _NOT_RESTORED)
        if name not in versions:
            continue
        needed = versions[name]
        # Core-folded modules ship inside Celerp itself, so their installed copy is the running one.
        have = read_manifest(path).get("version") if is_core_folded(name) else running_version(name)
        if not _new_enough(have, needed):
            restart = (" A newer copy is installed; restart Celerp, then try again."
                       if _new_enough(read_manifest(path).get("version"), needed) else "")
            raise BackupError(422, f"This company backup needs the {name} module version {needed} or later."
                              + restart + _NOT_RESTORED)


def _new_enough(have, needed: str) -> bool:
    try:
        return Version(str(have)) >= Version(needed)
    except InvalidVersion:
        return have == needed


def _lines(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        while line := fh.readline(MAX_ROW_BYTES + 1):
            if len(line) > MAX_ROW_BYTES and not line.endswith(b"\n"):
                raise BackupError(422, TOO_LARGE)
            if line.strip():
                yield line


def _scan_rows(backup: BackupFile, order: list[str], plan: _Plan) -> tuple[set[str], dict[str, int], set[str]]:
    """Check every row before anything is written: its shape, its keys, and that every
    reference points at the backup's own company, at a row the backup carries, or nowhere.
    Every attachment file of the backup's company it refers to must be one it carries.
    Returns the ids to replace, each table's row digest, and every other id-shaped value
    the backup holds, in its rows or its company settings."""
    m = backup.manifest
    source = m["company"]["id"]
    carried = set(order)
    files = {f["url"] for f in m["attachments"]}

    def check_files(value) -> None:
        if any(attachments.company_file_name(source, s) is not None and s not in files for s in _strings(value)):
            raise BackupError(422, ATTACHMENT_MISSING)

    ids: set[str] = set()
    seen: set[str] = set()
    check_files(m["company"]["settings"])
    _uuids(m["company"]["settings"], seen)
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
                _uuids(row, seen)
                check_files(row)
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
    return ids, digests, seen - ids - {source}


async def _check_foreign(session: AsyncSession, plan: _Plan, source: str, values: set[str]) -> None:
    """Refuse a backup holding the id of another company here, or of a record another
    company owns. The source company's own ids in tables that stay with the installation
    name nothing the restored company can reach, so they are left as they are."""
    if not values:
        return
    wanted = sorted(values)
    if await session.scalar(text("SELECT 1 FROM companies WHERE id = ANY(CAST(:v AS uuid[])) "
                                 "AND id <> CAST(:s AS uuid) LIMIT 1"), {"v": wanted, "s": source}):
        raise BackupError(422, FOREIGN)
    for name, table in plan.schema.items():
        if len(table.pk) != 1 or "company_id" not in table.columns:
            continue
        key = table.columns[table.pk[0]].udt
        if key not in _KEY_TYPES:
            continue
        kept = "CAST(company_id AS text) IS DISTINCT FROM :s" if name in EXCLUDED_TABLES else "true"
        if await session.scalar(text(
                f"SELECT 1 FROM {_ident(name)} WHERE {_ident(table.pk[0])} = ANY(CAST(:v AS {key}[])) "
                f"AND {kept} LIMIT 1"), {"v": wanted, "s": source}):
            raise BackupError(422, FOREIGN)


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
    ids, digests, others = await asyncio.to_thread(_scan_rows, backup, order, plan)
    await _check_foreign(session, plan, backup.manifest["company"]["id"], others)
    return _Checked(plan=plan, order=order, ids=ids, digests=digests)


# ── Restoring ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RestoreResult:
    company_id: str
    company_name: str
    created: bool
    backup_created_at: str
    user_id: str
    team_members: int


def _lock_key(backup_id: str) -> int:
    return int.from_bytes(hashlib.sha256(backup_id.encode()).digest()[:8], "big", signed=True)


def _landing_key(company_id) -> int:
    return _lock_key(f"landing:{company_id}")


async def _land(session: AsyncSession, company_id) -> None:
    """Mark the new company's attachment files as landing, held by this transaction: the
    mark outlives a crash, and the lock ends with the transaction, whether it commits,
    rolls back or its connection is lost."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _landing_key(company_id)})
    await asyncio.to_thread(attachments.mark_landing, str(company_id))


async def reconcile_landings() -> None:
    """Remove the attachment files of every company restore that stopped before it committed,
    then its landing mark. A restore still in progress is left alone."""
    for name in await asyncio.to_thread(attachments.landing_companies):
        try:
            company_id = uuid.UUID(name)
        except ValueError:
            continue
        async with AsyncSession(bind=celerp.db.engine) as session:
            if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                        {"k": _landing_key(company_id)}):
                continue
            try:
                if await session.get(Company, company_id) is None:
                    await attachments.delete_company_files(name)
                await asyncio.to_thread(attachments.clear_landing, name)
            except Exception:
                logger.warning("Removing the files of an unfinished company restore failed", exc_info=True)
            await session.rollback()


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


def _restored_as(backup_id: str):
    """Companies this backup was restored as, active or deactivated, active first."""
    return (select(Company)
            .where(text("companies.settings -> 'restored_backup' ->> 'backup_id' = :b").bindparams(b=backup_id))
            .order_by(Company.is_active.desc(), Company.created_at, Company.id).limit(1))


async def _bootstrap_existing(session: AsyncSession, backup_id: str,
                              owner_account: dict) -> tuple[Company, User] | None:
    """The company a bootstrap restore of this backup already created, with its owner, when
    the same account repeats it; refused for anyone else or when that company is deactivated."""
    company = await session.scalar(_restored_as(backup_id))
    if company is None:
        return None
    user = await session.scalar(select(User).where(User.email == owner_account["email"]).limit(1))
    if (not company.is_active or user is None or not user.auth_hash
            or not verify_password(owner_account["password"], user.auth_hash)
            or not await _is_member(session, user.id, company.id)):
        raise BackupError(409, ALREADY_SET_UP)
    return company, user


CREATE = "create"
RETURN_EXISTING = "return_existing"
ADD_TEAM = "return_existing_and_add_team"
OFFER_REACTIVATE = "offer_reactivate"
REFUSE = "refuse"


@dataclass(frozen=True)
class RestorePlan:
    """What restoring a backup does for one caller: the action, the company it lands in
    (named only to a caller entitled to it), the team members it gives access and whose
    role permissions they work under, with the source's role permissions carried.
    ``fingerprint`` identifies these facts, so a restore can tell whether the preview it
    confirms still holds."""
    action: str
    destination_id: str | None
    destination_name: str | None
    team_to_add: tuple[tuple[str, str], ...]
    team_blocked: int
    carry_role_grants: bool
    destination_policy: str
    fingerprint: str
    role_grants: dict | None = None

    def public(self) -> dict:
        return {"action": self.action, "destination_id": self.destination_id,
                "destination_name": self.destination_name, "team_members": len(self.team_to_add),
                "team_blocked": self.team_blocked, "carry_role_grants": self.carry_role_grants,
                "destination_policy": self.destination_policy, "plan_fingerprint": self.fingerprint}


def _planned(backup_id: str, mode: str, action: str, destination: Company | None = None, *,
             team: tuple[tuple[str, str], ...] = (), blocked: int = 0, carry: bool = False,
             grants: dict | None = None) -> RestorePlan:
    facts = {"backup_id": backup_id, "mode": mode, "action": action,
             "destination_id": str(destination.id) if destination is not None else None,
             "team": [list(m) for m in team], "blocked": blocked, "carry": carry,
             "policy": "source" if carry else "destination", "grants": grants if carry else None}
    digest = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return RestorePlan(action=action, destination_id=facts["destination_id"],
                       destination_name=destination.name if destination is not None else None,
                       team_to_add=team, team_blocked=blocked, carry_role_grants=carry,
                       destination_policy=facts["policy"], fingerprint=digest, role_grants=facts["grants"])


async def _missing_team(session: AsyncSession, source: str, user_id: uuid.UUID,
                        destination_id: uuid.UUID | None) -> tuple[tuple[str, str], ...]:
    """The source company's other active members, with their roles, who have no membership
    of the destination at all (an inactive one counts: removed access stays removed)."""
    rows = await session.execute(text(
        "SELECT CAST(s.user_id AS text), s.role FROM user_companies s "
        "WHERE s.company_id = CAST(:src AS uuid) AND s.is_active AND s.user_id <> :me "
        "AND NOT EXISTS (SELECT 1 FROM user_companies d WHERE d.user_id = s.user_id "
        "AND d.company_id = CAST(:dest AS uuid)) ORDER BY s.user_id"),
        {"src": source, "me": user_id, "dest": str(destination_id) if destination_id else None})
    return tuple((uid, role) for uid, role in rows.all())


async def _plan(session: AsyncSession, backup: BackupFile, mode: str, user_id, current_company_id, *,
                lock: bool) -> tuple[RestorePlan, Company | None]:
    """The plan and the destination company. With ``lock`` the source and destination
    companies (in id order) and the caller's membership of the destination are locked, so
    the plan, including the source team and role permissions it carries, holds until the
    transaction ends."""
    m = backup.manifest
    backup_id, source = m["backup_id"], m["company"]["id"]
    me = uuid.UUID(str(user_id))
    same_lineage = mode == "settings" and current_company_id is not None and str(current_company_id) == source
    found = await session.scalar(_restored_as(backup_id))
    if lock:
        for cid in sorted({*([source] if same_lineage else []), *([str(found.id)] if found is not None else [])}):
            await lock_company(session, uuid.UUID(cid))
    if found is None:
        team = await _missing_team(session, source, me, None) if same_lineage else ()
        grants = await _source_grants(session, source) if same_lineage else None
        return _planned(backup_id, mode, CREATE, team=team, carry=same_lineage, grants=grants), None
    destination = await locked_company(session, found.id) if lock else found
    membership = select(UserCompany.role, UserCompany.is_active).where(
        UserCompany.user_id == me, UserCompany.company_id == destination.id)
    row = (await session.execute(membership.with_for_update(read=True) if lock else membership)).first()
    role = row.role if row is not None and row.is_active else None
    if not destination.is_active:
        if role == "owner":
            return _planned(backup_id, mode, OFFER_REACTIVATE, destination), destination
        return _planned(backup_id, mode, REFUSE), None
    if role is None:
        return _planned(backup_id, mode, REFUSE), None
    missing = await _missing_team(session, source, me, destination.id) if same_lineage else ()
    if not missing:
        return _planned(backup_id, mode, RETURN_EXISTING, destination), destination
    if role != "owner":
        return _planned(backup_id, mode, RETURN_EXISTING, destination, blocked=len(missing)), destination
    settings = destination.settings or {}
    carry = not (settings.get("restored_backup") or {}).get("team_policy_carried") and not settings.get("role_grants")
    grants = await _source_grants(session, source) if carry else None
    return _planned(backup_id, mode, ADD_TEAM, destination, team=missing, carry=carry, grants=grants), destination


async def plan_existing_restore(session: AsyncSession, backup: BackupFile, mode: str, user_id,
                                current_company_id) -> RestorePlan:
    """What restoring this backup would do for the caller, read without writing or locking.
    ``settings`` restores from the caller's current company; only a backup of that same
    company gives its team access."""
    return (await _plan(session, backup, mode, user_id, current_company_id, lock=False))[0]


async def _add_team(session: AsyncSession, company_id, team: tuple[tuple[str, str], ...]) -> int:
    """Give each (user id, role) access to the company unless they already have a membership
    of it, active or not, which is left as it is. Returns how many were added."""
    rows = await session.execute(text(
        "INSERT INTO user_companies (id, user_id, company_id, role, is_active) "
        "SELECT gen_random_uuid(), t.u, CAST(:c AS uuid), t.r, true "
        "FROM unnest(CAST(:u AS uuid[]), CAST(:r AS text[])) AS t(u, r) "
        "ON CONFLICT (user_id, company_id) DO NOTHING RETURNING id"),
        {"c": str(company_id), "u": [u for u, _ in team], "r": [r for _, r in team]})
    return len(rows.all())


async def _source_grants(session: AsyncSession, source: str):
    current = await session.get(Company, uuid.UUID(source), populate_existing=True)
    return ((current.settings or {}) if current is not None else {}).get("role_grants")


def _row_batches(lines, limit: int):
    """Parsed rows in batches of up to ``limit`` rows and BATCH_BYTES of JSON. The budget
    is checked before a row is parsed, so a batch never holds more than BATCH_BYTES of
    rows (or one row, which MAX_ROW_NODES and MAX_ROW_DEPTH bound)."""
    rows, size = [], 0
    for line in lines:
        if rows and (len(rows) >= limit or size + len(line) > BATCH_BYTES):
            yield rows
            rows, size = [], 0
        rows.append(_parse_row(line))
        size += len(line)
    if rows:
        yield rows


async def _insert(session: AsyncSession, zf: zipfile.ZipFile, table: str, columns: list[str],
                  id_map: dict[str, str]) -> None:
    cols = ", ".join(_ident(c) for c in columns)
    picked = ", ".join(f"r.{_ident(c)}" for c in columns)
    statement = text(
        f"INSERT INTO {_ident(table)} ({cols}) SELECT {picked} "
        f"FROM jsonb_array_elements(CAST(:rows AS jsonb)) WITH ORDINALITY AS e(v, n), "
        f"jsonb_populate_record(NULL::{_ident(table)}, e.v) AS r ORDER BY e.n")
    batches = _row_batches(_lines(zf, f"tables/{table}.jsonl"), BATCH_ROWS)
    while rows := await asyncio.to_thread(next, batches, None):
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


async def _apply_existing(session: AsyncSession, plan: RestorePlan, destination: Company, source: str) -> int:
    """Give the missing team access to the company the backup was already restored as.
    The first time a team is carried into it, the source's role permissions come along
    unless it has its own; after that restoring never writes them again."""
    if plan.action != ADD_TEAM:
        return 0
    added = await _add_team(session, destination.id, plan.team_to_add)
    settings = dict(destination.settings or {})
    if plan.role_grants:
        settings["role_grants"] = plan.role_grants
    settings["restored_backup"] = {**(settings.get("restored_backup") or {}), "team_policy_carried": True}
    destination.settings = settings
    return added


async def _turn_off_shop_sync(session: AsyncSession, company_id, actor_id) -> None:
    """A restored company pushes no item to a store until its owner opts the item in
    again. Recorded as events, so a projection rebuild keeps the items opted out."""
    from celerp.events.engine import emit_event
    from celerp.models.projections import Projection

    opted_in = (await session.scalars(
        select(Projection.entity_id)
        .where(Projection.company_id == company_id, Projection.entity_type == "item",
               Projection.is_sync_to_shopify.is_(True))
        .order_by(Projection.entity_id)
    )).all()
    for entity_id in opted_in:
        await emit_event(session, company_id=company_id, entity_id=entity_id, entity_type="item",
                         event_type="shop.sync.disabled", data={}, actor_id=actor_id, location_id=None,
                         source="restore", idempotency_key=f"company-restore:shop-sync-off:{entity_id}",
                         metadata_={})


async def restore_company(path: Path, *, mode: str, user_id=None, current_company_id=None,
                          owner_account: dict | None = None, plan_fingerprint: str | None = None) -> RestoreResult:
    """Restore a backup file as a new company and commit it.

    ``settings`` and ``new_company`` restore it for the signed-in ``user_id`` as the
    preview identified by ``plan_fingerprint`` showed it; a preview that no longer holds
    raises StalePreview. ``start_company`` does the same for a login left with no company,
    and refuses once it has one. ``bootstrap`` creates the installation's first owner from
    ``owner_account`` ({name, email, password}) in the same transaction. Restoring a
    backup that was already restored here returns that company (``created`` False) to its
    members, never reactivating it when it is deactivated."""
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
    await reconcile_landings()
    async with AsyncSession(bind=celerp.db.engine, expire_on_commit=False) as session:
        try:
            await session.execute(text("SET LOCAL TimeZone = 'UTC'"))
            checked = await check_backup(session, backup)
            await _lock(session, backup_id, bootstrapping=mode == "bootstrap")
            if mode == "bootstrap":
                plan = _planned(backup_id, mode, CREATE)
                found = await _bootstrap_existing(session, backup_id, owner_account)
                if found is not None:
                    company, user = found
                    result = RestoreResult(company_id=str(company.id), company_name=company.name, created=False,
                                           backup_created_at=m["created_at"], user_id=str(user.id), team_members=0)
                    await session.rollback()
                    return result
            else:
                if mode == "start_company" and not await hold_companyless_login(session, user_id):
                    raise BackupError(409, HAS_COMPANY)
                plan, destination = await _plan(session, backup, mode, user_id, current_company_id, lock=True)
                if plan.action == REFUSE:
                    raise BackupError(409, NOT_A_MEMBER)
                if plan.action == OFFER_REACTIVATE:
                    raise BackupError(409, DEACTIVATED)
                # Opening the existing company with nothing to add is what any preview of it
                # led to, so it needs no matching preview.
                settled = plan.action == RETURN_EXISTING and not plan.team_blocked
                if not settled and plan.fingerprint != plan_fingerprint:
                    raise StalePreview(plan)
                if destination is not None:
                    team = await _apply_existing(session, plan, destination, source)
                    result = RestoreResult(company_id=str(destination.id), company_name=destination.name,
                                           created=False, backup_created_at=m["created_at"], user_id=str(user_id),
                                           team_members=team)
                    await session.commit()
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
                await _land(session, new_id)
                stored = True
                for f in m["attachments"]:
                    content = await asyncio.to_thread(_read_member, zf, f"attachments/{f['name']}")
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
                    "team_policy_carried": plan.carry_role_grants,
                }
                # The carried team keeps what its roles may do in the current company.
                if plan.role_grants:
                    settings["role_grants"] = plan.role_grants
                company = await provision_restored_company(session, owner=user, company_name=m["company"]["name"],
                                                           company_id=new_id, settings=settings)
                try:
                    for name in checked.order:
                        await _insert(session, zf, name, m["tables"][name]["columns"], id_map)
                except DBAPIError:
                    raise BackupError(422, UNSAVABLE) from None
            await _verify(session, checked, m, new_id, {new: old for old, new in id_map.items()})
            await _turn_off_shop_sync(session, new_id, user.id)
            team = await _add_team(session, new_id, plan.team_to_add) if plan.team_to_add else 0
            await session.commit()
        except BaseException:
            await session.rollback()
            if stored:
                try:
                    await attachments.delete_company_files(str(new_id))
                    await asyncio.to_thread(attachments.clear_landing, str(new_id))
                except Exception:
                    logger.warning("Removing attachment files of a failed company restore failed", exc_info=True)
            raise
    if stored:
        await asyncio.to_thread(attachments.clear_landing, str(new_id))
    return RestoreResult(company_id=str(company.id), company_name=company.name, created=True,
                         backup_created_at=m["created_at"], user_id=str(user.id), team_members=team)


async def reactivate_restored(path: Path, *, mode: str, user_id, current_company_id,
                              plan_fingerprint: str | None) -> company_lifecycle.Reactivated:
    """Reactivate the deactivated company this backup was already restored as, for one of
    its owners, as the preview identified by ``plan_fingerprint`` offered it; commits.

    Repeating it once the company is active again returns that company unchanged to its
    members. Anyone else gets the same refusal as for any company they may not open."""
    backup = await asyncio.to_thread(read_backup, path)
    async with AsyncSession(bind=celerp.db.engine, expire_on_commit=False) as session:
        await _lock(session, backup.manifest["backup_id"], bootstrapping=False)
        plan, destination = await _plan(session, backup, mode, user_id, current_company_id, lock=True)
        if plan.action == REFUSE:
            raise BackupError(409, NOT_A_MEMBER)
        if plan.action != OFFER_REACTIVATE:
            if destination is None:  # nothing was restored from it yet, so there is nothing to reactivate
                raise StalePreview(plan)
            done = company_lifecycle.Reactivated(company_id=destination.id, company_name=destination.name,
                                                 reactivated=False, connectors_to_reconnect=[])
            await session.rollback()
            return done
        if plan.fingerprint != plan_fingerprint:
            raise StalePreview(plan)
        try:
            return await company_lifecycle.reactivate_company(session, destination.id, user_id)
        except company_lifecycle.NotAnOwner:
            raise BackupError(409, NOT_A_MEMBER) from None

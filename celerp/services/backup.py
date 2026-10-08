# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Backup primitives — pg_dump/pg_restore and AES-256-GCM encrypt/decrypt.

Encryption:
  - Key: 32-byte random, base64-encoded, stored in config.toml [cloud] section
  - Nonce: 12-byte random, prepended to ciphertext
  - Wire format: nonce (12 bytes) + ciphertext (variable) + tag (16 bytes, appended by GCM)

These primitives are shared by the local export/import path and the content-addressed
cloud snapshot client (``backup_repo``).
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from celerp.config import settings

_NONCE_BYTES = 12

# macOS fallback dirs for .app builds whose PATH is stripped to /usr/bin:/bin:/usr/sbin:/sbin.
# shutil.which() is tried first (handles terminal launches, asdf, nix, mise, etc.).
# This list is only consulted when which() comes up empty.
_PG_CANDIDATE_DIRS: tuple[Path, ...] = (
    # Homebrew unversioned formula — symlinks land here on both architectures
    Path("/opt/homebrew/bin"),              # Apple Silicon
    Path("/usr/local/bin"),                 # Intel
    # Homebrew versioned formulae (Apple Silicon)
    Path("/opt/homebrew/opt/postgresql@17/bin"),
    Path("/opt/homebrew/opt/postgresql@16/bin"),
    Path("/opt/homebrew/opt/postgresql@15/bin"),
    Path("/opt/homebrew/opt/postgresql@14/bin"),
    Path("/opt/homebrew/opt/postgresql@13/bin"),
    # Homebrew versioned formulae (Intel)
    Path("/usr/local/opt/postgresql@17/bin"),
    Path("/usr/local/opt/postgresql@16/bin"),
    Path("/usr/local/opt/postgresql@15/bin"),
    Path("/usr/local/opt/postgresql@14/bin"),
    Path("/usr/local/opt/postgresql@13/bin"),
    # Postgres.app
    Path("/Applications/Postgres.app/Contents/Versions/latest/bin"),
    # EnterpriseDB / official installer
    Path("/Library/PostgreSQL/17/bin"),
    Path("/Library/PostgreSQL/16/bin"),
    Path("/Library/PostgreSQL/15/bin"),
    Path("/Library/PostgreSQL/14/bin"),
)


def _find_pg_tool(name: str) -> str:
    """Return the full path to a PostgreSQL tool (pg_dump, pg_restore, …).

    Resolution order:
      1. settings.pg_bin_dir — explicit override; set by the Electron app via
         CELERP_PG_BIN_DIR (points to bundled tools inside the .app bundle) or
         by the user via config.toml [backup] pg_bin_dir. When set it is
         AUTHORITATIVE: the tool must resolve here or we fail loudly — no PATH
         fallback. A packaged build pointed at a missing bundle must not silently
         dump with a system pg_dump of unknown version (incompatible/corrupt
         backups).
      2. shutil.which() — respects the current process PATH; works for any
         installation that correctly exports its bin dir (terminal, asdf, nix,
         mise, MacPorts, unversioned Homebrew formula, …). Only reached when
         pg_bin_dir is unset (dev, self-hosted without an override).
      3. macOS candidate dirs — legacy fallback for .app / Electron builds that
         predate the CELERP_PG_BIN_DIR injection (Homebrew, Postgres.app, EDB).

    Raises FileNotFoundError with a clear message if the tool cannot be found.
    """
    import shutil
    # On Windows the tools are pg_dump.exe / pg_restore.exe.
    names = [name, f"{name}.exe"] if sys.platform == "win32" else [name]
    if settings.pg_bin_dir:
        for n in names:
            candidate = Path(settings.pg_bin_dir) / n
            if candidate.is_file():
                return str(candidate)
        # Explicit bundle dir given but the tool isn't in it: packaging error or
        # a bad user override. Fail loudly rather than falling back to a system
        # pg_dump of unknown version, which risks an incompatible/corrupt backup.
        raise FileNotFoundError(
            f"{name} not found in configured pg_bin_dir ({settings.pg_bin_dir}). "
            "The bundled PostgreSQL tools are missing — reinstall the app, or set "
            "CELERP_PG_BIN_DIR to a valid client-tools directory."
        )
    if found := shutil.which(name):
        return found
    if sys.platform == "darwin":
        for d in _PG_CANDIDATE_DIRS:
            candidate = d / name
            if candidate.is_file():
                return str(candidate)
    raise FileNotFoundError(
        f"{name} not found. Install PostgreSQL client tools or set CELERP_PG_BIN_DIR."
    )


@dataclass
class BackupResult:
    ok: bool
    size_bytes: int
    error: str | None = None
    # Non-fatal issues the user should be told about (e.g. modules enabled
    # on the source that aren't installed on the destination). Distinct
    # from `error` (which means the operation failed).
    warnings: list[str] = field(default_factory=list)
    # True when the import scheduled an automatic server restart (the restored
    # module set differed); the UI tells the user instead of dying silently.
    restart_scheduled: bool = False
    # A recovery that could not make its safety archive changed nothing and waits
    # for the owner to continue without one: the staged recovery's id and the
    # sha256 of the staged archive the confirmation is bound to.
    needs_confirmation: bool = False
    confirmation_id: str | None = None
    archive_digest: str | None = None
    # Where the safety archive of the replaced installation was saved.
    safety_archive: str | None = None


def _parse_key(b64_key: str) -> bytes:
    """Decode and validate a base64-encoded 32-byte AES key."""
    try:
        key = base64.b64decode(b64_key)
    except Exception as exc:
        raise ValueError(f"BACKUP_ENCRYPTION_KEY is not valid base64: {exc}") from exc
    if len(key) != 32:
        raise ValueError(
            f"BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes, got {len(key)}"
        )
    return key


def dump_database(database_url: str, *, runner=None) -> bytes:
    """Run pg_dump against database_url and return raw dump bytes.

    Raises RuntimeError if pg_dump fails or is not found.
    """
    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    runner = runner or subprocess.run
    try:
        pg_dump = _find_pg_tool("pg_dump")
        result = runner(
            [pg_dump, "--format=custom", "--no-password", pg_url],
            capture_output=True,
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pg_dump not found in PATH — cannot create backup") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("pg_dump timed out after 300 seconds") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"pg_dump failed (exit {result.returncode}): {stderr}")
    return result.stdout


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt plaintext with AES-256-GCM. Returns nonce + ciphertext+tag."""
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(_NONCE_BYTES)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return nonce + ciphertext


def decrypt(blob: bytes, key: bytes) -> bytes:
    """Decrypt AES-256-GCM blob produced by encrypt(). Returns plaintext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < _NONCE_BYTES:
        raise ValueError("Blob too short to contain nonce")
    nonce = blob[:_NONCE_BYTES]
    ciphertext = blob[_NONCE_BYTES:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


# Celerp's own relations: the tables and sequences in public, except extension members.
_CELERP_RELATIONS = """pg_class c
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'S')
   AND NOT EXISTS (SELECT FROM pg_depend e WHERE e.classid = 'pg_class'::regclass
                                             AND e.objid = c.oid AND e.deptype = 'e')"""

# Empties public of Celerp's tables, then of the sequences left, so the restore script
# recreates exactly the backup. No CASCADE: anything else that depends on them makes the
# drop, and the restore with it, fail and change nothing.
_EMPTY_PUBLIC = f"""SET client_min_messages = warning;
DO $$ DECLARE names text; BEGIN
  SELECT string_agg(c.oid::regclass::text, ', ') INTO names FROM {_CELERP_RELATIONS} AND c.relkind <> 'S';
  IF names IS NOT NULL THEN EXECUTE 'DROP TABLE ' || names; END IF;
  SELECT string_agg(c.oid::regclass::text, ', ') INTO names FROM {_CELERP_RELATIONS} AND c.relkind = 'S';
  IF names IS NOT NULL THEN EXECUTE 'DROP SEQUENCE ' || names; END IF;
END $$"""

# What a restore would not replace exactly: any other object in public, a trigger or rule
# on Celerp's tables, one of them this role cannot drop, an object elsewhere depending on
# them, and any schema besides public. Extension members and default privileges, which a
# restore leaves as they are, are left alone, as are statistics on Celerp's tables.
_UNSUPPORTED_OBJECTS = f"""WITH ours AS (SELECT c.oid, c.relowner, c.reltype FROM {_CELERP_RELATIONS})
SELECT DISTINCT found FROM (
  SELECT pg_describe_object(d.classid, d.objid, 0) FROM pg_depend d
   WHERE d.refclassid = 'pg_namespace'::regclass AND d.refobjid = 'public'::regnamespace
     AND d.classid NOT IN ('pg_extension'::regclass, 'pg_default_acl'::regclass)
     AND NOT (d.classid = 'pg_class'::regclass AND d.objid IN (SELECT oid FROM ours))
     AND NOT (d.classid = 'pg_statistic_ext'::regclass
              AND d.objid IN (SELECT oid FROM pg_statistic_ext WHERE stxrelid IN (SELECT oid FROM ours)))
     AND NOT EXISTS (SELECT FROM pg_depend e WHERE e.classid = d.classid AND e.objid = d.objid
                                               AND e.deptype = 'e')
  UNION ALL
  SELECT pg_describe_object('pg_trigger'::regclass, oid, 0) FROM pg_trigger
   WHERE tgrelid IN (SELECT oid FROM ours) AND NOT tgisinternal
  UNION ALL
  SELECT pg_describe_object('pg_rewrite'::regclass, oid, 0) FROM pg_rewrite
   WHERE ev_class IN (SELECT oid FROM ours)
  UNION ALL
  SELECT format('%s, owned by %s', oid::regclass, relowner::regrole) FROM ours
   WHERE NOT pg_has_role(relowner, 'USAGE')
  UNION ALL
  SELECT pg_describe_object(d.classid, d.objid, d.objsubid) FROM pg_depend d
   CROSS JOIN LATERAL pg_identify_object(d.classid, d.objid, d.objsubid) o
   WHERE (d.refclassid = 'pg_class'::regclass AND d.refobjid IN (SELECT oid FROM ours)
          OR d.refclassid = 'pg_type'::regclass AND d.refobjid IN (SELECT reltype FROM ours))
     AND COALESCE(o.schema, (SELECT v.relnamespace::regnamespace::text FROM pg_rewrite r
                              JOIN pg_class v ON v.oid = r.ev_class
                             WHERE d.classid = 'pg_rewrite'::regclass AND r.oid = d.objid))
         NOT IN ('public', 'pg_toast')
  UNION ALL
  SELECT 'schema ' || quote_ident(n.nspname) FROM pg_namespace n
   WHERE n.nspname NOT IN ('public', 'information_schema') AND n.nspname !~ '^pg_'
     AND NOT EXISTS (SELECT FROM pg_depend e WHERE e.classid = 'pg_namespace'::regclass
                                               AND e.objid = n.oid AND e.deptype = 'e')
) AS screen (found)"""

# The pg_restore -l entries of a Celerp backup: its tables and sequences in public with
# their data, defaults, constraints, indexes, partitions, statistics, row security,
# comments and grants, the public schema itself and default privileges, which a restore
# leaves as they are, and extensions, which it drops and creates.
_SCHEMA_ENTRY = r"\d+; \d+ \d+ (?:SCHEMA -|COMMENT - SCHEMA|ACL - SCHEMA) public \S+$"
_EXTENSION_ENTRY = re.compile(r'\d+; \d+ \d+ (?:EXTENSION -|COMMENT - EXTENSION) "?([a-z0-9_-]+)"? $')
_BACKUP_ENTRY = re.compile(
    r"\d+; \d+ \d+ (?:(?:TABLE|TABLE DATA|TABLE ATTACH|SEQUENCE|SEQUENCE OWNED BY|SEQUENCE SET|DEFAULT|CONSTRAINT"
    r"|CHECK CONSTRAINT|FK CONSTRAINT|INDEX|INDEX ATTACH|STATISTICS|ROW SECURITY|POLICY) public "
    r"|COMMENT public (?:TABLE|COLUMN|SEQUENCE|INDEX|CONSTRAINT|STATISTICS|POLICY) |ACL public (?:TABLE|COLUMN|SEQUENCE) "
    r"|DEFAULT ACL \S+ DEFAULT PRIVILEGES FOR )|" + _SCHEMA_ENTRY + "|" + _EXTENSION_ENTRY.pattern)

# Of the extensions named, those this role could not drop and create again in this database.
_UNINSTALLABLE_EXTENSIONS = """SELECT n.name FROM unnest(string_to_array('{}', ' ')) AS n (name)
 WHERE EXISTS (SELECT FROM pg_extension e WHERE e.extname = n.name AND NOT pg_has_role(e.extowner, 'USAGE'))
    OR NOT EXISTS (SELECT FROM pg_available_extensions a
                     JOIN pg_available_extension_versions v ON v.name = a.name AND v.version = a.default_version
                    WHERE a.name = n.name
                      AND (current_setting('is_superuser') = 'on' OR NOT v.superuser
                           OR v.trusted AND has_database_privilege(current_database(), 'CREATE')))"""


def check_restore_target(database_url: str) -> None:
    """ValueError naming what a restore into this database would not replace exactly
    (``_UNSUPPORTED_OBJECTS``); read-only."""
    names = _psql(database_url, _UNSUPPORTED_OBJECTS)
    if names:
        raise ValueError("Celerp restores only its own tables and sequences in the public schema. "
                         "Remove or move these database objects, then try again: "
                         + "; ".join(name for name in names.split("\n") if name))


def _psql(database_url: str, sql: str) -> str:
    """The rows `sql` returns, one per line, read-only."""
    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    return _text(_run_tool([_find_pg_tool("psql"), "-X", "-q", "-w", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                            "-c", sql, "-d", pg_url], None, timeout=60)).strip()


# The script pg_restore writes beside a dump measured 4.6 to 6.7 times the dump's size.
_SCRIPT_PER_DUMP_BYTE = 10
_FREE_SPACE_FLOOR = 1 << 30


def check_free_space(dump_path: Path) -> None:
    need = dump_path.stat().st_size * _SCRIPT_PER_DUMP_BYTE + _FREE_SPACE_FLOOR
    free = shutil.disk_usage(dump_path.parent).free
    if free < need:
        raise ValueError(f"Not enough free disk space to restore this backup: {-(-need >> 20)} MB must be free "
                         f"on the disk holding {dump_path.parent}, and {free >> 20} MB is.")


def check_backup_dump(dump_path: Path, database_url: str,
                      source: str = "the database the backup was taken from") -> None:
    """ValueError naming the entries of a pg_dump archive that are not a Celerp backup's
    (``_BACKUP_ENTRY``), the extensions in it a restore into this database could not
    install, or the roles, collations and tablespaces it needs that this database
    does not have (``_check_server_objects``). `source` names, for the owner, the
    database the dump was taken from."""
    listing = _run_tool([_find_pg_tool("pg_restore"), "-l", dump_path.name], None, timeout=60, cwd=dump_path.parent)
    lines = [line for line in _text(listing).splitlines() if line and not line.startswith(";")]
    other = [line.split(" ", 3)[3] for line in lines if not _BACKUP_ENTRY.match(line)]
    if other:
        raise ValueError("This backup holds database objects Celerp does not restore. Remove them from "
                         f"{source}, then try again: " + "; ".join(other))
    _check_extensions(sorted({match[1] for match in map(_EXTENSION_ENTRY.match, lines) if match}),
                      database_url, source)
    _check_server_objects(dump_path, database_url, source)


# pg_dump writes a policy's roles, unless it is for PUBLIC, as quoted identifiers after TO.
_IDENTIFIER = r'(?:"(?:[^"]|"")*"|[^\s",.;()]+)'
_POLICY_ROLES = re.compile(rf"^CREATE POLICY ({_IDENTIFIER}) ON {_IDENTIFIER}\.({_IDENTIFIER})"
                           rf"(?: AS RESTRICTIVE)?(?: FOR [A-Z]+)? TO ({_IDENTIFIER}(?:, {_IDENTIFIER})*)", re.M)
_COLLATION = re.compile(rf" COLLATE ((?:{_IDENTIFIER}\.)?{_IDENTIFIER})")
_TABLESPACE = re.compile(rf"^SET default_tablespace = ({_IDENTIFIER});$", re.M)
# String literals and comments, which name no object; quoted identifiers are kept.
_TEXT = re.compile(r"""("(?:[^"]|"")*")|'(?:[^']|'')*'|--[^\n]*""")
_MISSING = """SELECT 'role', n FROM unnest(ARRAY[{}]::text[]) AS n
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = n)
UNION ALL SELECT 'collation', n FROM unnest(ARRAY[{}]::text[]) AS n WHERE to_regcollation(n) IS NULL
UNION ALL SELECT 'tablespace', n FROM unnest(ARRAY[{}]::text[]) AS n
 WHERE NOT EXISTS (SELECT FROM pg_tablespace WHERE spcname = n AND has_tablespace_privilege(oid, 'CREATE'))"""


def _unquote(identifier: str) -> str:
    return identifier[1:-1].replace('""', '"') if identifier.startswith('"') else identifier


def _literals(names) -> str:
    return ", ".join("'" + name.replace("'", "''") + "'" for name in sorted(names))


def _check_server_objects(dump_path: Path, database_url: str, source: str) -> None:
    """ValueError naming the roles of the dump's row security policies, and the collations
    and tablespaces of its tables and indexes, that this server does not have or, for a
    tablespace, this role may not use."""
    script = _text(_run_tool([_find_pg_tool("pg_restore"), "--schema-only", "-f", "-", dump_path.name], None,
                             timeout=60, cwd=dump_path.parent))
    script = _TEXT.sub(lambda match: match[1] or "''", script)
    policies = [(_unquote(name), _unquote(table), [_unquote(role) for role in re.findall(_IDENTIFIER, roles)])
                for name, table, roles in _POLICY_ROLES.findall(script)]
    roles = {role for _, _, names in policies for role in names}
    collations = set(_COLLATION.findall(script))
    tablespaces = {_unquote(name) for name in _TABLESPACE.findall(script)} - {"''"}
    rows = _psql(database_url, _MISSING.format(_literals(roles), _literals(collations), _literals(tablespaces)))
    missing = [row.split("|", 1) for row in rows.split("\n") if row]
    absent = {name for kind, name in missing if kind == "role"}
    found = [f"policy {name} on {table} (role {', '.join(r for r in names if r in absent)})"
             for name, table, names in policies if absent.intersection(names)]
    found += [f"{kind} {name}" for kind, name in missing if kind != "role"]
    if found:
        raise ValueError("This backup needs database roles, collations or tablespaces this database "
                         f"does not have. Add them here, or stop using them in {source}, then try again: "
                         + "; ".join(found))


def check_database_extensions(database_url: str) -> None:
    """``check_backup_dump``'s extension check for a dump of this database, before one is taken.
    pg_dump leaves out the extensions built into PostgreSQL."""
    names = _psql(database_url, "SELECT extname FROM pg_extension WHERE oid >= 16384")
    _check_extensions(names.split(), database_url, "this database")


def _check_extensions(extensions: list[str], database_url: str, source: str) -> None:
    if extensions and (blocked := _psql(database_url, _UNINSTALLABLE_EXTENSIONS.format(" ".join(extensions)))):
        raise ValueError("This backup uses database extensions that Celerp's database user cannot install "
                         f"here. Remove them from {source}, or have a database administrator allow that "
                         "user to install them, then try again: " + ", ".join(blocked.split()))


def restore_tools() -> tuple[str, str]:
    """pg_restore and psql, which a database restore needs; RuntimeError naming one that
    is missing or does not run."""
    try:
        tools = _find_pg_tool("pg_restore"), _find_pg_tool("psql")
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    for tool in tools:
        _run_tool([tool, "--version"], None, timeout=10)
    return tools


def restore_database_file(dump_path: Path, database_url: str, *, runner=None) -> None:
    """Replace the database with a pg_dump custom-format file, all or nothing
    (``write_restore_script``, then ``run_restore_script``)."""
    import tempfile

    restore_tools()
    with tempfile.TemporaryDirectory(dir=dump_path.parent) as work:
        script = write_restore_script(dump_path, Path(work) / "restore.sql", runner=runner)
        run_restore_script(script, database_url, runner=runner)


def write_restore_script(dump_path: Path, script: Path, *, runner=None) -> Path:
    """Write the SQL script that restores a pg_dump custom-format file to *script*, in the
    dump's directory or one inside it, and return it. pg_restore reads every entry of the
    dump to write it, so a dump it cannot read fails here, before anything is changed;
    ValueError when the disk has no room for the script and the restore."""
    check_free_space(dump_path)
    pg_restore = _find_pg_tool("pg_restore")
    name = script.relative_to(dump_path.parent).as_posix()
    listing = name.removesuffix(".sql") + ".list"
    script.touch(mode=0o600)
    entries = _text(_run_tool([pg_restore, "-l", dump_path.name], runner, cwd=dump_path.parent))
    (dump_path.parent / listing).write_text("".join(line for line in entries.splitlines(keepends=True)
                                                    if not re.match(_SCHEMA_ENTRY, line)))
    _run_tool([pg_restore, "--clean", "--if-exists", "--no-privileges", "--no-owner",
               "-L", listing, "-f", name, dump_path.name], runner, cwd=dump_path.parent)
    return script


def run_restore_script(script: Path, database_url: str, *, runner=None) -> None:
    """Replace the database with a ``write_restore_script`` script, all or nothing: psql
    empties the public schema and runs the script in one transaction. psql writes from a
    process of its own, so it runs inside the fence's write window
    (celerp.migrations.compatibility). It holds the schema key alone
    (``celerp.cli._migration_lock``), on a connection of its own, so it waits for every
    running company backup or restore and they are refused until it ends."""
    from celerp.cli import _migration_lock
    from celerp.db_url import sync_url
    from celerp.migrations.compatibility import mutating_scope

    psql = _find_pg_tool("psql")
    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    with mutating_scope(sync_url(database_url)) as held, _migration_lock(database_url), held.write_window():
        _run_tool([psql, "-X", "-q", "-w", "-v", "ON_ERROR_STOP=1", "--single-transaction",
                   "-c", _EMPTY_PUBLIC, "-f", script.name, "-d", pg_url], runner, cwd=script.parent)


def _text(output: bytes) -> str:
    """What a PostgreSQL tool printed, with the CRLF line ends it prints on Windows read as LF."""
    return output.decode(errors="replace").replace("\r\n", "\n")


def _run_tool(command: list[str], runner, timeout: int = 600, cwd: Path | None = None) -> bytes:
    """Runs a PostgreSQL tool. Files are given to it relative to `cwd`: on Windows the tools
    read their arguments in the system code page, which cannot hold every folder name,
    while the working directory reaches them whole."""
    name = Path(command[0]).stem
    try:
        result = (runner or subprocess.run)(command, capture_output=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{name} not found") from exc
    except OSError as exc:
        raise RuntimeError(f"{name} could not run: {exc.strerror}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{name} timed out after {timeout} seconds") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"{name} failed (exit {result.returncode}): {stderr}")
    return result.stdout

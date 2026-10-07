# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Celerp CLI — init, start, migrate, status, demo, upgrade."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import click

from celerp.config import config_path as _config_path, read_config as _read_config, write_config as _write_config
from celerp.db_url import sync_url as _sync_url
from celerp.services.auth import MIN_PASSWORD_LENGTH, validate_password

# ── Config helpers ────────────────────────────────────────────────────────────

_DEFAULT_DB_URL = "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"
_DEFAULT_API_PORT = 8000
_DEFAULT_UI_PORT = 8080


def _parse_db_url(db_url: str) -> dict:
    """Extract user, password, host, port, dbname from a postgres URL."""
    import re
    m = re.match(
        r"postgresql(?:\+asyncpg)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/(.+)",
        db_url,
    )
    if not m:
        return {}
    return {
        "user": m.group(1),
        "password": m.group(2),
        "host": m.group(3),
        "port": int(m.group(4) or 5432),
        "dbname": m.group(5),
    }


def _psql(sql: str, db: str = "postgres", *flags: str) -> subprocess.CompletedProcess:
    """Run one statement through psql as the postgres OS user."""
    return subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", db, "-v", "ON_ERROR_STOP=1", *flags, "-c", sql],
        capture_output=True,
        text=True,
    )


def _psql_value(sql: str, db: str = "postgres") -> str:
    """The unaligned single value *sql* returns. Raises RuntimeError on failure."""
    r = _psql(sql, db, "-At")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def _existing_db_compatibility(dbname: str):
    """Classify an existing database for this copy, reading it as the superuser
    (the app role may not reach it yet). None when the database does not exist."""
    from celerp.migrations._data_reconcile import _META_TABLE
    from celerp.migrations.compatibility import decide

    if _psql_value(f"SELECT 1 FROM pg_database WHERE datname = '{dbname}';") != "1":
        return None
    tables = _psql_value(
        f"SELECT to_regclass('public.{_META_TABLE}') IS NOT NULL, "
        f"to_regclass('public.alembic_version') IS NOT NULL;", dbname)
    has_meta, has_stamp = (flag == "t" for flag in tables.split("|"))
    meta = json.loads(_psql_value(
        f"SELECT coalesce(json_object_agg(key, value), '{{}}') FROM public.{_META_TABLE};",
        dbname)) if has_meta else {}
    stamped = json.loads(_psql_value(
        "SELECT coalesce(json_agg(version_num), '[]') FROM public.alembic_version;",
        dbname)) if has_stamp else []
    return decide(meta, stamped)


class ExistingDatabaseUnreachable(RuntimeError):
    """The database exists and this copy may open it, but the app credentials fail.
    Init repairs nothing on an existing database: the operator does, as told."""


def _provision_db(db_url: str, drop_existing: bool = False) -> None:
    """Create the Postgres role and database by shelling out to psql as the postgres OS user.

    Uses `sudo -u postgres psql`, which works on any standard Postgres install
    regardless of pg_hba.conf, since the postgres OS user always has superuser access.
    If drop_existing=True (init --force), resets the role's password and drops and
    recreates the database. Otherwise only a database that does not exist is
    provisioned; an existing one is never changed here, because it may be in use by
    another copy: IncompatibleDatabase when this copy may not open it, else
    ExistingDatabaseUnreachable with the repair for the operator to run. Ownership
    and grants are _fix_ownership's, run by _init_database under the version fence.
    Raises RuntimeError on failure.
    """
    from celerp.migrations.compatibility import IncompatibleDatabase

    parts = _parse_db_url(db_url)
    if not parts:
        raise RuntimeError(f"Could not parse DB URL: {db_url}")

    user = parts["user"]
    password = parts["password"]
    dbname = parts["dbname"]
    role_exists = _psql_value(f"SELECT 1 FROM pg_roles WHERE rolname = '{user}';") == "1"

    if drop_existing:
        verb = "ALTER" if role_exists else "CREATE"
        r = _psql(f"{verb} USER {user} WITH PASSWORD '{password}';")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        click.echo(f"  ✓ Postgres user '{user}' ready")
        # Terminate any active connections before dropping
        _psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{dbname}' AND pid <> pg_backend_pid();")
        r = _psql(f"DROP DATABASE IF EXISTS {dbname};")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        click.echo(f"  · Dropped database '{dbname}'")
    else:
        existing = _existing_db_compatibility(dbname)
        if existing is not None and not existing.ok:
            raise IncompatibleDatabase(existing)
        if existing is not None:
            verb = "ALTER" if role_exists else "CREATE"
            raise ExistingDatabaseUnreachable(
                f"Database '{dbname}' already exists, but user '{user}' cannot sign in to it. "
                f"Init does not change an existing database's users or permissions. "
                f"Set the password to the one in your database URL, then re-run init:\n"
                f"  sudo -u postgres psql -c \"{verb} USER {user} WITH PASSWORD '<password>';\"")
        if not role_exists:
            r = _psql(f"CREATE USER {user} WITH PASSWORD '{password}';")
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip())
            click.echo(f"  ✓ Postgres user '{user}' created")

    # A database created in the meantime makes this fail, leaving it untouched.
    r = _psql(f"CREATE DATABASE {dbname} OWNER {user};")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    click.echo(f"  ✓ Created database '{dbname}'")


def _stop_servers() -> None:
    """Kill any running celerp uvicorn processes (best-effort)."""
    try:
        result = subprocess.run(
            ["pkill", "-f", "uvicorn.*celerp"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            click.echo("  · Stopped running celerp servers")
            time.sleep(1)  # Let ports free up
    except FileNotFoundError:
        pass  # pkill not available; ignore


@contextmanager
def _db_engine(db_url: str, **kwargs):
    """An engine for a step that may change the database, inside its mutating
    scope (celerp.migrations.compatibility.mutating_scope)."""
    from celerp.migrations.compatibility import mutating_scope

    with mutating_scope(_sync_url(db_url)) as held, held.engine(**kwargs) as engine:
        yield engine


def _fix_ownership(db_url: str) -> str | None:
    """Reassign ownership of user-created objects to the app user and grant privileges.

    Uses sudo -u postgres psql (per-object ALTER, not REASSIGN which fails on system objects).
    Returns error string on failure, None on success.
    Call BEFORE migrations so ALTER TABLE etc. succeed as the app user.
    """
    parts = _parse_db_url(db_url)
    if not parts:
        return None
    from celerp.migrations.compatibility import mutating_scope

    user = parts["user"]
    dbname = parts["dbname"]
    with mutating_scope(_sync_url(db_url)) as held, held.write_window():
        return _fix_ownership_statements(user, dbname)


def _fix_ownership_statements(user: str, dbname: str) -> str | None:
    # Change ownership per-table/sequence (avoids REASSIGN system object error)
    for fix_sql in [
        f"DO $$ DECLARE r record; BEGIN "
        f"FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tableowner='postgres' LOOP "
        f"EXECUTE 'ALTER TABLE public.' || quote_ident(r.tablename) || ' OWNER TO {user}'; "
        f"END LOOP; END $$;",
        f"DO $$ DECLARE r record; BEGIN "
        f"FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname='public' AND sequenceowner='postgres' LOOP "
        f"EXECUTE 'ALTER SEQUENCE public.' || quote_ident(r.sequencename) || ' OWNER TO {user}'; "
        f"END LOOP; END $$;",
    ]:
        r = _psql(fix_sql, dbname)
        if r.returncode != 0:
            return r.stderr.strip()
    # Grant schema-level privileges + defaults for future objects
    for sql in [
        f"GRANT ALL PRIVILEGES ON DATABASE {dbname} TO {user};",
        f"GRANT ALL PRIVILEGES ON SCHEMA public TO {user};",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {user};",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {user};",
        f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {user};",
        f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {user};",
    ]:
        _psql(sql, dbname)
    return None


def _needs_ownership_fix(db_url: str) -> bool:
    """Check if any tables in the public schema are NOT owned by the app user."""
    parts = _parse_db_url(db_url)
    if not parts:
        return False
    user = parts["user"]
    from sqlalchemy import text
    with _db_engine(db_url, pool_pre_ping=True, connect_args={"connect_timeout": 5}) as engine:
        try:
            with engine.connect() as conn:
                result = conn.execute(text(
                    "SELECT count(*) FROM pg_tables "
                    "WHERE schemaname = 'public' AND tableowner != :user"
                ), {"user": user})
                return result.scalar() > 0
        except Exception:
            return False


def _post_migration_grants(db_url: str) -> None:
    """Grant privileges on all tables and sequences to the app user.

    Must run AFTER migrations since sequences/tables created by migrations
    won't be covered by ALTER DEFAULT PRIVILEGES set by _fix_ownership.

    Runs in-process over the existing connection rather than shelling out to
    `sudo -u postgres psql` (which does not exist on Windows and crashed the
    bundled launcher's migrate step). On the bundled single-user cluster the
    connecting user is the superuser/owner, so the grants apply; best-effort so a
    grant error never blocks startup.
    """
    parts = _parse_db_url(db_url)
    if not parts:
        return
    user = parts["user"]
    from sqlalchemy import text
    with _db_engine(db_url) as engine:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{user}";'))
                conn.execute(text(f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{user}";'))
        except Exception:
            # Best-effort: on the bundled cluster the app user already owns its objects.
            pass





def _config_to_env(cfg: dict, root: Path | None = None) -> dict:
    """Convert config dict to env vars for a server or command running the
    release installed at `root` (default: the release this process runs)."""
    from celerp import runtime

    env = runtime.base_env()
    env["DATABASE_URL"] = cfg["database"]["url"]
    env["JWT_SECRET"] = cfg["auth"]["jwt_secret"]
    # The UI reaches the API on the configured port, whatever the shell says.
    env["API_URL"] = runtime.api_url(cfg["server"]["api_port"])
    if cfg["cloud"]["token"]:
        env["GATEWAY_TOKEN"] = cfg["cloud"]["token"]
    # A headless service install (`init --no-start`, then a process manager runs
    # `start`) reports its launch channel on activation, the same way the desktop
    # launcher does; a plain local run reports none. setdefault never clobbers an
    # explicit channel.
    if cfg.get("server", {}).get("headless"):
        env.setdefault("CELERP_MODE", "headless")
    # Tells the API a supervisor (`celerp start`) is there to carry out an update.
    env["CELERP_SUPERVISED"] = "1"
    # Module directories: a writable drop-in for imports FIRST (the importer
    # installs into MODULE_DIR.split(",")[0]), then the read-only bundled
    # default (core) and premium (opt-in add-ons) trees. Keeping the writable
    # dir separate means a sideload never lands in default_modules/.
    from celerp.modules.loader import bundled_module_dirs, first_party_names, is_first_party, writable_module_dir
    _pkg_root = root or runtime.package_root()
    _mod_dirs = bundled_module_dirs(_pkg_root)
    _writable_dir = None
    try:
        _writable_dir = writable_module_dir()
        _mod_dirs.insert(0, _writable_dir)
    except OSError:
        # A read-only data dir is unusual; fall back to the bundled dirs so the
        # app still starts. Imports will fail with a clear "no module directory"
        # message rather than silently writing into default_modules/.
        pass
    env["MODULE_DIR"] = ",".join(str(d) for d in _mod_dirs if d.exists())
    # Set ENABLED_MODULES from config (explicit; no implicit defaults)
    enabled = cfg.get("modules", {}).get("enabled", [])
    env["ENABLED_MODULES"] = ",".join(enabled) if enabled else ""
    # Add each module package to PYTHONPATH so intra-module imports resolve.
    # If an old backup left a stale writable copy of a current first-party module,
    # let the verified bundled copy win without deleting the stale directory. The
    # check is lazy, so normal launches with no shadow pay no digest cost.
    _lock_names = first_party_names()
    _extra_paths = []
    for _mod_dir in _mod_dirs:
        if not _mod_dir.exists():
            continue
        for _path in _mod_dir.iterdir():
            if not _path.is_dir() or not (_path / "__init__.py").exists():
                continue
            if (
                _writable_dir is not None
                and _mod_dir == _writable_dir
                and _path.name in _lock_names
                and is_first_party(_pkg_root / "default_modules" / _path.name)
            ):
                continue
            _extra_paths.append(str(_path))
    return runtime.release_env(_pkg_root, env, _extra_paths)


def _test_db(db_url: str) -> str | None:
    """Try connecting to DB. Returns error string or None on success."""
    sync_url = _sync_url(db_url)
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(sync_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return None
    except Exception as e:
        return str(e)


# ── Database mode resolution (external vs embedded) ────────────────────────────
#
# `celerp init` supports two ways to reach Postgres:
#   external — a PostgreSQL server the user (or the droplet/apt) already runs.
#   embedded — a self-contained cluster we boot from bundled binaries when the
#              user has no server. See celerp.embedded_pg.
# The rule is "an existing server always wins": embedded is only a fallback so
# a machine with a real Postgres never ends up with two postmasters.


def _is_root() -> bool:
    """True only where we can `sudo -u postgres` to provision (POSIX, uid 0).

    Guarded with hasattr because os.getuid() does not exist on Windows — the old
    unguarded call raised AttributeError instead of showing guidance there.
    """
    return hasattr(os, "getuid") and os.getuid() == 0


def _classify_probe(err: str | None) -> str:
    """Map a connection attempt's outcome to server reachability.

    Returns:
      "ok"              — connected; server up and the target DB exists.
      "needs_provision" — server is up but the role/database is missing or auth
                          failed; external provisioning (root) can fix it.
      "unreachable"     — nothing is listening (connection refused / timeout).

    "needs_provision" still means a server EXISTS, so init stays in external
    mode rather than booting an embedded cluster beside it.
    """
    if err is None:
        return "ok"
    low = err.lower()
    # Server answered, but the role/db isn't set up yet, or auth was rejected.
    if (
        "does not exist" in low
        or "authentication failed" in low
        or "no password supplied" in low
        or "role" in low and "does not exist" in low
    ):
        return "needs_provision"
    # Nothing accepted the connection.
    return "unreachable"


def _resolve_db_mode(
    *,
    db_url: str | None,
    server_reachable: bool | None,
    provider_available: bool,
    want_embedded: bool,
    no_embedded: bool,
    existing_embedded: bool | None = None,
) -> tuple[str, str | None]:
    """Pure decision function for `celerp init`'s database mode.

    Returns (mode, error_kind) where mode is "external" | "embedded" | "error".
    Kept side-effect-free so the whole branch table can be unit-tested without a
    database.

    Args:
      db_url: explicit --db-url; taken at face value (external), never probed.
      server_reachable: result of probing the default host; None when not probed.
      provider_available: whether the embedded backend can run here.
      want_embedded/no_embedded: the --embedded / --no-embedded overrides.
      existing_embedded: the mode of an already-initialized instance (True/False),
        or None for a fresh install. On `--force` this PRESERVES the instance's
        mode so re-initializing an external install (e.g. a droplet) never flips
        to embedded just because its server was momentarily unreachable.

    Precedence: conflicting-flags → db_url → --embedded → --no-embedded →
    existing config's mode → probe.
    """
    if want_embedded and no_embedded:
        return ("error", "conflicting_flags")
    if db_url and want_embedded:
        # An explicit external URL and "force embedded" are contradictory.
        return ("error", "db_url_with_embedded")
    if db_url:
        return ("external", None)
    if want_embedded:
        return ("embedded", None) if provider_available else ("error", "no_provider")
    if no_embedded:
        # User requires external. Reachable → use it; otherwise fail with external
        # guidance rather than silently starting a cluster.
        return ("external", None) if server_reachable else ("error", "no_server_external_only")
    if existing_embedded is True:
        return ("embedded", None) if provider_available else ("error", "no_provider")
    if existing_embedded is False:
        return ("external", None)
    # Fresh install, no overrides: an existing server always wins.
    if server_reachable:
        return ("external", None)
    if provider_available:
        return ("embedded", None)
    return ("error", "no_server_no_provider")


def _emit_db_error(kind: str, db_url: str) -> None:
    """Print actionable guidance for a resolve/connect failure (stderr)."""
    if kind == "conflicting_flags":
        click.echo("  ✗ --embedded and --no-embedded are mutually exclusive.", err=True)
        return
    if kind == "db_url_with_embedded":
        click.echo(
            "  ✗ --embedded cannot be combined with --db-url (which selects an "
            "external server).",
            err=True,
        )
        return
    if kind == "no_provider":
        click.echo(
            f"  ✗ Embedded PostgreSQL is not available on this platform ({sys.platform}).",
            err=True,
        )
        click.echo(
            "\nInstall PostgreSQL and re-run, or use a supported platform "
            "(Linux x86_64/arm64 with glibc or musl, or Windows x64).",
            err=True,
        )
        if sys.platform == "darwin":
            click.echo(
                "On macOS 26 or newer: pip install celerp-postgres, then re-run "
                "`celerp init` for the bundled database.",
                err=True,
            )
        return
    # no_server_external_only / no_server_no_provider: nothing was listening.
    click.echo(f"  ✗ No PostgreSQL server found at {db_url}.", err=True)
    if kind == "no_server_external_only":
        click.echo(
            "\n--no-embedded was set, so no cluster was started. Install and start "
            "PostgreSQL, then re-run `celerp init`.",
            err=True,
        )
        return
    # no_server_no_provider — embedded unavailable AND no server.
    click.echo(
        "\nInstall PostgreSQL and start it, then re-run `celerp init`, or use a "
        "supported platform (Linux x86_64/arm64 with glibc or musl, or Windows x64) "
        "to get the bundled database automatically.",
        err=True,
    )
    if sys.platform.startswith("linux"):
        click.echo("  e.g.  sudo apt install postgresql && sudo service postgresql start", err=True)


def ensure_database(cfg: dict, *, own: bool = False) -> None:
    """Boot the embedded cluster for a DB-touching command, if this install uses
    one, and refresh the connection URI in `cfg` in place. `own` makes this
    process stop the cluster at exit (see embedded_pg.ensure_cluster).

    No-op for external mode (never imports the embedded provider, so external
    installs — including every droplet — are unaffected). Idempotent: safe to
    call from every command that reads cfg["database"]["url"].
    """
    db = cfg.get("database", {})
    if not db.get("embedded"):
        return
    from celerp import embedded_pg

    config_dir = _config_path().parent
    # Re-derive the URI on every boot rather than trusting the stored one: the
    # unix-socket path lives under a runtime dir that a reboot can relocate.
    db["url"] = embedded_pg.ensure_cluster(config_dir, own=own)
    bd = embedded_pg.bin_dir()
    if bd:
        cfg.setdefault("backup", {}).setdefault("pg_bin_dir", bd)


def _run_upgrade_with_auto_stamp(alembic_cfg, engine_url: str) -> None:
    """Run alembic upgrade head, auto-stamping past any already-applied revisions.

    When a migration's DDL was applied outside Alembic (e.g. dev testing before
    a formal release), the DB has the tables but the version stamp is behind.
    Alembic will crash with DuplicateTable/DuplicateObject on the re-apply.
    This helper catches those errors per-revision, stamps past them, and retries
    until all pending migrations are applied cleanly.
    """
    from alembic import command
    from alembic.util.exc import CommandError
    import sqlalchemy as _sa2

    _MAX_RETRIES = 50  # safety cap - one per migration at most
    for _ in range(_MAX_RETRIES):
        try:
            command.upgrade(alembic_cfg, "head")
            return  # success
        except Exception as exc:
            msg = str(exc)
            # Detect DDL-already-exists errors from Postgres.
            _ALREADY_EXISTS = (
                "DuplicateTable",
                "DuplicateObject",
                "DuplicateColumn",
                "already exists",
            )
            if not any(tok in msg for tok in _ALREADY_EXISTS):
                raise  # unrelated error - propagate

            # Find the revision currently stamped and advance it by one so the
            # offending migration is skipped on the next attempt.
            with _db_engine(engine_url, pool_pre_ping=True) as engine2:
                with engine2.connect() as conn:
                    current = conn.execute(
                        _sa2.text("SELECT version_num FROM alembic_version")
                    ).scalar()
                from alembic.script import ScriptDirectory
                script = ScriptDirectory.from_config(alembic_cfg)
                # Walk revisions from base to head; find the one after current.
                revs = list(reversed(list(script.walk_revisions())))
                next_rev = None
                found = current is None  # if no stamp, first revision is the one
                for rev in revs:
                    if found:
                        next_rev = rev.revision
                        break
                    if rev.revision == current:
                        found = True
                if next_rev:
                    click.echo(
                        f"  · Schema already contains changes from {next_rev}: "
                        f"stamping past it..."
                    )
                    command.stamp(alembic_cfg, next_rev)
                else:
                    raise RuntimeError(
                        f"Cannot auto-stamp past failed migration. Error: {msg}"
                    )

    raise RuntimeError("Migration auto-stamp loop exceeded safety cap.")


def _apply_migrations(db_url: str) -> None:
    """Run alembic upgrade head programmatically; raises on failure.

    Detects a corrupted alembic_version stamp (stamp head without actually running
    migrations) by cross-checking recorded revision against actual DB columns.
    Repairs by re-stamping to the last known-good revision before upgrading.
    Callable outside the CLI (the backup restore reconciles the restored schema
    through this same path); ``_run_migrations`` wraps it with CLI exit semantics.
    """
    import os as _os
    _os.environ["DATABASE_URL"] = db_url

    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    import sqlalchemy as _sa
    from celerp.alembic_config import build_alembic_config as _build_alembic_config

    alembic_cfg = _build_alembic_config()

    # --- Repair the alembic stamp from the live schema before upgrading.
    #
    # Dev DBs are built by Base.metadata.create_all() from whatever models
    # are checked out, so the stamp can be missing, behind, or ahead of
    # the real schema. "Ahead" is the dangerous one: a stamp at head with
    # a migration's DDL absent skips that migration forever and only
    # surfaces as a runtime UndefinedColumn error. The walker
    # (celerp.migrations._auto_stamp) introspects the live schema and
    # returns the newest revision whose DDL is actually present; we stamp
    # there — forward or back — and let alembic upgrade apply the rest.
    # False negatives are safe: the re-applied revision fails with
    # DuplicateColumn, which _run_upgrade_with_auto_stamp catches.
    #
    # All of it runs in the database's mutating scope: a database a newer Celerp
    # already opened is refused before anything below can restamp it back to
    # this copy's head; otherwise this copy is recorded as having opened it
    # before anything below changes it.
    from celerp.migrations.compatibility import mutating_scope
    sync_url = _sync_url(db_url)
    with mutating_scope(sync_url) as held:
        with held.engine(pool_pre_ping=True) as engine:
            inspector = _sa.inspect(engine)
            existing_tables = set(inspector.get_table_names())
            if "alembic_version" in existing_tables:
                with engine.connect() as conn:
                    stamped = conn.execute(_sa.text("SELECT version_num FROM alembic_version")).scalar()
            else:
                stamped = None

            if "companies" in existing_tables:
                from celerp.migrations._auto_stamp import (
                    extract_signatures, find_safe_stamp, load_kernel_metadata,
                )
                from pathlib import Path as _Path
                script = ScriptDirectory.from_config(alembic_cfg)
                versions_dir = _Path(alembic_cfg.get_main_option("script_location")) / "versions"
                sigs_by_rev: dict = {}
                for mig in versions_dir.glob("*.py"):
                    if mig.name == "__init__.py":
                        continue
                    sigs = extract_signatures(mig)
                    if sigs:
                        sigs_by_rev[sigs[0].rev] = sigs
                # walk_revisions() yields head→base, the order the walker
                # requires.
                revs_newest_first = list(script.walk_revisions())
                safe = find_safe_stamp(
                    revs_newest_first, sigs_by_rev, inspector,
                    expected_metadata=load_kernel_metadata(),
                )
                if safe != "base" and safe != stamped:
                    click.echo(
                        f"  · Live schema matches revision {safe}: "
                        f"restamping (was {stamped or 'unstamped'})..."
                    )
                    command.stamp(alembic_cfg, safe, purge=True)
        _run_upgrade_with_auto_stamp(alembic_cfg, engine_url=sync_url)


def _run_migrations(db_url: str) -> None:
    """CLI entrypoint: apply migrations, exiting non-zero with a readable message."""
    from celerp.migrations.compatibility import IncompatibleDatabase
    try:
        _apply_migrations(db_url)
    except IncompatibleDatabase as e:
        click.echo(f"  ✗ {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"  ✗ Migration failed: {e}", err=True)
        sys.exit(1)


def _stamped_revision(db_url: str) -> str | None:
    """The alembic revision the database is stamped at, or None if unstamped.

    The stamp records what alembic believes; on a develop database built by
    create_all it can sit behind or ahead of the real schema, which is what
    `_apply_migrations` repairs. So this is for reporting what changed, never for
    deciding whether the schema is sound. It only reads, so `celerp status` can
    report on any database, a newer version's included, without opening it.
    """
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url(db_url), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


@contextmanager
def _migration_lock(db_url: str):
    """Hold this version's fence and the shared Postgres migration advisory
    lock for the wrapped block.

    Single source of the lock protocol (acquire, guaranteed release, engine
    disposal), so every path that migrates a database serializes the same way
    and cannot drift. Reused by `_migrate_to_head` and the restore reconcile.

    The lock exists because a service restart overlapping a manual start would
    otherwise have both processes migrating the same database: the loser
    re-applies DDL that already exists and is stamped past it by the
    duplicate-object handler, which reaches the right answer for the wrong
    reason. The second holder waits here and then finds nothing pending.
    """
    from sqlalchemy import text

    from celerp.db import _MIGRATION_LOCK_KEY

    # The mutating scope is entered before the migration lock, so a process still
    # waiting for the fence never holds the migration lock a fence holder (the
    # restore reconcile inside a running server) may be waiting for. Inside a
    # fence this process holds already, that fence is reused.
    with _db_engine(db_url, pool_pre_ping=True, isolation_level="AUTOCOMMIT") as engine, \
            engine.connect() as lock_conn:
        lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        try:
            yield
        finally:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )


def _migrate_to_head(db_url: str) -> bool:
    """Apply pending migrations, the grants, then the develop→release reconcile.

    Shared by `celerp migrate` and `celerp start` so the steps and their order
    exist once. The start path cannot drift from the explicit command. Held
    under the shared migration advisory lock (`_migration_lock`).

    Reports the stamp it moved, and says nothing when it moved nothing, so a
    routine start is as quiet as it was and a start that changed the schema
    cannot be mistaken for one that did not. Returns False when an unfinished
    System Recovery left the database to the server, so callers do not report it ready.
    """
    from celerp.services.backup_import import recovery_incomplete
    if recovery_incomplete():
        # The database is mid System Recovery: the server's startup recovery replaces
        # it and brings its schema to head, so a migration here would only run on
        # the half-replaced one.
        click.echo("  System Recovery unfinished; the database is migrated when it completes.")
        return False
    from celerp.migrations.compatibility import IncompatibleDatabase
    before = after = None
    try:
        with _migration_lock(db_url):
            before = _stamped_revision(db_url)
            _run_migrations(db_url)
            _post_migration_grants(db_url)
            _reconcile_after_migrate(db_url)
            # Read inside the lock: outside it, a process queued behind this one
            # could move the stamp further and this would report a transition that
            # never happened here.
            after = _stamped_revision(db_url)
    except IncompatibleDatabase as e:
        click.echo(f"  ✗ {e}", err=True)
        sys.exit(1)
    if after != before:
        click.echo(f"  ✓ Database migrated: {before or 'base'} -> {after}")
    return True


def _reconcile_after_migrate(db_url: str) -> None:
    """Replay data-backfill migrations the auto-stamp walker skips on a develop
    (create_all) database, so a develop→release in-place upgrade preserves data.

    Gated by an instance_meta marker so it runs once per version; the backfills
    are idempotent, so a redundant run would be a no-op anyway. The projection
    rebuild (the other half of the version-change reconcile) runs in the API
    lifespan, where module projection handlers are loaded.

    Each backfill replays in its own transaction; the version marker is written
    in a SEPARATE final transaction, and only when every backfill succeeded, so
    a backfill that fails is retried on the next start instead of being marked
    done for this version's lifetime. The marker write no longer shares a
    transaction with the replay, so its own failure cannot roll back committed
    backfills.
    """
    from celerp import __version__
    from celerp.migrations._data_reconcile import (
        BACKFILL_VERSION_KEY,
        get_meta,
        replay_data_backfills,
        set_meta,
    )

    with _db_engine(db_url, pool_pre_ping=True) as engine:
        with engine.begin() as conn:
            if get_meta(conn, BACKFILL_VERSION_KEY) == __version__:
                return  # already reconciled for this version
        replayed, failures = replay_data_backfills(engine)
        if failures:
            click.echo(
                f"  ! {len(failures)} data-backfill migration(s) failed to reconcile for "
                f"{__version__}; leaving the version marker unset so they retry next start: "
                f"{', '.join(failures)}"
            )
        else:
            with engine.begin() as conn:
                set_meta(conn, BACKFILL_VERSION_KEY, __version__)
        if replayed:
            click.echo(f"  · Reconciled {len(replayed)} data-backfill migration(s) for {__version__}")


def _wipe_attachment_dirs(purge_dirs: list) -> None:
    """Remove attached-file directories on --force so a re-init is truly fresh."""
    import shutil
    for _d in purge_dirs:
        if _d.exists():
            shutil.rmtree(_d, ignore_errors=True)
            click.echo(f"  ✓ Removed {_d.resolve()}")


def _init_embedded(cfg: dict, config_dir: "Path", *, force: bool, purge_dirs: list) -> None:
    """Provision the bundled PostgreSQL cluster and point cfg at it.

    No sudo, no ownership/grant dance: the app connects as the cluster superuser
    over a unix socket, so it owns every object it creates.
    """
    from celerp import embedded_pg

    if force:
        click.echo("Wiping embedded database for fresh init...")
        embedded_pg.wipe(config_dir)
        _wipe_attachment_dirs(purge_dirs)
        click.echo("  ✓ Database ready (fresh)")

    click.echo("Starting embedded PostgreSQL...")
    uri = embedded_pg.ensure_cluster(config_dir)
    cfg["database"]["url"] = uri
    cfg["database"]["embedded"] = True
    bd = embedded_pg.bin_dir()
    if bd:
        cfg.setdefault("backup", {})["pg_bin_dir"] = bd
    click.echo(f"  ✓ Using embedded PostgreSQL at {embedded_pg.pgdata_dir(config_dir)}")


def _init_database(db_url_val: str) -> None:
    """Bring the database init points at up to date: table ownership, then
    migrations, inside the database's mutating scope: an existing database is admitted
    before either changes it, and before init writes the config; an incompatible
    one exits having changed nothing."""
    from celerp.migrations.compatibility import IncompatibleDatabase, mutating_scope
    try:
        with mutating_scope(_sync_url(db_url_val)):
            _init_admitted_database(db_url_val)
    except IncompatibleDatabase as e:
        click.echo(f"  ✗ {e}", err=True)
        sys.exit(1)


def _init_admitted_database(db_url_val: str) -> None:
    # Fix table ownership before migrations (covers tables created by postgres superuser)
    if _needs_ownership_fix(db_url_val):
        click.echo("Fixing table ownership...")
        err = _fix_ownership(db_url_val)
        if err:
            parts = _parse_db_url(db_url_val)
            user = parts["user"] if parts else "celerp"
            dbname = parts["dbname"] if parts else "celerp"
            click.echo(f"  ✗ Could not fix ownership: {err}", err=True)
            click.echo(
                f"\nFix manually:\n"
                f"  sudo -u postgres psql -d {dbname} -c "
                f"\"REASSIGN OWNED BY postgres TO {user};\"",
                err=True,
            )
            sys.exit(1)
        click.echo("  ✓ Table ownership fixed")

    # Run migrations. The grants are part of that path, not a step here:
    # sequences and tables created by migrations are not covered by the ALTER
    # DEFAULT PRIVILEGES set by _fix_ownership, so they are re-granted after.
    click.echo("Running migrations...")
    if _migrate_to_head(db_url_val):
        click.echo("  ✓ Database ready")


def _init_external(cfg: dict, *, force: bool, db_url: str | None, purge_dirs: list) -> None:
    """Connect to (and, as root, provision) an external PostgreSQL server.

    This is the pre-existing path, preserved verbatim for the droplet/self-hosted
    contract: explicit --db-url, root auto-provisioning via `sudo -u postgres`,
    and the same connect-or-guidance flow for non-root.
    """
    if force:
        click.echo("Wiping database for fresh init...")
        db_url_for_wipe = db_url or _DEFAULT_DB_URL
        try:
            _provision_db(db_url_for_wipe, drop_existing=True)
        except RuntimeError as e:
            click.echo(f"  ✗ DB wipe failed: {e}", err=True)
            click.echo("\nEnsure PostgreSQL is running and you have sudo access.", err=True)
            sys.exit(1)
        click.echo("  ✓ Database ready (fresh)")
        _wipe_attachment_dirs(purge_dirs)
        return

    click.echo("Connecting to database...")
    err = _test_db(cfg["database"]["url"])
    if not err:
        click.echo("  ✓ Database connection OK")
        return
    if _is_root():
        click.echo("  · Could not connect, attempting to provision database...")
        from celerp.migrations.compatibility import IncompatibleDatabase
        try:
            _provision_db(cfg["database"]["url"])
        except (IncompatibleDatabase, ExistingDatabaseUnreachable) as e:
            click.echo(f"  ✗ {e}", err=True)
            sys.exit(1)
        except RuntimeError as e:
            click.echo(f"  ✗ Provisioning failed: {e}", err=True)
            click.echo("\nEnsure PostgreSQL is installed and running, then retry with sudo.", err=True)
            sys.exit(1)
        err = _test_db(cfg["database"]["url"])
        if err:
            click.echo(f"  ✗ Still could not connect after provisioning: {err}", err=True)
            sys.exit(1)
        click.echo("  ✓ Database connection OK")
        return
    # Non-root and the server rejected us. On POSIX, sudo can provision; on
    # Windows there is no `sudo -u postgres`, so only suggest it where it works.
    click.echo(f"  ✗ Could not connect: {err}", err=True)
    import shutil
    real_bin = shutil.which("celerp") or "celerp"
    if hasattr(os, "getuid"):
        click.echo(
            f"\nRe-run with sudo to have Celerp create the database automatically:\n"
            f"  sudo {real_bin} init\n"
            "\nOr create it manually:\n"
            "  sudo -u postgres psql -c \"CREATE USER celerp WITH PASSWORD 'celerp';\"\n"
            "  sudo -u postgres psql -c \"CREATE DATABASE celerp OWNER celerp;\"",
            err=True,
        )
    else:
        click.echo(
            "\nCreate the database and role on your PostgreSQL server, then re-run "
            "`celerp init --db-url ...`:\n"
            "  CREATE USER celerp WITH PASSWORD 'celerp';\n"
            "  CREATE DATABASE celerp OWNER celerp;",
            err=True,
        )
    sys.exit(1)


# ── Commands ──────────────────────────────────────────────────────────────────

def _utf8_output() -> None:
    """Write output as UTF-8. On Windows, output sent to a file or a service log
    otherwise uses the legacy code page, and printing a check mark fails."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _exec_celerp(args: list[str], env: dict) -> None:
    """Replace this process with `python -m celerp <args>` under `env`. POSIX
    replaces it in place (same PID for a service manager); Windows has no
    in-place exec that keeps the PID, so this process waits on the new one and
    passes its exit code through, leaving Ctrl+C to the new one."""
    sys.stdout.flush()
    sys.stderr.flush()
    argv = [sys.executable, "-m", "celerp", *args]
    if os.name == "nt":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        sys.exit(subprocess.call(argv, env=env))
    os.execve(sys.executable, argv, env)


def _run_active_release() -> None:
    """Run the command on the release a self-update switched to, when this is
    the installed one (`celerp.runtime`)."""
    from celerp import runtime

    if runtime.PKG_ROOT_ENV in os.environ:
        return
    root = runtime.active()
    if root is not None:
        _exec_celerp(sys.argv[1:], runtime.release_env(root))


@click.group()
def main() -> None:
    """Celerp ERP — self-hosted business management."""
    _utf8_output()
    _run_active_release()


@main.command()
@click.option("--db-url", default=None, help="PostgreSQL connection URL.")
@click.option("--api-port", default=None, type=int, help="API server port (default 8000).")
@click.option("--ui-port", default=None, type=int, help="UI server port (default 8080).")
@click.option("--cloud-token", default=None, help="Celerp Connect token (optional).")
@click.option("--force", is_flag=True, help="Reconfigure: WIPES the database and all attached files.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip the --force wipe confirmation (non-interactive use).")
@click.option(
    "--no-start", "no_start", is_flag=True,
    help="Provision the database, run migrations, and write config, then exit "
         "WITHOUT launching the servers. For service-managed/headless installs "
         "where a process manager (e.g. systemd) runs `celerp start`.",
)
@click.option(
    "--embedded", "want_embedded", is_flag=True,
    help="Force the bundled PostgreSQL even if a server is reachable. "
         "Cannot be combined with --db-url.",
)
@click.option(
    "--no-embedded", "no_embedded", is_flag=True,
    help="Never fall back to the bundled PostgreSQL; require an external server.",
)
def init(db_url, api_port, ui_port, cloud_token, force, assume_yes, no_start, want_embedded, no_embedded):
    """Initialize Celerp: write config and run database migrations.

    With no --db-url, init uses an existing PostgreSQL server if one is running,
    and otherwise boots a self-contained bundled cluster (no sudo, no system
    service). An existing server always wins; pass --embedded / --no-embedded to
    override the auto-detection.
    """
    config_path = _config_path()

    if config_path.exists() and not force:
        click.echo(f"Already initialized. Config: {config_path}")
        click.echo("Run `celerp migrate` to apply updates, or `celerp init --force` to reconfigure.")
        return

    from celerp import embedded_pg

    # ── Resolve external vs embedded ──────────────────────────────────────────
    # On --force, config_path exists: preserve the initialized instance's mode so
    # re-init never flips an external install (e.g. a droplet) to embedded.
    existing_cfg = _read_config() if config_path.exists() else {}
    existing_embedded = (
        bool(existing_cfg.get("database", {}).get("embedded")) if existing_cfg else None
    )
    provider_available = embedded_pg.is_available()
    # Probe the default host only when nothing else determines the mode — an
    # explicit --db-url/--embedded, or a known existing mode, needs no probe.
    server_reachable: bool | None = None
    need_probe = not db_url and not want_embedded and (no_embedded or existing_embedded is None)
    if need_probe:
        server_reachable = _classify_probe(_test_db(_DEFAULT_DB_URL)) in ("ok", "needs_provision")
    mode, error_kind = _resolve_db_mode(
        db_url=db_url,
        server_reachable=server_reachable,
        provider_available=provider_available,
        want_embedded=want_embedded,
        no_embedded=no_embedded,
        existing_embedded=existing_embedded,
    )
    if mode == "error":
        _emit_db_error(error_kind, db_url or _DEFAULT_DB_URL)
        sys.exit(1)

    config_dir = config_path.parent

    if force:
        # --force is destructive: it wipes the database AND attached files.
        # init can run under sudo, so warn (with full paths) and confirm first.
        from celerp.config import settings
        _purge_dirs = [
            settings.data_dir / "static" / "attachments",
            settings.data_dir / "ai_uploads",
        ]
        click.echo("⚠  `init --force` permanently WIPES this instance:")
        if mode == "embedded":
            click.echo(f"     - database: embedded cluster at {embedded_pg.pgdata_dir(config_dir)}")
        else:
            click.echo(f"     - database: {db_url or _DEFAULT_DB_URL}")
        for _d in _purge_dirs:
            click.echo(f"     - files:    {_d.resolve()}")
        click.echo("   Make sure you have a backup.")
        if not assume_yes and not click.confirm("Continue?", default=False):
            click.echo("Aborted.")
            return
        _stop_servers()

    enabled_modules: list[str] = []
    cfg = {
        "database": {"url": db_url or _DEFAULT_DB_URL},
        "auth": {"jwt_secret": secrets.token_hex(32)},
        "server": {
            "api_port": api_port or _DEFAULT_API_PORT,
            "ui_port": ui_port or _DEFAULT_UI_PORT,
        },
        "cloud": {"token": cloud_token or ""},
        "modules": {"enabled": enabled_modules},
    }

    if mode == "embedded":
        _init_embedded(cfg, config_dir, force=force, purge_dirs=_purge_dirs if force else [])
    else:
        _init_external(cfg, force=force, db_url=db_url, purge_dirs=_purge_dirs if force else [])

    db_url_val = cfg["database"]["url"]
    _init_database(db_url_val)

    # Headless installs (a process manager runs `start`) are network-exposed, so the
    # first-admin page shouldn't be claimable by whoever reaches it first. Mint a
    # one-time setup code the operator must present; keep the API on loopback too.
    setup_code = None
    if no_start:
        import hashlib
        setup_code = secrets.token_hex(16)
        cfg["auth"]["setup_code_hash"] = hashlib.sha256(setup_code.encode()).hexdigest()
        cfg["server"]["headless"] = True

    # Write config
    _write_config(cfg)

    if setup_code:
        from celerp.config import config_path as _cfg_path
        code_file = _cfg_path().parent / "setup-code"
        code_file.write_text(setup_code + "\n")
        code_file.chmod(0o600)

    api_port_val = cfg["server"]["api_port"]
    ui_port_val = cfg["server"]["ui_port"]
    click.echo(f"""
✓ Celerp initialized
  Config: {config_path}
  App:    http://localhost:{ui_port_val}  (open this link in your browser)
  API:    internal service, port {api_port_val}
  Modules: none - choose an industry preset in the setup wizard
""")

    from celerp.config import settings as _settings
    from celerp.gateway.state import build_handoff_url as _handoff
    if _settings.star_cta_enabled:
        click.echo(
            "New and independent - early stargazers are how other teams find us.\n"
            f"  Back us early: {_handoff('/github', medium='cli')}\n"
        )

    if no_start:
        if setup_code:
            code_file = config_path.parent / "setup-code"
            click.echo(
                f"\nSetup code: {setup_code}\n"
                f"  Enter it on the first-admin page to claim this instance.\n"
                f"  (also saved to {code_file})\n"
            )
        click.echo("Setup complete. Start the servers with: celerp start")
        return
    _start(cfg)


def _wait_ready(api: tuple, ui: tuple, timeout: float = 180.0) -> bool:
    """Announce readiness in dependency order: the API when its port accepts
    connections, then one 'Celerp ready' line with the UI URL once BOTH ports
    do. The UI URL is the only one printed at all, because it is the only
    address a user should visit. Returns False early when either process dies (the
    supervisor loop reports the crash); after `timeout` prints a still-starting
    note and returns False rather than blocking forever on a very slow machine.
    True when both are accepting connections."""
    import socket

    def _accepting(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            return False

    api_proc, api_port = api
    ui_proc, ui_port = ui
    api_ready = ui_ready = False
    deadline = time.time() + timeout
    while not (api_ready and ui_ready) and time.time() < deadline:
        if api_proc.poll() is not None or ui_proc.poll() is not None:
            return False  # crashed; supervisor loop reports it
        if not api_ready and _accepting(api_port):
            api_ready = True
            click.echo(f"  ✓ API ready (internal service, port {api_port})")
        if api_ready and not ui_ready and _accepting(ui_port):
            ui_ready = True
            click.echo(
                f"  ✓ Celerp ready → http://localhost:{ui_port}"
                "  (open this link in your browser)"
            )
        if not (api_ready and ui_ready):
            time.sleep(0.3)
    if not api_ready:
        click.echo(f"  … the API is still starting on port {api_port}, hang tight.")
    if not ui_ready:
        click.echo(f"  … the UI is still starting on port {ui_port}, hang tight.")
    return api_ready and ui_ready


def _spawn_server(app: str, host: str, env: dict, port: int) -> subprocess.Popen:
    from celerp import runtime

    child_env = dict(env)
    child_env[runtime.SUPERVISOR_PIPE_ENV] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app, "--host", host, "--port", str(port),
         "--timeout-graceful-shutdown", "3"],
        env=child_env,
        stdin=subprocess.PIPE,
    )


def _server_spawners(cfg: dict) -> tuple:
    """(spawn_api, spawn_ui), each called as spawn(env, port)."""
    from functools import partial

    # Headless installs expose the UI publicly but the UI reaches the API over
    # localhost, so keep the API off the public interface there.
    api_host = "127.0.0.1" if cfg.get("server", {}).get("headless") else "0.0.0.0"
    return (partial(_spawn_server, "celerp.main:app", api_host),
            partial(_spawn_server, "ui.app:app", "0.0.0.0"))


def _verification_spawners() -> tuple:
    """API/UI launchers used only before an update is committed."""
    from functools import partial

    return (partial(_spawn_server, "celerp.main:app", "127.0.0.1"),
            partial(_spawn_server, "ui.app:app", "127.0.0.1"))


def _update_steps(cfg: dict):
    """The self-update steps for this supervisor (`celerp.services.update`)."""
    from celerp.services import update

    spawn_api, spawn_ui = _verification_spawners()
    return update.SupervisorSteps(cfg, lambda root: _config_to_env(cfg, root),
                                  spawn_api=spawn_api, spawn_ui=spawn_ui, wait_ready=_wait_ready)


def _hold_update_lock(what: str):
    """The update lock (`update.lock_path`), or exit: one `celerp start` or
    `celerp upgrade` per install at a time. Returns its release function."""
    from celerp import config_store
    from celerp.services import update

    release = config_store.hold_lock(str(update.lock_path()), config_store._LOCK_STALE_S + 5)
    if release is None:
        click.echo(f"Cannot {what}: Celerp is already running or being upgraded for this "
                   f"configuration ({update.config_dir()}).", err=True)
        sys.exit(1)
    return release


def _hand_over(steps, release_lock) -> None:
    """Replace this supervisor with one running the release just switched to.

    Called with the API and UI stopped; the embedded cluster is stopped here, so
    the new supervisor starts it with its own binaries and owns its shutdown.
    """
    from celerp import runtime

    steps.stop_cluster()
    release_lock()
    click.echo("Starting the new version...")
    _exec_celerp(["start"], runtime.base_env())


def _update_state_or_exit() -> dict:
    from celerp.services import update

    try:
        return update.read_state()
    except update.UpdateStateError as exc:
        click.echo(f"Update record unreadable: {exc}", err=True)
        sys.exit(1)


def _exit_if_rollback_failed(result: dict | None) -> None:
    from celerp.services import update

    if result and result["outcome"] == update.ROLLBACK_FAILED:
        click.echo(f"The update to {result['to']} failed ({update.reason_text(result['reason'])}) "
                   f"and the database could not be restored from {update.dump_path()}. Celerp "
                   "will not start until it is; starting again retries the restore.", err=True)
        sys.exit(1)


def _start(cfg: dict) -> None:
    """Launch API and UI servers and block until one exits or Ctrl+C.

    When the API exits with the restart sentinel present, both servers are
    respawned (to load newly enabled modules). A sentinel reading `update
    <version>` installs that version first (`celerp.services.update`). Any other
    exit is treated as a real error and terminates the supervisor.

    Migrations are applied first. Installing a new version and starting it is one
    act, so a start against a database the new code cannot read is not a state
    worth preserving: it serves an unknown subset of the ledger as errors and
    reads as a bug in the app rather than a schema behind the code. `migrate` is
    idempotent, so this costs nothing when there is nothing to apply, and a
    failure exits non-zero with the alembic error rather than starting anyway.
    Before that, an update a previous supervisor did not live to finish is
    finished or undone. The update lock is held for the supervisor's lifetime.
    """
    # Installed first, so a stop at any point before the servers exist (taking the
    # lock, starting the database, finishing an update, migrating) exits normally:
    # the lock is released below and the database this process started is stopped
    # at exit. `_supervise` replaces it once there are servers to end as well.
    def _stop(sig, frame):
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    release_lock = _hold_update_lock("start")
    try:
        ensure_database(cfg, own=True)
        _supervise(cfg, release_lock)
    finally:
        release_lock()


def _supervise(cfg: dict, release_lock) -> None:
    from celerp.config import config_path as _cfg_path
    from celerp.services import update

    def _sentinel() -> "Path":
        return _cfg_path().parent / ".restart_requested"

    # A request left by a supervisor that stopped before acting on it is stale.
    _sentinel().unlink(missing_ok=True)
    if _update_state_or_exit().get("in_progress"):
        _exit_if_rollback_failed(update.reconcile(_update_steps(cfg)))

    env = _config_to_env(cfg)
    _migrate_to_head(cfg["database"]["url"])

    spawn_api, spawn_ui = _server_spawners(cfg)
    api_port = cfg["server"]["api_port"]
    ui_port = cfg["server"]["ui_port"]

    api_proc = ui_proc = None

    # Replaces `_start`'s handler before the servers start, so a stop while they
    # are still starting also ends them.
    def _shutdown(sig, frame):
        click.echo("\nShutting down...")
        for proc in (api_proc, ui_proc):
            if proc is not None:
                update.stop_process(proc)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    click.echo("Starting Celerp...")
    click.echo(f"  API starting on port {api_port} ...")
    click.echo(f"  UI  starting on port {ui_port} ...")

    api_proc = spawn_api(env, api_port)
    ui_proc = spawn_ui(env, ui_port)

    # Readiness prints in dependency order (API first, then the single
    # user-facing URL) so the link never points at a UI whose API is still
    # importing modules.
    _wait_ready((api_proc, api_port), (ui_proc, ui_port))
    click.echo("Press Ctrl+C to stop.\n")

    while True:
        if api_proc.poll() is not None:
            sentinel = _sentinel()
            if sentinel.exists():
                request = sentinel.read_text(encoding="utf-8")
                sentinel.unlink()
                try:
                    target = update.requested_target(request)
                except update.UpdateError as exc:
                    click.echo(f"Ignoring update request: {exc}", err=True)
                    target = None
                update.stop_process(ui_proc)
                if target:
                    click.echo(f"Updating Celerp to {target}...")
                    steps = _update_steps(cfg)
                    result, children = update.run_update(target, steps)
                    if children:
                        api_proc, ui_proc = children
                        steps.stop_children(children)
                        _hand_over(steps, release_lock)
                    _exit_if_rollback_failed(result)
                    click.echo(f"Update not installed ({update.reason_text(result['reason'])}); "
                               "starting the current version.", err=True)
                else:
                    click.echo("Restarting API server (config changed)...")
                # Re-read config so newly enabled modules are picked up; keep the
                # database URL this supervisor is using (the embedded cluster's
                # may have been refreshed by an update attempt).
                fresh_cfg = _read_config() or cfg
                fresh_cfg["database"]["url"] = cfg["database"]["url"]
                env = _config_to_env(fresh_cfg)
                api_proc = spawn_api(env, api_port)
                # Restart UI too so its module nav slots reflect the new config
                ui_proc = spawn_ui(env, ui_port)
            else:
                click.echo(f"API server exited with code {api_proc.returncode}", err=True)
                update.stop_process(ui_proc)
                sys.exit(api_proc.returncode)
        if ui_proc.poll() is not None:
            click.echo(f"UI server exited with code {ui_proc.returncode}", err=True)
            update.stop_process(api_proc)
            sys.exit(ui_proc.returncode)
        time.sleep(0.5)


@main.command("reset-password")
@click.option("--email", prompt="User email", help="Email of the user account to reset.")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True, help="New password.")
def reset_password(email: str, password: str) -> None:
    """Reset a user's password directly via the database."""
    try:
        validate_password(password)
    except ValueError:
        click.echo(f"Error: password must be at least {MIN_PASSWORD_LENGTH} characters.", err=True)
        sys.exit(1)
    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)
    ensure_database(cfg)
    db_url = cfg["database"]["url"]
    try:
        from sqlalchemy import text
        from celerp.services.auth import hash_password
        with _db_engine(db_url) as engine, engine.begin() as conn:
            row = conn.execute(text("SELECT id, name FROM users WHERE email = :e"), {"e": email}).fetchone()
            if not row:
                click.echo(f"No user found with email: {email}", err=True)
                sys.exit(1)
            conn.execute(
                text("UPDATE users SET auth_hash = :h, reset_token = NULL, reset_token_expires = NULL WHERE id = :id"),
                {"h": hash_password(password), "id": row[0]},
            )
        click.echo(f"  \u2713 Password reset for {row[1]} ({email})")
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command()
def start():
    """Start the API and UI servers."""
    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)
    _start(cfg)


@main.command()
@click.option("--db-url", default=None, help="Database URL (overrides config; used by the packaged launcher).")
def migrate(db_url):
    """Apply pending database migrations, then the develop→release reconcile."""
    url = db_url
    if url is None:
        cfg = _read_config()
        if not cfg:
            click.echo("Not initialized. Run `celerp init` first.", err=True)
            sys.exit(1)
        ensure_database(cfg)
        url = cfg["database"]["url"]
    click.echo("Running migrations...")
    if _migrate_to_head(url):
        click.echo("  ✓ Done")


# `celerp compatibility` exit status when this copy must not open the database.
COMPATIBILITY_REFUSED_EXIT = 3


@main.command()
@click.option("--db-url", required=True, help="Database to check.")
def compatibility(db_url):
    """Say, without changing anything, whether this copy may open the database.

    Prints the decision as JSON; exits 0 when compatible and 3 when refused. The
    desktop launcher runs this before it changes anything.
    """
    from celerp.migrations.compatibility import check_url
    result = check_url(_sync_url(db_url))
    click.echo(result.to_json())
    if not result.ok:
        sys.exit(COMPATIBILITY_REFUSED_EXIT)


@main.command()
def status():
    """Show configuration and connectivity status."""
    config_path = _config_path()
    cfg = _read_config()

    click.echo(f"Config:  {config_path} {'✓' if config_path.exists() else '✗ (missing)'}")
    if not cfg:
        click.echo("Run `celerp init` to initialize.")
        return

    is_embedded = cfg.get("database", {}).get("embedded")
    ensure_database(cfg)
    db_url = cfg["database"]["url"]
    # Mask password in display
    import re
    display_url = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", db_url)
    prefix = "embedded (bundled PostgreSQL) — " if is_embedded else ""
    click.echo(f"Database: {prefix}{display_url}")

    err = _test_db(db_url)
    click.echo(f"  DB connection: {'✓ OK' if not err else f'✗ {err}'}")

    if not err:
        # Check migration state
        try:
            from alembic.script import ScriptDirectory
            from celerp.alembic_config import build_alembic_config as _build_alembic_config

            script = ScriptDirectory.from_config(_build_alembic_config())
            head = script.get_current_head()
            current = _stamped_revision(db_url)

            if current == head:
                click.echo(f"  Migrations: ✓ up to date ({current})")
            else:
                click.echo(f"  Migrations: ✗ behind (current: {current}, head: {head})")
                click.echo("  Run `celerp migrate` to update.")
        except Exception as e:
            click.echo(f"  Migrations: could not check ({e})")

    api_port = cfg["server"]["api_port"]
    ui_port = cfg["server"]["ui_port"]
    click.echo(f"API port: {api_port}")
    click.echo(f"UI port:  {ui_port}")
    cloud_token = cfg["cloud"]["token"]
    click.echo(f"Cloud:    {'connected (token set)' if cloud_token else 'not connected'}")


@main.command()
def demo():
    """Seed the database with demo data."""
    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)
    ensure_database(cfg)
    env = _config_to_env(cfg)
    pkg_root = Path(__file__).parent.parent
    script = pkg_root / "scripts" / "seed_demo.py"
    if not script.exists():
        click.echo(f"Demo script not found at {script}", err=True)
        sys.exit(1)
    result = subprocess.run([sys.executable, str(script)], env=env)
    sys.exit(result.returncode)


@main.command()
def upgrade():
    """Upgrade Celerp to the latest version and run migrations.

    Same steps as the in-app update (backup first, undone on failure), for use
    while Celerp is stopped.
    """
    from celerp import runtime
    from celerp.services import update

    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)
    if update.get_json(f"{runtime.api_url(cfg['server']['api_port'])}/health") is not None:
        click.echo("Celerp is running. Stop it first, then run `celerp upgrade` again.", err=True)
        sys.exit(1)
    release_lock = _hold_update_lock("upgrade")
    try:
        _upgrade(cfg)
    finally:
        release_lock()


def _upgrade(cfg: dict) -> None:
    from celerp.services import update

    _update_state_or_exit()
    if update.update_in_progress():
        click.echo("An update is unfinished. Run `celerp start`, which completes or undoes it.", err=True)
        sys.exit(1)
    reasons = {
        "pip_missing": "pip is not available to this Python",
        "pip_old": "pip 22.2 or newer is needed (python -m pip install --upgrade pip)",
        "not_writable": "this user cannot write to the install; run as the user that owns it",
    }
    blocked = [reasons[b] for b in update.self_update_blockers() if b in update.PIP_BLOCKERS]
    if blocked:
        click.echo(f"Cannot upgrade: {'; '.join(blocked)}.", err=True)
        sys.exit(1)
    click.echo("Checking for a newer version...")
    try:
        target = update.available_update()
    except update.UpdateError as exc:
        click.echo(f"Could not check for updates: {exc}.", err=True)
        sys.exit(1)
    if not target:
        click.echo(f"No newer version found (installed: {update.installed_version()}).")
        return
    ensure_database(cfg)
    click.echo(f"Upgrading Celerp {update.installed_version()} -> {target}...")
    steps = _update_steps(cfg)
    result, children = update.run_update(target, steps)
    steps.stop_children(children)
    steps.stop_cluster()  # the next start runs it with the new release's binaries
    _exit_if_rollback_failed(result)
    if not result["ok"]:
        click.echo(f"Upgrade not installed: {update.reason_text(result['reason'])}.", err=True)
        sys.exit(1)
    click.echo(f"\u2713 Upgraded to {target}. Start Celerp with `celerp start`.")


@main.group()
def module() -> None:
    """Manage Celerp modules."""



async def _enable_for_every_company(db_url: str, names: list[str]) -> int:
    """Turn *names* on for every company and recompute the load set; the number of companies.

    Refused, with nothing changed, for a name that is not an installed module."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from celerp.models.company import Company
    from celerp.modules.registry import commit_with_load_set, enable_for_company, hold_module_state, is_installed
    from celerp.services.company_lock import locked_company

    engine = create_async_engine(db_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await hold_module_state(session)
            missing = [n for n in names if not is_installed(n)]
            if missing:
                raise ValueError(f"Module '{missing[0]}' is not installed.")
            company_ids = (await session.scalars(select(Company.id).order_by(Company.id))).all()
            for company_id in company_ids:
                company = await locked_company(session, company_id)
                for name in names:
                    company.settings, _deps = enable_for_company(company.settings, name)
            await commit_with_load_set(session)
            return len(company_ids)
    finally:
        await engine.dispose()


@module.command("install")
@click.argument("names", nargs=-1, required=True)
def module_install(names: tuple[str, ...]) -> None:
    """Turn one or more installed modules on for every company (with the modules they need).

    Example: celerp module install celerp-crm
    """
    import asyncio

    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)

    ensure_database(cfg)
    from celerp.migrations.compatibility import mutating_scope
    db_url = cfg["database"]["url"]
    try:
        with mutating_scope(_sync_url(db_url)) as held, held.write_window():
            companies = asyncio.run(_enable_for_every_company(db_url, list(names)))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if not companies:
        click.echo("No company yet. Finish setup, then turn modules on in Settings > Modules.", err=True)
        sys.exit(1)
    click.echo(f"\u2713 Turned on for {companies} company(ies): {', '.join(names)}.")
    click.echo("Restart Celerp for changes to take effect: celerp start")


@module.command("list")
def module_list() -> None:
    """List installed, enabled, and available modules."""
    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)

    enabled: set[str] = set(cfg.get("modules", {}).get("enabled", []))
    _pkg_root = Path(__file__).parent.parent
    module_dir = _pkg_root / "default_modules"
    available = sorted(
        p.name for p in module_dir.iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    ) if module_dir.exists() else []

    click.echo(f"{'Module':<30} {'Status':<12}")
    click.echo("-" * 44)
    for name in available:
        status = "enabled" if name in enabled else "disabled"
        click.echo(f"{name:<30} {status:<12}")
    click.echo(f"\n{len(enabled)} enabled, {len(available) - len(enabled)} disabled, {len(available)} total available")

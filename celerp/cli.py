# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Celerp CLI — init, start, migrate, status, demo, upgrade."""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from celerp.config import config_path as _config_path, read_config as _read_config, write_config as _write_config, resolve_install_order as _resolve_install_order, set_enabled_modules as _set_enabled_modules

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


def _provision_db(db_url: str, drop_existing: bool = False) -> None:
    """Create Postgres user + database by shelling out to psql as the postgres OS user.

    Uses `sudo -u postgres psql` — works on any standard Postgres install regardless
    of pg_hba.conf configuration, since the postgres OS user always has superuser access.
    If drop_existing=True, drops and recreates the database (used by init --force).
    Raises RuntimeError on failure.
    """
    parts = _parse_db_url(db_url)
    if not parts:
        raise RuntimeError(f"Could not parse DB URL: {db_url}")

    user = parts["user"]
    password = parts["password"]
    dbname = parts["dbname"]

    def _psql(sql: str, db: str = "postgres") -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True,
            text=True,
        )

    # Create user if not exists, or reset password if it does
    r = _psql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{user}') THEN CREATE USER {user} WITH PASSWORD '{password}'; ELSE ALTER USER {user} WITH PASSWORD '{password}'; END IF; END $$;")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    click.echo(f"  ✓ Postgres user '{user}' ready")

    if drop_existing:
        # Terminate any active connections before dropping
        _psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{dbname}' AND pid <> pg_backend_pid();")
        r = _psql(f"DROP DATABASE IF EXISTS {dbname};")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        click.echo(f"  · Dropped database '{dbname}'")

    # Create database if not exists
    r = _psql(f"SELECT 1 FROM pg_database WHERE datname = '{dbname}';")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    if "1 row" not in r.stdout:
        r = _psql(f"CREATE DATABASE {dbname} OWNER {user};")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        click.echo(f"  ✓ Created database '{dbname}'")
    else:
        click.echo(f"  · Database '{dbname}' already exists")

    # Fix ownership for existing DBs (not freshly created). On a fresh DB with
    # OWNER=user, all new objects inherit the correct owner automatically.
    if not drop_existing:
        # Reassign user-created objects. Can't reassign postgres system objects,
        # so instead change ownership per-table.
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
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-d", dbname, "-c", fix_sql],
                capture_output=True, text=True,
            )
    # Ensure schema-level privileges are correct regardless of ownership history
    for grant_sql in [
        f"GRANT ALL PRIVILEGES ON DATABASE {dbname} TO {user};",
        f"GRANT ALL PRIVILEGES ON SCHEMA public TO {user};",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {user};",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {user};",
    ]:
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", dbname, "-c", grant_sql],
            capture_output=True, text=True,
        )


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


def _fix_ownership(db_url: str) -> str | None:
    """Reassign ownership of user-created objects to the app user and grant privileges.

    Uses sudo -u postgres psql (per-object ALTER, not REASSIGN which fails on system objects).
    Returns error string on failure, None on success.
    Call BEFORE migrations so ALTER TABLE etc. succeed as the app user.
    """
    parts = _parse_db_url(db_url)
    if not parts:
        return None
    user = parts["user"]
    dbname = parts["dbname"]
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
        r = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", dbname, "-v", "ON_ERROR_STOP=1", "-c", fix_sql],
            capture_output=True, text=True,
        )
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
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", dbname, "-c", sql],
            capture_output=True, text=True,
        )
    return None


def _sync_url(db_url: str) -> str:
    """The configured URL with any async/psycopg2 driver stripped.

    Alembic, the grant statements and the pg_dump helpers all need a plain
    synchronous URL, and the conversion was written out inline at each of them.
    One copy, so a third driver prefix is added here rather than in seven places.
    """
    return (
        db_url.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )


def _needs_ownership_fix(db_url: str) -> bool:
    """Check if any tables in the public schema are NOT owned by the app user."""
    parts = _parse_db_url(db_url)
    if not parts:
        return False
    user = parts["user"]
    sync_url = _sync_url(db_url)
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(sync_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
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
    won't be covered by ALTER DEFAULT PRIVILEGES set during provisioning.

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
    sync_url = _sync_url(db_url)
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(text(f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{user}";'))
                conn.execute(text(f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{user}";'))
        finally:
            engine.dispose()
    except Exception:
        # Best-effort: on the bundled cluster the app user already owns its objects.
        pass





def _config_to_env(cfg: dict) -> dict:
    """Convert config dict to env vars for subprocess launch."""
    env = os.environ.copy()
    env["DATABASE_URL"] = cfg["database"]["url"]
    env["JWT_SECRET"] = cfg["auth"]["jwt_secret"]
    if cfg["cloud"]["token"]:
        env["GATEWAY_TOKEN"] = cfg["cloud"]["token"]
    # A headless service install (`init --no-start`, then a process manager runs
    # `start`) reports its launch channel on activation, the same way the desktop
    # launcher does; a plain local run reports none. setdefault never clobbers an
    # explicit channel.
    if cfg.get("server", {}).get("headless"):
        env.setdefault("CELERP_MODE", "headless")
    # Module directories: a writable drop-in for imports FIRST (the importer
    # installs into MODULE_DIR.split(",")[0]), then the read-only bundled
    # default (core) and premium (opt-in add-ons) trees. Keeping the writable
    # dir separate means a sideload never lands in default_modules/.
    from celerp.modules.loader import writable_module_dir
    _pkg_root = Path(__file__).parent.parent
    _mod_dirs = [_pkg_root / "default_modules", _pkg_root / "premium_modules"]
    try:
        _mod_dirs.insert(0, writable_module_dir())
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
    _extra_paths = []
    for _mod_dir in _mod_dirs:
        if _mod_dir.exists():
            _extra_paths.extend(str(p) for p in _mod_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists())
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = ":".join(filter(None, [str(_pkg_root)] + _extra_paths + [existing_path]))
    return env



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
            "(Linux x86_64/arm64 — glibc or musl — or Windows x64).",
            err=True,
        )
        if sys.platform == "darwin":
            click.echo(
                "On macOS 26 or newer: pip install celerp-postgres — then re-run "
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
        "\nInstall PostgreSQL and start it, then re-run `celerp init` — or use a "
        "supported platform (Linux x86_64/arm64 — glibc or musl — or Windows x64) "
        "to get the bundled database automatically.",
        err=True,
    )
    if sys.platform.startswith("linux"):
        click.echo("  e.g.  sudo apt install postgresql && sudo service postgresql start", err=True)


def ensure_database(cfg: dict) -> None:
    """Boot the embedded cluster for a DB-touching command, if this install uses
    one, and refresh the connection URI in `cfg` in place.

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
    db["url"] = embedded_pg.ensure_cluster(config_dir)
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
            engine2 = _sa2.create_engine(engine_url, pool_pre_ping=True)
            try:
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
                        f"  · Schema already contains changes from {next_rev} "
                        f"— stamping past it..."
                    )
                    command.stamp(alembic_cfg, next_rev)
                else:
                    raise RuntimeError(
                        f"Cannot auto-stamp past failed migration. Error: {msg}"
                    )
            finally:
                engine2.dispose()

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
    sync_url = _sync_url(db_url)
    engine = _sa.create_engine(sync_url, pool_pre_ping=True)
    try:
        inspector = _sa.inspect(engine)
        existing_tables = set(inspector.get_table_names())
        if "alembic_version" in existing_tables:
            with engine.connect() as conn:
                stamped = conn.execute(_sa.text("SELECT version_num FROM alembic_version")).scalar()
        else:
            stamped = None

        if "companies" in existing_tables:
            from celerp.migrations._auto_stamp import (
                extract_signatures, find_safe_stamp,
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
            safe = find_safe_stamp(revs_newest_first, sigs_by_rev, inspector)
            if safe != "base" and safe != stamped:
                click.echo(
                    f"  · Live schema matches revision {safe} — "
                    f"restamping (was {stamped or 'unstamped'})..."
                )
                command.stamp(alembic_cfg, safe, purge=True)
    finally:
        engine.dispose()
    _run_upgrade_with_auto_stamp(alembic_cfg, engine_url=sync_url)


def _run_migrations(db_url: str) -> None:
    """CLI entrypoint: apply migrations, exiting non-zero with a readable message."""
    try:
        _apply_migrations(db_url)
    except Exception as e:
        click.echo(f"  ✗ Migration failed: {e}", err=True)
        sys.exit(1)


def _stamped_revision(db_url: str) -> str | None:
    """The alembic revision the database is stamped at, or None if unstamped.

    The stamp records what alembic believes; on a develop database built by
    create_all it can sit behind or ahead of the real schema, which is what
    `_apply_migrations` repairs. So this is for reporting what changed, never for
    deciding whether the schema is sound.
    """
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url(db_url), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def _migrate_to_head(db_url: str) -> None:
    """Apply pending migrations, the grants, then the develop→release reconcile.

    Shared by `celerp migrate` and `celerp start` so the steps and their order
    exist once. The start path cannot drift from the explicit command.

    Held under a Postgres advisory lock, because a service restart overlapping a
    manual start would otherwise have both processes migrating the same database:
    the loser re-applies DDL that already exists and is stamped past it by the
    duplicate-object handler, which reaches the right answer for the wrong
    reason. The second holder waits here and then finds nothing pending.

    Reports the stamp it moved, and says nothing when it moved nothing, so a
    routine start is as quiet as it was and a start that changed the schema
    cannot be mistaken for one that did not.
    """
    from sqlalchemy import create_engine, text

    from celerp.db import _MIGRATION_LOCK_KEY

    engine = create_engine(_sync_url(db_url), pool_pre_ping=True).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    before = after = None
    try:
        with engine.connect() as lock_conn:
            lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
            try:
                before = _stamped_revision(db_url)
                _run_migrations(db_url)
                _post_migration_grants(db_url)
                _reconcile_after_migrate(db_url)
                # Read inside the lock: outside it, a process queued behind this
                # one could move the stamp further and this would report a
                # transition that never happened here.
                after = _stamped_revision(db_url)
            finally:
                lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
                )
    finally:
        engine.dispose()
    if after != before:
        click.echo(f"  ✓ Database migrated: {before or 'base'} -> {after}")


def _reconcile_after_migrate(db_url: str) -> None:
    """Replay data-backfill migrations the auto-stamp walker skips on a develop
    (create_all) database, so a develop→release in-place upgrade preserves data.

    Gated by an instance_meta marker so it runs once per version; the backfills
    are idempotent, so a redundant run would be a no-op anyway. The projection
    rebuild (the other half of the version-change reconcile) runs in the API
    lifespan, where module projection handlers are loaded.
    """
    import sqlalchemy as _sa

    from celerp import __version__
    from celerp.migrations._data_reconcile import (
        BACKFILL_VERSION_KEY,
        get_meta,
        replay_data_backfills,
        set_meta,
    )

    sync_url = _sync_url(db_url)
    engine = _sa.create_engine(sync_url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            if get_meta(conn, BACKFILL_VERSION_KEY) == __version__:
                return  # already reconciled for this version
            replayed = replay_data_backfills(conn)
            set_meta(conn, BACKFILL_VERSION_KEY, __version__)
        if replayed:
            click.echo(f"  · Reconciled {len(replayed)} data-backfill migration(s) for {__version__}")
    finally:
        engine.dispose()


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
        click.echo("  · Could not connect — attempting to provision database...")
        try:
            _provision_db(cfg["database"]["url"])
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

@click.group()
def main() -> None:
    """Celerp ERP — self-hosted business management."""


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

    # Fix table ownership before migrations (covers tables created by postgres superuser)
    db_url_val = cfg["database"]["url"]
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
    # DEFAULT PRIVILEGES set during provisioning, so they are re-granted after.
    click.echo("Running migrations...")
    _migrate_to_head(db_url_val)
    click.echo("  ✓ Database ready")

    # Headless installs (a process manager runs `start`) are network-exposed, so the
    # first-admin page shouldn't be claimable by whoever reaches it first. Mint a
    # one-time setup code the operator must present; keep the API on loopback too.
    setup_code = None
    if no_start:
        import hashlib
        setup_code = secrets.token_hex(4)
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


def _wait_ready(api: tuple, ui: tuple, timeout: float = 180.0) -> None:
    """Announce readiness in dependency order: the API when its port accepts
    connections, then one 'Celerp ready' line with the UI URL once BOTH ports
    do. The UI port opens before the API has finished registering modules, so
    a UI URL printed on its own would send users to a page of errors; and the
    UI URL is the only one printed at all, because it is the only address a
    user should visit. Returns early when either process dies (the supervisor
    loop reports the crash); after `timeout` prints a still-starting note
    rather than blocking forever on a very slow machine."""
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
            return  # crashed; supervisor loop reports it
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


def _start(cfg: dict) -> None:
    """Launch API and UI servers and block until one exits or Ctrl+C.

    When a subprocess exits with the restart sentinel present, it is respawned
    once (to load newly enabled modules). Any subsequent exit is treated as a
    real error and terminates the supervisor.

    Migrations are applied first. Installing a new version and starting it is one
    act, so a start against a database the new code cannot read is not a state
    worth preserving: it serves an unknown subset of the ledger as errors and
    reads as a bug in the app rather than a schema behind the code. `migrate` is
    idempotent, so this costs nothing when there is nothing to apply, and a
    failure exits non-zero with the alembic error rather than starting anyway.
    """
    from celerp.config import config_path as _cfg_path

    _migrate_to_head(cfg["database"]["url"])

    def _sentinel() -> "Path":
        return _cfg_path().parent / ".restart_requested"

    # Headless installs expose the UI publicly but the UI reaches the API over
    # localhost, so keep the API off the public interface there.
    _api_host = "127.0.0.1" if cfg.get("server", {}).get("headless") else "0.0.0.0"

    def _spawn_api(env: dict, port: int) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "celerp.main:app", "--host", _api_host, "--port", str(port),
             "--timeout-graceful-shutdown", "3"],
            env=env,
        )

    def _spawn_ui(env: dict, port: int) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "ui.app:app", "--host", "0.0.0.0", "--port", str(port),
             "--timeout-graceful-shutdown", "3"],
            env=env,
        )

    env = _config_to_env(cfg)
    api_port = cfg["server"]["api_port"]
    ui_port = cfg["server"]["ui_port"]

    click.echo("Starting Celerp...")
    click.echo(f"  API starting on port {api_port} ...")
    click.echo(f"  UI  starting on port {ui_port} ...")

    api_proc = _spawn_api(env, api_port)
    ui_proc = _spawn_ui(env, ui_port)

    # Readiness prints in dependency order (API first, then the single
    # user-facing URL) so the link never points at a UI whose API is still
    # importing modules.
    _wait_ready((api_proc, api_port), (ui_proc, ui_port))
    click.echo("Press Ctrl+C to stop.\n")

    def _shutdown(sig, frame):
        click.echo("\nShutting down...")
        api_proc.terminate()
        ui_proc.terminate()
        api_proc.wait()
        ui_proc.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        if api_proc.poll() is not None:
            sentinel = _sentinel()
            if sentinel.exists():
                sentinel.unlink()
                click.echo("Restarting API server (config changed)...")
                # Re-read config so newly enabled modules are picked up
                fresh_cfg = _read_config() or cfg
                env = _config_to_env(fresh_cfg)
                api_proc = _spawn_api(env, api_port)
                # Restart UI too so its module nav slots reflect the new config
                ui_proc.terminate()
                ui_proc.wait()
                ui_proc = _spawn_ui(env, ui_port)
            else:
                click.echo(f"API server exited with code {api_proc.returncode}", err=True)
                ui_proc.terminate()
                sys.exit(api_proc.returncode)
        if ui_proc.poll() is not None:
            click.echo(f"UI server exited with code {ui_proc.returncode}", err=True)
            api_proc.terminate()
            sys.exit(ui_proc.returncode)
        time.sleep(0.5)


@main.command("reset-password")
@click.option("--email", prompt="User email", help="Email of the user account to reset.")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True, help="New password.")
def reset_password(email: str, password: str) -> None:
    """Reset a user's password directly via the database."""
    if len(password) < 8:
        click.echo("Error: password must be at least 8 characters.", err=True)
        sys.exit(1)
    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)
    ensure_database(cfg)
    db_url = cfg["database"]["url"]
    sync_url = _sync_url(db_url)
    try:
        from sqlalchemy import create_engine, text
        from celerp.services.auth import hash_password
        engine = create_engine(sync_url)
        with engine.begin() as conn:
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
    ensure_database(cfg)
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
    _migrate_to_head(url)
    click.echo("  ✓ Done")


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
    """Upgrade Celerp to the latest version and run migrations."""
    click.echo("Upgrading Celerp...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "celerp"])
    if result.returncode != 0:
        sys.exit(result.returncode)
    click.echo("Running migrations...")
    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)
    ensure_database(cfg)
    _migrate_to_head(cfg["database"]["url"])
    click.echo("✓ Upgrade complete")


@main.group()
def module() -> None:
    """Manage Celerp modules."""



@module.command("install")
@click.argument("names", nargs=-1, required=True)
def module_install(names: tuple[str, ...]) -> None:
    """Install one or more modules (auto-installs dependencies).

    Example: celerp module install celerp-crm
    """
    cfg = _read_config()
    if not cfg:
        click.echo("Not initialized. Run `celerp init` first.", err=True)
        sys.exit(1)

    _pkg_root = Path(__file__).parent.parent
    module_dir = _pkg_root / "default_modules"

    # Validate all requested modules exist
    for name in names:
        if not (module_dir / name / "__init__.py").exists():
            click.echo(f"Module '{name}' not found in {module_dir}", err=True)
            sys.exit(1)

    currently_enabled: list[str] = cfg.get("modules", {}).get("enabled", [])
    to_install = [n for n in names if n not in currently_enabled]

    if not to_install:
        click.echo("All requested modules are already enabled.")
        return

    install_order = _resolve_install_order(list(to_install), module_dir)
    new_modules = [n for n in install_order if n not in currently_enabled]

    click.echo(f"Installing: {', '.join(new_modules)}")
    _set_enabled_modules(list(names))
    click.echo(f"✓ {len(new_modules)} module(s) installed.")
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

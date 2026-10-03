# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""This copy refuses a database a newer Celerp has opened, through every path that
opens one, before it changes anything; it opens older and legacy databases as before.

Each refused path runs against its own real Postgres database holding a sentinel row,
and leaves the sentinel, alembic_version, instance_meta and the table, column, index
and constraint inventory exactly as they were.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from celerp.db_url import sync_url

DATABASE_URL = os.environ.get("DATABASE_URL", "")
UNKNOWN = "ffff00c0ffee"
RUNNING = "2.5.4"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql"), reason="needs a live Postgres database"
)


def _head() -> str:
    from alembic.script import ScriptDirectory
    from celerp.alembic_config import build_alembic_config
    return ScriptDirectory.from_config(build_alembic_config()).get_current_head()


# Refused databases: (alembic revision, recorded version, expected status). None for the
# revision means the current head; None for the version means no instance_meta at all.
REFUSED = {
    "newer_marker": (None, "2.6.0", "newer_app"),
    "newer_patch_marker": (None, "2.5.5", "newer_app"),
    "newer_dev_marker": (None, "2.5.5.dev3", "newer_app"),
    "unknown_revision_same_marker": (UNKNOWN, RUNNING, "unknown_schema"),
    "unknown_revision_older_marker": (UNKNOWN, "2.4.0", "unknown_schema"),
    "unknown_revision_no_marker": (UNKNOWN, None, "unknown_schema"),
    "unknown_revision_newer_marker": (UNKNOWN, "2.6.0", "newer_app"),
    "corrupt_marker": (None, "not a version", "invalid_version_record"),
}


@pytest.fixture(autouse=True)
def _running(monkeypatch):
    import celerp
    monkeypatch.setattr(celerp, "__version__", RUNNING)
    # The migrate path points DATABASE_URL at the database it migrates.
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)


@pytest.fixture()
def scratch():
    """Factory for scratch databases with the current schema and a sentinel row; dropped afterwards."""
    from celerp.models.base import Base
    import celerp.models  # noqa: F401  (registers every table)

    admin = sa.create_engine(sync_url(DATABASE_URL), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    names: list[str] = []

    def make(revision: str | None = "head", version: str | None = None, *, backfill: str | None = None) -> str:
        name = f"compat_{uuid.uuid4().hex[:10]}"
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        names.append(name)
        base, _, _ = DATABASE_URL.rpartition("/")
        url = f"{base}/{name}"
        engine = sa.create_engine(sync_url(url), poolclass=NullPool)
        try:
            with engine.begin() as conn:
                Base.metadata.create_all(conn)
                conn.execute(sa.text("CREATE TABLE zz_sentinel (id int PRIMARY KEY, note text NOT NULL)"))
                conn.execute(sa.text("INSERT INTO zz_sentinel VALUES (1, 'sentinel-before')"))
                if revision is not None:
                    conn.execute(sa.text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
                    conn.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"),
                                 {"v": _head() if revision == "head" else revision})
                if version is not None or backfill is not None:
                    conn.execute(sa.text("CREATE TABLE instance_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"))
                    for key, value in (("projection_version", version), ("backfill_version", backfill)):
                        if value is not None:
                            conn.execute(sa.text("INSERT INTO instance_meta VALUES (:k, :v)"), {"k": key, "v": value})
        finally:
            engine.dispose()
        return url

    def refused(case: str) -> str:
        revision, version, _ = REFUSED[case]
        return make(revision or "head", version)

    make.refused = refused
    yield make
    for name in names:
        _drop(admin, name)
    admin.dispose()


def _drop(admin, name: str) -> None:
    # An autovacuum worker briefly attached to a fresh database cannot be terminated
    # by a non-superuser; it finishes within moments.
    for attempt in range(20):
        try:
            with admin.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            return
        except sa.exc.DBAPIError:
            if attempt == 19:
                raise
            time.sleep(0.5)


def snapshot(url: str) -> dict:
    """Everything a refused path must leave unchanged."""
    engine = sa.create_engine(sync_url(url), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            tables = set(sa.inspect(conn).get_table_names())

            def rows(sql):
                return [tuple(r) for r in conn.execute(sa.text(sql))]

            return {
                "sentinel": rows("SELECT id, note FROM zz_sentinel ORDER BY id"),
                "alembic_version": rows("SELECT version_num FROM alembic_version ORDER BY 1")
                if "alembic_version" in tables else None,
                "instance_meta": rows("SELECT key, value FROM instance_meta ORDER BY key")
                if "instance_meta" in tables else None,
                "columns": rows(
                    "SELECT table_name, column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2"),
                "indexes": rows(
                    "SELECT tablename, indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' ORDER BY 1, 2"),
                "constraints": rows(
                    "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"),
            }
    finally:
        engine.dispose()


# ── The primitive ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", sorted(REFUSED))
def test_check_classifies_and_changes_nothing(scratch, case):
    from celerp.migrations.compatibility import check_url
    url = scratch.refused(case)
    before = snapshot(url)
    result = check_url(sync_url(url))
    assert result.status == REFUSED[case][2]
    assert result.running == RUNNING
    assert not result.ok
    assert snapshot(url) == before


def test_newer_app_names_both_versions(scratch):
    from celerp.migrations.compatibility import check_url
    result = check_url(sync_url(scratch.refused("unknown_revision_newer_marker")))
    assert result.recorded == "2.6.0" and result.revisions == (UNKNOWN,)
    assert result.message == (
        "This data was last opened with Celerp 2.6.0, which is newer than this copy (2.5.4). "
        "Nothing was changed. Install the latest version of Celerp to open it."
    )


def test_unknown_schema_message_is_generic(scratch):
    from celerp.migrations.compatibility import check_url
    result = check_url(sync_url(scratch.refused("unknown_revision_same_marker")))
    assert result.message.startswith("This data cannot safely be opened by this copy of Celerp (2.5.4).")
    assert UNKNOWN in result.message


@pytest.mark.parametrize("revision,version", [
    ("head", None),          # known schema, legacy: no instance_meta table
    (None, None),            # develop database built by create_all, never stamped
    ("head", RUNNING),       # current
    ("head", "2.5.3"),       # older
    ("head", "2.5.4.dev9"),  # this version's own develop build
])
def test_compatible_databases_open(scratch, revision, version):
    from celerp.migrations.compatibility import check_url
    url = scratch(revision, version)
    before = snapshot(url)
    assert check_url(sync_url(url)).ok
    assert snapshot(url) == before


def test_legacy_database_without_a_marker_row_opens(scratch):
    """instance_meta exists (a migrate wrote backfill_version) but no projection_version yet."""
    from celerp.migrations.compatibility import check_url
    url = scratch("head", None, backfill="2.5.0")
    assert check_url(sync_url(url)).ok


def test_check_never_creates_instance_meta(scratch):
    from celerp.migrations.compatibility import check_url
    url = scratch("head", None)
    check_url(sync_url(url))
    assert snapshot(url)["instance_meta"] is None


def test_check_runs_read_only(scratch):
    """The check's own transaction cannot write, whatever a later change to it reads."""
    from celerp.migrations.compatibility import check
    url = scratch("head", RUNNING)
    engine = sa.create_engine(sync_url(url), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            assert check(conn).ok
            with pytest.raises(sa.exc.InternalError, match="read-only transaction"):
                conn.execute(sa.text("UPDATE zz_sentinel SET note = 'written'"))
            conn.rollback()
    finally:
        engine.dispose()
    assert snapshot(url)["sentinel"] == [(1, "sentinel-before")]


# ── Every path that opens a database ─────────────────────────────────────────

def _apply_migrations(url):
    from celerp.cli import _apply_migrations
    _apply_migrations(url)


def _celerp_migrate(url):
    from click.testing import CliRunner
    from celerp.cli import main
    result = CliRunner().invoke(main, ["migrate", "--db-url", url])
    assert result.exit_code == 1, result.output
    raise _Refused(result.output)


def _celerp_start(url, monkeypatch=None):
    """`celerp start` past the embedded-database start: migrate, then the servers."""
    from celerp import cli
    spawned = []
    cli_mp = pytest.MonkeyPatch()
    try:
        cli_mp.setattr(cli, "_update_state_or_exit", lambda: {})
        cli_mp.setattr(cli, "_server_spawners", lambda cfg: (lambda *a: spawned.append("api"),
                                                             lambda *a: spawned.append("ui")))
        cfg = {"database": {"url": url}, "server": {"api_port": 1, "ui_port": 2},
               "auth": {"jwt_secret": "x" * 40}, "cloud": {"token": None}}
        with pytest.raises(SystemExit) as exc:
            cli._supervise(cfg, release_lock=None)
    finally:
        cli_mp.undo()
    assert exc.value.code == 1 and spawned == []
    raise _Refused("refused")


def _restore_reconcile(url):
    import asyncio
    from celerp.config import settings
    from celerp.services import backup_import
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(settings, "database_url", url)
        asyncio.run(backup_import._reconcile_schema())
    finally:
        mp.undo()


def _api_startup(url):
    """The API on its own (headless, the desktop's server, the update's verify start)."""
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine
    import celerp.main as app_main

    engine = create_async_engine(url, poolclass=NullPool)
    mp = pytest.MonkeyPatch()
    mp.setattr(app_main, "lifecycle_engine", engine)

    async def boot():
        async with app_main.lifespan(app_main.app):
            pass

    try:
        with pytest.raises(SystemExit) as exc:
            asyncio.run(boot())
    finally:
        mp.undo()
        asyncio.run(engine.dispose())
    assert exc.value.code == 1
    raise _Refused("refused")


def _compatibility_command(url):
    from click.testing import CliRunner
    from celerp.cli import main, COMPATIBILITY_REFUSED_EXIT
    result = CliRunner().invoke(main, ["compatibility", "--db-url", url])
    assert result.exit_code == COMPATIBILITY_REFUSED_EXIT, result.output
    raise _Refused(json.loads(result.output)["message"])


class _Refused(Exception):
    pass


PATHS = {
    "apply_migrations": _apply_migrations,
    "celerp_migrate": _celerp_migrate,
    "celerp_start": _celerp_start,
    "restore_reconcile": _restore_reconcile,
    "api_startup": _api_startup,
    "compatibility_command": _compatibility_command,
}


@pytest.mark.parametrize("case", sorted(REFUSED))
@pytest.mark.parametrize("path", sorted(PATHS))
def test_every_entry_path_refuses_before_any_change(scratch, tmp_path, monkeypatch, path, case):
    from celerp.config import settings
    from celerp.migrations.compatibility import IncompatibleDatabase
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    url = scratch.refused(case)
    before = snapshot(url)
    with pytest.raises((IncompatibleDatabase, _Refused, RuntimeError)):
        PATHS[path](url)
    assert snapshot(url) == before


def test_celerp_migrate_prints_the_plain_message(scratch, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from celerp.cli import main
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    result = CliRunner().invoke(main, ["migrate", "--db-url", scratch.refused("newer_marker")])
    assert result.exit_code == 1
    assert "This data was last opened with Celerp 2.6.0, which is newer than this copy (2.5.4)." in result.output


def test_api_startup_prints_the_plain_message(scratch, tmp_path, monkeypatch, capsys):
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with pytest.raises(_Refused):
        _api_startup(scratch.refused("newer_marker"))
    assert "which is newer than this copy (2.5.4)" in capsys.readouterr().err


def test_restore_reconcile_reports_the_refusal(scratch, tmp_path, monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with pytest.raises(RuntimeError, match="newer than this copy"):
        _restore_reconcile(scratch.refused("newer_marker"))


def test_compatibility_command_runs_as_the_packaged_launcher_calls_it(scratch):
    """`python -m celerp compatibility`, with this checkout's real version, as the desktop launcher runs it."""
    from packaging.version import Version
    from celerp.cli import COMPATIBILITY_REFUSED_EXIT
    real = Version(subprocess.run([sys.executable, "-c", "import celerp; print(celerp.__version__)"],
                                  capture_output=True, text=True, check=True).stdout.strip())
    env = {**os.environ, "DATABASE_URL": DATABASE_URL}
    newer = scratch("head", f"{real.major + 1}.0.0")
    current = scratch("head", None)
    out = subprocess.run([sys.executable, "-m", "celerp", "compatibility", "--db-url", newer],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == COMPATIBILITY_REFUSED_EXIT, out.stderr
    decision = json.loads(out.stdout)
    assert decision["status"] == "newer_app" and decision["recorded"] == f"{real.major + 1}.0.0"
    assert decision["running"] == str(real) or Version(decision["running"]) == real
    out = subprocess.run([sys.executable, "-m", "celerp", "compatibility", "--db-url", current],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["status"] == "compatible"


# ── Databases this copy opens ────────────────────────────────────────────────

def test_older_schema_with_older_marker_migrates_forward(scratch, monkeypatch):
    """One revision behind, last opened by an older Celerp: migrated to head, data kept,
    and the recorded version left for the server's successful start to advance."""
    from alembic import command
    from celerp.alembic_config import build_alembic_config
    from celerp.migrations.compatibility import check_url
    url = scratch("head", "2.5.3")
    monkeypatch.setenv("DATABASE_URL", url)
    command.downgrade(build_alembic_config(), "-1")
    older = snapshot(url)
    assert older["alembic_version"] != [(_head(),)]
    assert check_url(sync_url(url)).ok

    _apply_migrations(url)

    after = snapshot(url)
    assert after["alembic_version"] == [(_head(),)]
    assert after["sentinel"] == [(1, "sentinel-before")]
    assert after["instance_meta"] == [("projection_version", "2.5.3")]


def test_older_backup_restored_and_migrated_forward(scratch, tmp_path, monkeypatch):
    """A dump from an older Celerp, one revision behind, restored by pg_restore and
    brought to head by the recovery's schema reconcile, its data intact."""
    import asyncio
    from alembic import command
    from celerp.alembic_config import build_alembic_config
    from celerp.config import settings
    from celerp.services import backup, backup_import
    source = scratch("head", "2.5.3")
    monkeypatch.setenv("DATABASE_URL", source)
    command.downgrade(build_alembic_config(), "-1")
    dump = tmp_path / "database.dump"
    dump.write_bytes(backup.dump_database(source))
    target = scratch(None, None)
    engine = sa.create_engine(sync_url(target), poolclass=NullPool)
    with engine.begin() as conn:  # an empty database, as pg_restore --clean leaves one
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    engine.dispose()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", target)

    asyncio.run(backup_import._run_pg_restore(dump, target))
    asyncio.run(backup_import._reconcile_schema())

    after = snapshot(target)
    assert after["alembic_version"] == [(_head(),)]
    assert after["sentinel"] == [(1, "sentinel-before")]
    assert ("projection_version", "2.5.3") in after["instance_meta"]


def test_current_database_start_changes_nothing(scratch, tmp_path, monkeypatch):
    """A database this version already opened: the start's migrate step is a no-op."""
    from celerp.cli import _migrate_to_head
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    url = scratch("head", RUNNING, backfill=RUNNING)
    before = snapshot(url)
    assert _migrate_to_head(url) is True
    assert snapshot(url) == before

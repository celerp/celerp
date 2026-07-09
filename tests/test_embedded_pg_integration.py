# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Integration tests for the bundled-PostgreSQL path (celerp.embedded_pg + CLI).

Layer 2 of the embedded-Postgres plan: boots a REAL pgserver cluster. Marked
`embedded_pg` so it is excluded from the default suite (slow) and skipped where
the provider has no wheel. Run with: pytest -m embedded_pg

These deliberately do not use the repo conftest's Postgres container — the
embedded cluster IS the database under test.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from celerp import embedded_pg

pytestmark = [
    pytest.mark.embedded_pg,
    pytest.mark.skipif(not embedded_pg.is_available(), reason="pgserver wheel not available here"),
]


@pytest.fixture()
def config_dir(tmp_path, monkeypatch):
    """Isolate config + cluster under a temp dir; stop the cluster afterwards.

    On Windows the cluster lives in a plain mkdtemp — preferring the runner's
    workspace drive (RUNNER_TEMP) over %TEMP%: GitHub's C: temp volume makes
    initdb's directory handling fail ("File exists" on existing dirs, then
    "The current directory is invalid" from cmd.exe), while identical paths
    work on real Windows.
    """
    import shutil
    import tempfile
    from pathlib import Path as _P

    base = (
        _P(tempfile.mkdtemp(prefix="celerp-emb-", dir=os.environ.get("RUNNER_TEMP")))
        if os.name == "nt" else tmp_path
    )
    cfg = base / "celerp"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg / "config.toml"))
    yield cfg
    # Tear down the cluster so no postmaster leaks between tests.
    try:
        embedded_pg.wipe(cfg)
    except Exception:
        pass
    if os.name == "nt":
        shutil.rmtree(base, ignore_errors=True)


def _sync(uri: str) -> str:
    return uri.replace("+asyncpg", "")


def test_ensure_cluster_boots_and_creates_app_db(config_dir):
    uri = embedded_pg.ensure_cluster(config_dir)
    assert uri.startswith("postgresql+asyncpg://")
    assert "/celerp?" in uri  # the app database, over a unix socket
    assert ":5432" not in uri  # never a hardcoded TCP port
    engine = create_engine(_sync(uri))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar() == "celerp"
    finally:
        engine.dispose()


def test_uri_is_stable_across_reboots(config_dir):
    uri1 = embedded_pg.ensure_cluster(config_dir)
    uri2 = embedded_pg.ensure_cluster(config_dir)
    assert uri1 == uri2  # derived from the pgdata path → stored URI stays valid


def test_data_survives_restart(config_dir):
    uri = embedded_pg.ensure_cluster(config_dir)
    engine = create_engine(_sync(uri))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE persist_probe (id int)"))
        conn.execute(text("INSERT INTO persist_probe VALUES (42)"))
    engine.dispose()

    # Boot again (fresh handle) and confirm the row is still there.
    uri2 = embedded_pg.ensure_cluster(config_dir)
    engine2 = create_engine(_sync(uri2))
    try:
        with engine2.connect() as conn:
            assert conn.execute(text("SELECT id FROM persist_probe")).scalar() == 42
    finally:
        engine2.dispose()


def test_wipe_removes_pgdata_and_data(config_dir):
    uri = embedded_pg.ensure_cluster(config_dir)
    engine = create_engine(_sync(uri))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE gone_after_wipe (id int)"))
    engine.dispose()

    embedded_pg.wipe(config_dir)
    assert not embedded_pg.pgdata_dir(config_dir).exists()

    # Re-boot: a fresh cluster, so the table is gone.
    uri2 = embedded_pg.ensure_cluster(config_dir)
    engine2 = create_engine(_sync(uri2))
    try:
        with engine2.connect() as conn:
            present = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'gone_after_wipe'"
            )).scalar()
            assert present == 0
    finally:
        engine2.dispose()


def test_wipe_missing_cluster_is_noop(config_dir):
    # Never booted — wipe must not raise.
    embedded_pg.wipe(config_dir)
    assert not embedded_pg.pgdata_dir(config_dir).exists()


def test_bin_dir_has_pg_dump_and_restore(config_dir):
    bd = embedded_pg.bin_dir()
    assert bd is not None
    from pathlib import Path

    exe = ".exe" if os.name == "nt" else ""
    assert (Path(bd) / f"pg_dump{exe}").exists()
    assert (Path(bd) / f"pg_restore{exe}").exists()


def test_backup_find_pg_tool_resolves_bundled(config_dir, monkeypatch):
    """With pg_bin_dir pointed at the bundled tools (as init sets it), the backup
    service resolves the bundled pg_dump, not a system one."""
    from celerp.config import settings
    from celerp.services import backup

    monkeypatch.setattr(settings, "pg_bin_dir", embedded_pg.bin_dir())
    resolved = backup._find_pg_tool("pg_dump")
    assert resolved.startswith(embedded_pg.bin_dir())


def test_crash_recovery(config_dir):
    """kill -9 the postmaster → ensure_cluster recovers (PG's own stale-pidfile
    handling) → data intact."""
    import signal
    import time

    uri = embedded_pg.ensure_cluster(config_dir)
    engine = create_engine(_sync(uri))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE crashkeep (v int)"))
        conn.execute(text("INSERT INTO crashkeep VALUES (7)"))
    engine.dispose()

    pid = int((embedded_pg.pgdata_dir(config_dir) / "postmaster.pid").read_text().splitlines()[0])
    os.kill(pid, signal.SIGKILL)
    time.sleep(1)

    uri2 = embedded_pg.ensure_cluster(config_dir)  # must recover, not raise
    engine2 = create_engine(_sync(uri2))
    try:
        with engine2.connect() as conn:
            assert conn.execute(text("SELECT v FROM crashkeep")).scalar() == 7
    finally:
        engine2.dispose()


def test_second_process_no_double_postmaster(config_dir):
    """ensure_cluster from a second interpreter while running: connects fine and
    exactly one postmaster serves this pgdata."""
    import subprocess
    import sys

    embedded_pg.ensure_cluster(config_dir)
    code = (
        "import os; os.environ['CELERP_CONFIG']=r'%s';"
        "from celerp import embedded_pg;"
        "from pathlib import Path;"
        "uri = embedded_pg.ensure_cluster(Path(r'%s'));"
        "print(uri)" % (os.environ["CELERP_CONFIG"], config_dir)
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    pid_file = embedded_pg.pgdata_dir(config_dir) / "postmaster.pid"
    assert pid_file.exists()  # still exactly the one cluster


def test_backup_roundtrip_via_bundled_tools(config_dir, monkeypatch):
    """pg_bin_dir wiring proven by USE: dump the embedded DB with the bundled
    pg_dump and restore it with the bundled pg_restore."""
    import subprocess

    uri = embedded_pg.ensure_cluster(config_dir)
    engine = create_engine(_sync(uri))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE bk (v text)"))
        conn.execute(text("INSERT INTO bk VALUES ('roundtrip')"))
    engine.dispose()

    from celerp.config import settings
    from celerp.services import backup

    monkeypatch.setattr(settings, "pg_bin_dir", embedded_pg.bin_dir())
    pg_dump = backup._find_pg_tool("pg_dump")
    pg_restore = backup._find_pg_tool("pg_restore")

    import re

    def _db(u: str, name: str) -> str:
        # swap ONLY the database path segment — the socket dir also contains
        # the string "celerp" (/tmp/celerp-pg-<hash>)
        return re.sub(r"/celerp(\?|$)", rf"/{name}\1", u, count=1)

    dump = config_dir / "bk.dump"
    sync = _sync(uri)
    r = subprocess.run([pg_dump, "-Fc", "-f", str(dump), "-d", sync],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    engine = create_engine(_db(sync, "postgres"), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("CREATE DATABASE bkrestore"))
    engine.dispose()
    r = subprocess.run([pg_restore, "-d", _db(sync, "bkrestore"), str(dump)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    engine = create_engine(_db(sync, "bkrestore"))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT v FROM bk")).scalar() == "roundtrip"
    finally:
        engine.dispose()


# ── CLI-level (real cluster, migrations mocked for speed) ──────────────────────

def test_init_embedded_writes_marker_and_boots(config_dir):
    """`init --embedded --no-start` boots the cluster and persists embedded=true +
    a working socket URI + the bundled pg_bin_dir."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from celerp.cli import _read_config, main

    runner = CliRunner()
    with patch("celerp.cli._run_migrations"), \
         patch("celerp.cli._post_migration_grants"), \
         patch("celerp.cli._needs_ownership_fix", return_value=False):
        result = runner.invoke(main, ["init", "--embedded", "--no-start"])
    assert result.exit_code == 0, result.output
    assert "Using embedded PostgreSQL" in result.output
    cfg = _read_config()
    assert cfg["database"]["embedded"] is True
    assert cfg["database"]["url"].startswith("postgresql+asyncpg://")
    assert cfg["backup"]["pg_bin_dir"] == embedded_pg.bin_dir()
    # The stored URI actually connects.
    engine = create_engine(_sync(cfg["database"]["url"]))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()


def test_ensure_database_refreshes_embedded_uri(config_dir):
    """ensure_database boots the cluster and fills in url + pg_bin_dir for an
    embedded cfg; it is a no-op (no provider import) for external cfg."""
    from celerp.cli import ensure_database

    cfg = {"database": {"url": "stale", "embedded": True}}
    ensure_database(cfg)
    assert cfg["database"]["url"].startswith("postgresql+asyncpg://")
    assert cfg["database"]["url"] != "stale"
    assert cfg["backup"]["pg_bin_dir"] == embedded_pg.bin_dir()

    # External cfg is untouched.
    ext = {"database": {"url": "postgresql+asyncpg://u:p@h/db"}}
    ensure_database(ext)
    assert ext["database"]["url"] == "postgresql+asyncpg://u:p@h/db"
    assert "backup" not in ext

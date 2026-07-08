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
    """Isolate config + cluster under a temp dir; stop the cluster afterwards."""
    cfg = tmp_path / "celerp"
    monkeypatch.setenv("CELERP_CONFIG", str(cfg / "config.toml"))
    yield cfg
    # Tear down the cluster so no postmaster leaks between tests.
    try:
        embedded_pg.wipe(cfg)
    except Exception:
        pass


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

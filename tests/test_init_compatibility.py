# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""`celerp init` checks an existing database before it changes anything.

Without --force, a database a newer Celerp already opened is refused before the
role, ownership, grants, schema, data or the config change, whether init reaches
it directly or provisions it through `sudo -u postgres psql`. --force still wipes
and recreates the database as before.

The databases are real Postgres scratch databases. `sudo -u postgres psql` is
stood in for by running its reads against the same database and recording every
statement that would change something.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from click.testing import CliRunner
from sqlalchemy.pool import NullPool

from celerp.cli import main
from celerp.db_url import sync_url
from test_db_compatibility import DATABASE_URL, NEWER, _running, scratch, snapshot  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql"), reason="needs a live Postgres database"
)


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    """No local config: init starts from nothing."""
    from celerp.config import settings
    path = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(path))
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    return path


class _SuperuserPsql:
    """`sudo -u postgres psql`, as subprocess.run sees it: reads answer from the real
    database in psql's output format; anything else is recorded and not run."""

    def __init__(self):
        self.changes: list[str] = []
        self.reads: list[str] = []

    def __call__(self, args, *a, **k) -> subprocess.CompletedProcess:
        assert list(args[:4]) == ["sudo", "-u", "postgres", "psql"], args
        sql = args[args.index("-c") + 1]
        db = args[args.index("-d") + 1] if "-d" in args else "postgres"
        # Ending other sessions is a change too, though it is spelled as a SELECT.
        if not sql.lstrip().upper().startswith("SELECT") or "pg_terminate_backend" in sql:
            self.changes.append(sql)
            return subprocess.CompletedProcess(args, 0, "", "")
        self.reads.append(sql)
        base, _, _ = DATABASE_URL.rpartition("/")
        engine = sa.create_engine(sync_url(f"{base}/{db}"), poolclass=NullPool)
        try:
            with engine.connect() as conn:
                rows = conn.execute(sa.text(sql.replace("%", "%%").rstrip(";"))).all()
        finally:
            engine.dispose()
        lines = ["|".join(map(_psql_text, r)) for r in rows]
        if "-At" not in args:
            lines.append(f"({len(rows)} row{'' if len(rows) == 1 else 's'})")
        return subprocess.CompletedProcess(args, 0, "\n".join(lines), "")


def _psql_text(value) -> str:
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return "" if value is None else str(value)


def _init(url: str, *extra: str, connects: bool):
    """Run `celerp init` against *url* with every step past the database observed."""
    psql = _SuperuserPsql()
    with patch("celerp.cli.subprocess.run", psql), \
         patch("celerp.cli._test_db", side_effect=[None] if connects else ["password authentication failed", None]), \
         patch("celerp.cli._is_root", return_value=True), \
         patch("celerp.cli._needs_ownership_fix", return_value=True), \
         patch("celerp.cli._fix_ownership", return_value=None) as fix_ownership, \
         patch("celerp.cli._migrate_to_head") as migrate, \
         patch("celerp.cli._start") as start:
        result = CliRunner().invoke(main, ["init", "--db-url", url, "--no-start", *extra])
    return result, psql, fix_ownership, migrate, start


@pytest.mark.parametrize("connects", [True, False], ids=["reaches_it", "provisions_it"])
def test_init_refuses_a_newer_database_before_changing_anything(scratch, config_file, connects):
    url = scratch.refused("newer_marker")
    before = snapshot(url)
    result, psql, fix_ownership, migrate, start = _init(url, connects=connects)
    assert result.exit_code == 1, result.output
    assert "last opened with Celerp 2.6.0" in result.output, result.output
    assert psql.changes == []
    fix_ownership.assert_not_called()
    migrate.assert_not_called()
    start.assert_not_called()
    assert not config_file.exists()
    assert snapshot(url) == before


def test_init_refuses_a_database_a_newer_copy_began_opening(scratch, config_file):
    url = scratch("head", "2.5.4", opened=NEWER)
    before = snapshot(url)
    result, psql, fix_ownership, migrate, _ = _init(url, connects=False)
    assert result.exit_code == 1, result.output
    assert f"last opened with Celerp {NEWER}" in result.output, result.output
    assert psql.changes == []
    fix_ownership.assert_not_called()
    migrate.assert_not_called()
    assert not config_file.exists()
    assert snapshot(url) == before


@pytest.mark.parametrize("connects", [True, False], ids=["reaches_it", "provisions_it"])
def test_init_carries_on_with_a_database_this_copy_may_open(scratch, config_file, connects):
    url = scratch("head", "2.5.4")
    result, psql, fix_ownership, migrate, _ = _init(url, connects=connects)
    assert result.exit_code == 0, result.output
    fix_ownership.assert_called_once()
    migrate.assert_called_once()
    assert config_file.exists()
    if not connects:
        assert any("ALTER USER" in sql for sql in psql.changes)


def test_force_still_wipes_a_newer_database_without_checking_it(scratch, config_file):
    url = scratch.refused("newer_marker")
    dbname = url.rpartition("/")[2]
    psql = _SuperuserPsql()
    with patch("celerp.cli.subprocess.run", psql), \
         patch("celerp.cli._stop_servers"), \
         patch("celerp.cli._init_database") as init_database, \
         patch("celerp.cli._start"):
        result = CliRunner().invoke(main, ["init", "--db-url", url, "--force", "--yes", "--no-start"])
    assert result.exit_code == 0, result.output
    assert not [sql for sql in psql.reads if "to_regclass" in sql]
    assert f"DROP DATABASE IF EXISTS {dbname};" in psql.changes
    init_database.assert_called_once()
    assert config_file.exists()

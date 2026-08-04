# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp.modules.migrations_runner.

The runner applies each enabled module's manifest-declared migration files at
startup, before load_all, under the shared advisory lock. Every test builds its
own fixture module directory under tmp_path (a manifest plus migration files),
so nothing depends on an installed module.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text

from celerp.modules import loader
from celerp.modules.migrations_runner import (
    GuardedOperations,
    run_migration_phase,
    run_module_migrations,
)
from celerp.db import _MIGRATION_LOCK_KEY


# ── fixture builders ──────────────────────────────────────────────────────────

def _make_module(base: Path, name: str, migrations: dict[str, str], *,
                 migrations_pkg: str = "inner.migrations",
                 table_prefix: str = "acme_",
                 declare: bool = True) -> Path:
    """Write base/name/__init__.py plus base/name/<migrations_pkg dirs>/*.py."""
    pkg = base / name
    pkg.mkdir(parents=True)
    manifest = {"name": name, "version": "1.0.0"}
    if declare:
        manifest["migrations"] = migrations_pkg
        manifest["table_prefix"] = table_prefix
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    mig_dir = pkg.joinpath(*migrations_pkg.split("."))
    mig_dir.mkdir(parents=True)
    for fname, body in migrations.items():
        (mig_dir / fname).write_text(body)
    return pkg


def _sync_url() -> str:
    return os.environ["DATABASE_URL"].replace("+asyncpg", "")


# Guarded: creates acme_equipment(id, name) only if absent.
_MIG_001_GUARDED = """
import sqlalchemy as sa
from alembic import op


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "acme_equipment" not in insp.get_table_names():
        op.create_table(
            "acme_equipment",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=100)),
        )
"""

# Guarded: adds a missing column and a missing table.
_MIG_002_GUARDED = """
import sqlalchemy as sa
from alembic import op


def upgrade():
    insp = sa.inspect(op.get_bind())
    cols = [c["name"] for c in insp.get_columns("acme_equipment")]
    if "instructions" not in cols:
        op.add_column("acme_equipment", sa.Column("instructions", sa.Text()))
    if "acme_service_log" not in insp.get_table_names():
        op.create_table(
            "acme_service_log",
            sa.Column("id", sa.Integer(), primary_key=True),
        )
"""

# Unguarded: a bare create_table that raises DuplicateTable on a re-run.
_MIG_UNGUARDED = """
import sqlalchemy as sa
from alembic import op


def upgrade():
    op.create_table(
        "acme_thing",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
"""

# Out-of-prefix DDL: the guard must reject it.
_MIG_OUT_OF_PREFIX = """
import sqlalchemy as sa
from alembic import op


def upgrade():
    op.create_table(
        "outside_table",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
"""

# Creates a prefixed table, then raises: proves rollback.
_MIG_BOOM = """
import sqlalchemy as sa
from alembic import op


def upgrade():
    op.create_table(
        "acme_rollback_probe",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    raise RuntimeError("intentional boom")
"""

# Reads the effective timeouts and writes them to a file.
_MIG_TIMEOUT = """
import sqlalchemy as sa
from alembic import op
from pathlib import Path


def upgrade():
    bind = op.get_bind()
    st = bind.execute(sa.text("SELECT current_setting('statement_timeout')")).scalar()
    lt = bind.execute(sa.text("SELECT current_setting('lock_timeout')")).scalar()
    Path(r"__RESULT__").write_text(st + "|" + lt)
"""

# Probes, from an INDEPENDENT connection, whether the migration lock is held.
_MIG_PROBE_LOCK = """
import os
import sqlalchemy as sa
from pathlib import Path


def upgrade():
    url = os.environ["DATABASE_URL"].replace("+asyncpg", "")
    eng = sa.create_engine(url)
    try:
        with eng.connect() as c:
            got = c.execute(sa.text("SELECT pg_try_advisory_lock(__KEY__)")).scalar()
            Path(r"__RESULT__").write_text("acquired" if got else "blocked")
            if got:
                c.execute(sa.text("SELECT pg_advisory_unlock(__KEY__)"))
    finally:
        eng.dispose()
"""


# ── low-level run helpers (rolled back so no DDL persists) ────────────────────

async def _run_once(engine, pkg, prefix, inspect_fn=None, *,
                    migrations_pkg="inner.migrations"):
    conn = await engine.connect()
    trans = await conn.begin()
    try:
        def _do(sc):
            run_module_migrations(sc, pkg.name, pkg, migrations_pkg, prefix)
            return inspect_fn(sc) if inspect_fn else None
        return await conn.run_sync(_do)
    finally:
        await trans.rollback()
        await conn.close()


async def _second_run_raises(engine, pkg, prefix):
    conn = await engine.connect()
    trans = await conn.begin()
    try:
        def _do(sc):
            run_module_migrations(sc, pkg.name, pkg, "inner.migrations", prefix)
            try:
                run_module_migrations(sc, pkg.name, pkg, "inner.migrations", prefix)
                return False
            except Exception:
                return True
        return await conn.run_sync(_do)
    finally:
        await trans.rollback()
        await conn.close()


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_run_module_migrations_adds_missing_columns_and_tables(_db_engine, tmp_path):
    pkg = _make_module(tmp_path / "modules", f"acme-{uuid.uuid4().hex[:8]}", {
        "m_001_base.py": _MIG_001_GUARDED,
        "m_002_add.py": _MIG_002_GUARDED,
    })

    def _inspect(sc):
        insp = sa.inspect(sc)
        return set(insp.get_table_names()), {c["name"] for c in insp.get_columns("acme_equipment")}

    tables, cols = await _run_once(_db_engine, pkg, "acme_", _inspect)
    assert "acme_equipment" in tables
    assert "acme_service_log" in tables
    assert "instructions" in cols


async def test_unguarded_create_table_raises_on_rerun_while_guarded_pattern_is_a_noop(
        _db_engine, tmp_path):
    unguarded = _make_module(tmp_path / "u", f"acme-{uuid.uuid4().hex[:8]}",
                             {"m_001.py": _MIG_UNGUARDED})
    guarded = _make_module(tmp_path / "g", f"acme-{uuid.uuid4().hex[:8]}",
                           {"m_001.py": _MIG_001_GUARDED})

    assert await _second_run_raises(_db_engine, unguarded, "acme_") is True
    assert await _second_run_raises(_db_engine, guarded, "acme_") is False


async def test_migration_phase_holds_advisory_lock_for_its_duration(
        _db_engine, tmp_path, monkeypatch):
    base = tmp_path / "modules"
    result = tmp_path / "lock_probe.txt"
    body = (_MIG_PROBE_LOCK
            .replace("__KEY__", str(_MIGRATION_LOCK_KEY))
            .replace("__RESULT__", str(result)))
    pkg = _make_module(base, f"acme-{uuid.uuid4().hex[:8]}", {"m_001.py": body})
    monkeypatch.setenv("MODULE_DIR", str(base))

    surviving, errors = await run_migration_phase(_db_engine, {pkg.name})

    assert pkg.name in surviving
    assert errors == {}
    # During the phase, an independent connection could NOT take the lock.
    assert result.read_text() == "blocked"
    # After the phase, the lock is released and freely acquirable again.
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as c:
            got = c.execute(text("SELECT pg_try_advisory_lock(:k)"),
                            {"k": _MIGRATION_LOCK_KEY}).scalar()
            assert got is True
            c.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY})
    finally:
        eng.dispose()


async def test_third_party_migration_failure_records_load_error_and_rolls_back_without_crashing_app(
        _db_engine, tmp_path, monkeypatch):
    base = tmp_path / "modules"
    pkg = _make_module(base, f"acme-{uuid.uuid4().hex[:8]}", {"m_001.py": _MIG_BOOM})
    monkeypatch.setenv("MODULE_DIR", str(base))

    # Not first-party (no lock entry): the phase records and continues, no crash.
    surviving, errors = await run_migration_phase(_db_engine, {pkg.name})

    assert pkg.name not in surviving
    assert pkg.name in errors
    assert "boom" in errors[pkg.name]
    # The failed migration's DDL rolled back: the probe table does not exist.
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as c:
            reg = c.execute(text("SELECT to_regclass('acme_rollback_probe')")).scalar()
            assert reg is None
    finally:
        eng.dispose()


async def test_first_party_migration_failure_raises(_db_engine, tmp_path, monkeypatch):
    base = tmp_path / "modules"
    pkg = _make_module(base, f"acme-{uuid.uuid4().hex[:8]}", {"m_001.py": _MIG_BOOM})
    monkeypatch.setenv("MODULE_DIR", str(base))

    # Make the fixture module first-party by pointing the lock at its live digest.
    lock_file = tmp_path / "fp.lock.json"
    lock_file.write_text(json.dumps({pkg.name: loader.module_content_digest(pkg)}))
    monkeypatch.setattr(loader, "_lock_path", lambda: lock_file)
    loader._first_party_lock.cache_clear()
    try:
        assert loader.is_first_party(pkg)
        with pytest.raises(Exception):
            await run_migration_phase(_db_engine, {pkg.name})
    finally:
        loader._first_party_lock.cache_clear()


async def test_guarded_operations_rejects_ddl_outside_table_prefix(_db_engine, tmp_path):
    pkg = _make_module(tmp_path / "modules", f"acme-{uuid.uuid4().hex[:8]}",
                       {"m_001.py": _MIG_OUT_OF_PREFIX})
    conn = await _db_engine.connect()
    trans = await conn.begin()
    try:
        def _do(sc):
            with pytest.raises(ValueError) as ei:
                run_module_migrations(sc, pkg.name, pkg, "inner.migrations", "acme_")
            return str(ei.value)
        msg = await conn.run_sync(_do)
    finally:
        await trans.rollback()
        await conn.close()
    assert "outside_table" in msg
    assert "acme_" in msg
    # GuardedOperations exposes the single prefix check the overrides share.
    assert issubclass(GuardedOperations, __import__("alembic.operations", fromlist=["Operations"]).Operations)


async def test_runner_fails_closed_on_malformed_table_prefix(_db_engine, tmp_path):
    pkg = _make_module(tmp_path / "modules", f"acme-{uuid.uuid4().hex[:8]}",
                       {"m_001.py": _MIG_001_GUARDED}, table_prefix="ab")
    conn = await _db_engine.connect()
    trans = await conn.begin()
    try:
        def _do(sc):
            with pytest.raises(ValueError):
                run_module_migrations(sc, pkg.name, pkg, "inner.migrations", "ab")
        await conn.run_sync(_do)
    finally:
        await trans.rollback()
        await conn.close()


async def test_runner_sets_statement_and_lock_timeouts(_db_engine, tmp_path):
    result = tmp_path / "timeouts.txt"
    body = _MIG_TIMEOUT.replace("__RESULT__", str(result))
    pkg = _make_module(tmp_path / "modules", f"acme-{uuid.uuid4().hex[:8]}",
                       {"m_001.py": body})
    await _run_once(_db_engine, pkg, "acme_")
    st, lt = result.read_text().split("|")
    assert st == "30s"
    assert lt == "30s"

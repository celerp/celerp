# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The stamp repair reads a table only the startup creates as evidence only once
the startup has created what it needs.

A few kernel tables (the assistant's and the notices') are created from the models
when the app starts, not by a revision, and the revisions that change them return
when they are absent. An install migrated by alembic that has never started the app
lacks them, so reading their absence as a missing change would move its stamp back
and re-run every revision above. Once they exist they are checked as any table: a
change missing from one is still a gap.

Generated over every kernel table (the startup set is exactly what an install
lacks), every table only the startup creates (absent: no gap), and every historical
change on one (missing while the table exists: a gap below its revision).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic_support import apply_migrations
from celerp.alembic_config import build_alembic_config
from celerp.migrations._auto_stamp import (
    _signature_expected,
    extract_signatures,
    find_safe_stamp,
    load_kernel_metadata,
    startup_requirements,
)

_SCRIPT = ScriptDirectory.from_config(build_alembic_config())
_REVS = list(_SCRIPT.walk_revisions())
_HEAD = _SCRIPT.get_current_head()
_METADATA = load_kernel_metadata()
_SIGS = {
    sigs[0].rev: sigs
    for sigs in (extract_signatures(f) for f in Path(_SCRIPT.versions).glob("*.py") if f.name != "__init__.py")
    if sigs
}
_NEEDS = startup_requirements(_METADATA, _SIGS)
_ALONE = sorted(name for name, needs in _NEEDS.items() if name in needs)
_CHANGES = [
    sig for sigs in _SIGS.values() for sig in sigs
    if sig.table in _NEEDS and _signature_expected(_METADATA, sig)
]


def _walk(inspector) -> str:
    return find_safe_stamp(_REVS, _SIGS, inspector, expected_metadata=_METADATA)


@contextlib.contextmanager
def _started(*statements: str):
    """The current model schema, as a started install has it, in a schema of its own,
    with ``statements`` applied; yields an inspector over it."""
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"startup_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        _METADATA.create_all(engine)
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
        yield inspect(engine)
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _below(rev: str) -> set[str]:
    return {r.revision for r in _SCRIPT.iterate_revisions(_SCRIPT.get_revision(rev).down_revision, "base")}


def _undo(sig) -> str:
    if sig.kind == "create_table":
        return f'DROP TABLE "{sig.table}" CASCADE'
    if sig.kind == "add_column":
        return f'ALTER TABLE "{sig.table}" DROP COLUMN "{sig.column}" CASCADE'
    if sig.kind == "drop_column":
        return f'ALTER TABLE "{sig.table}" ADD COLUMN "{sig.column}" BOOLEAN'
    return f'DROP INDEX "{sig.extra}"'


def test_an_install_that_never_started_keeps_its_stamp(fresh_db):
    """The tables only the startup creates are exactly those an alembic install lacks,
    and the walker proves the install's own stamp, so a second migrate moves nothing."""
    async_url, sync_url = fresh_db
    apply_migrations(async_url)
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        assert set(_METADATA.tables) - set(inspector.get_table_names()) == set(_NEEDS)
        assert _walk(inspector) == _HEAD
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", _ALONE)
def test_a_table_only_the_startup_creates_proves_nothing_while_absent(table):
    dependents = sorted(name for name, needs in _NEEDS.items() if table in needs)
    with _started(*(f'DROP TABLE IF EXISTS "{name}" CASCADE' for name in dependents)) as inspector:
        assert _walk(inspector) == _HEAD


@pytest.mark.parametrize("sig", _CHANGES, ids=lambda s: f"{s.rev}-{s.kind}-{s.table}-{s.column or s.extra or ''}")
def test_a_change_missing_from_a_started_table_is_a_gap(sig):
    with _started(_undo(sig)) as inspector:
        assert _walk(inspector) in _below(sig.rev)


def test_every_axis_has_cases():
    assert _ALONE and _CHANGES
    assert {s.kind for s in _CHANGES} >= {"create_table", "add_column", "drop_column"}

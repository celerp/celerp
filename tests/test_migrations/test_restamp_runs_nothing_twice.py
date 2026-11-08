# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A restamp that moves the stamp back runs no revision again whose changes are all
there.

The stamp repair moves a database's stamp back below a schema change it finds
missing, and the upgrade then works up from there. A revision above that ran under
the old stamp may rewrite data as well as change the schema (f0y1z2a3b4c5 names each
peer key's firm, i3b4c5d6e7f8 sets every usage row's reconcile time, k5e6f7a8b9c0
converts each connection's direction). Run again, it would rewrite data a user has
set since. So it is stamped past by the record of what ran, the old stamp's history,
whenever the live schema holds all of its changes, rather than left to fail on its
first schema change before it reaches the rewrite. One with a change missing runs.

Generated over every revision (a restamp below all of them on an install runs none),
every schema change of every revision (hidden from the live schema, its revision is
not held), every revision none of whose changes can be read (not held: nothing shows
they are there), and the three revisions above (their changes removed, each runs
once).
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic_support import apply_migrations
from celerp.alembic_config import build_alembic_config
from celerp.migrations import _auto_stamp
from celerp.migrations._auto_stamp import (
    _signature_applied,
    _signature_expected,
    extract_signatures,
    held_revisions,
    load_kernel_metadata,
    startup_requirements,
)

from .conftest import head_rev, throwaway_db

_SCRIPT = ScriptDirectory.from_config(build_alembic_config())
_REVS = list(_SCRIPT.walk_revisions())
_IDS = [sc.revision for sc in _REVS]
_METADATA = load_kernel_metadata()
_SIGS = {
    sigs[0].rev: sigs
    for sigs in (extract_signatures(f) for f in Path(_SCRIPT.versions).glob("*.py") if f.name != "__init__.py")
    if sigs
}
_STARTUP = startup_requirements(_METADATA, _SIGS)
_READABLE = [
    sig for sigs in _SIGS.values() for sig in sigs
    if _signature_expected(_METADATA, sig) and sig.table not in _STARTUP
]
# Each revision's schema changes, undone; the upgrade puts them back.
_REWRITERS = {
    "f0y1z2a3b4c5": ['ALTER TABLE company_peer_grants DROP COLUMN firm'],
    "i3b4c5d6e7f8": ['ALTER TABLE ai_usage DROP COLUMN reconcile_at, DROP COLUMN unresolved'],
    "k5e6f7a8b9c0": [
        "ALTER TABLE connector_configs DROP COLUMN entity_directions, DROP COLUMN orders_from,"
        " DROP COLUMN connect_lapsed_at, ADD COLUMN direction VARCHAR(16)",
    ],
}


@pytest.fixture()
def upgrade_calls(monkeypatch):
    """Counts each revision's upgrade() calls: alembic loads revision files through
    alembic.util.load_python_file."""
    import alembic.util

    calls: collections.Counter = collections.Counter()
    load = alembic.util.load_python_file

    def counting_load(dir_, filename):
        module = load(dir_, filename)
        rev = getattr(module, "revision", None)
        if rev in _IDS and not getattr(module.upgrade, "_counted", False):
            upgrade = module.upgrade

            def counted():
                calls[rev] += 1
                return upgrade()

            counted._counted = True
            module.upgrade = counted
        return module

    monkeypatch.setattr(alembic.util, "load_python_file", counting_load)
    return calls


def _stamp(sync_url: str) -> str:
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def test_a_restamp_below_every_revision_runs_none_whose_changes_are_there(fresh_db, upgrade_calls, monkeypatch):
    async_url, sync_url = fresh_db
    apply_migrations(async_url)
    upgrade_calls.clear()
    monkeypatch.setattr(_auto_stamp, "find_safe_stamp", lambda *args, **kwargs: _IDS[-1])

    apply_migrations(async_url)

    # Only those whose changes nothing shows are there run, once each.
    assert dict(upgrade_calls) == dict.fromkeys(_UNREADABLE, 1)
    assert _stamp(sync_url) == head_rev()


class _Hiding:
    """The live schema with one revision's change taken out of it."""

    def __init__(self, inner, sig):
        self._inner, self._sig = inner, sig

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_table_names(self, *args, **kwargs):
        names = self._inner.get_table_names(*args, **kwargs)
        return [n for n in names if not (self._sig.kind == "create_table" and n == self._sig.table)]

    def get_columns(self, table, *args, **kwargs):
        cols = self._inner.get_columns(table, *args, **kwargs)
        if table != self._sig.table:
            return cols
        if self._sig.kind == "add_column":
            return [c for c in cols if c["name"] != self._sig.column]
        if self._sig.kind == "drop_column":
            return [*cols, {"name": self._sig.column}]
        return cols

    def _without(self, items, table):
        if table != self._sig.table:
            return items
        return [
            i for i in items
            if i.get("name") != self._sig.extra
            and not (self._sig.columns and tuple(i.get("column_names") or ()) == self._sig.columns)
        ]

    def get_indexes(self, table, *args, **kwargs):
        return self._without(self._inner.get_indexes(table, *args, **kwargs), table)

    def get_unique_constraints(self, table, *args, **kwargs):
        return self._without(self._inner.get_unique_constraints(table, *args, **kwargs), table)


@pytest.fixture(scope="module")
def install_inspector():
    """The schema of an install at head, in a database of its own."""
    with throwaway_db("held") as (async_url, sync_url):
        apply_migrations(async_url)
        engine = create_engine(sync_url)
        try:
            yield inspect(engine)
        finally:
            engine.dispose()


@pytest.mark.parametrize("sig", _READABLE, ids=lambda s: f"{s.rev}-{s.kind}-{s.table}-{s.column or s.extra or ''}")
def test_a_revision_missing_any_of_its_changes_is_not_held(install_inspector, sig):
    if not _signature_applied(install_inspector, sig):
        pytest.skip("a later revision undid this change")  # its revision carries no evidence of it here
    hiding = _Hiding(install_inspector, sig)
    assert sig.rev in held_revisions(_REVS, _SIGS, install_inspector, expected_metadata=_METADATA)
    assert sig.rev not in held_revisions(_REVS, _SIGS, hiding, expected_metadata=_METADATA)


# Revisions with schema changes, none of which the walker can read on an install
# (a module's table, or a table only the startup creates, which is absent there).
_UNREADABLE = sorted(rev for rev, sigs in _SIGS.items() if not set(sigs) & set(_READABLE))


@pytest.mark.parametrize("rev", _UNREADABLE)
def test_a_revision_whose_changes_cannot_be_read_is_not_held(install_inspector, rev):
    assert rev not in held_revisions(_REVS, _SIGS, install_inspector, expected_metadata=_METADATA)


def test_a_revision_with_no_schema_changes_is_held(install_inspector):
    backfills = set(_IDS) - set(_SIGS)
    assert backfills
    assert backfills <= held_revisions(_REVS, _SIGS, install_inspector, expected_metadata=_METADATA)


@pytest.mark.parametrize("rev", sorted(_REWRITERS))
def test_a_revision_missing_its_changes_runs_once(fresh_db, upgrade_calls, rev):
    async_url, sync_url = fresh_db
    apply_migrations(async_url)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            for statement in _REWRITERS[rev]:
                conn.execute(text(statement))
    finally:
        engine.dispose()
    upgrade_calls.clear()

    apply_migrations(async_url)

    # The stamp goes back to the newest revision below it with changes to read; of
    # those above, it and each whose changes cannot be read run, once each.
    evidence = {sig.rev for sig in _READABLE}
    stamp = next(r for r in _IDS[_IDS.index(rev) + 1:] if r in evidence)
    above = _IDS[:_IDS.index(stamp)]
    assert dict(upgrade_calls) == dict.fromkeys([r for r in above if r == rev or r in _UNREADABLE], 1)
    assert _stamp(sync_url) == head_rev()

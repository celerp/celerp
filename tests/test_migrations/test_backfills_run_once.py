# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every data backfill runs once per database.

A backfill converts data as it stood when it was written. Run again later, it
converts data a user has set since: the category-unit backfill turns a stone
sold by the carat into one sold by the gram, so 1.5 ct reads as 1.5 g. So each
data backfill runs once: by alembic upgrade, or by the reconcile when a stamp
moved past it without running it. The alembic stamp is the record of what ran:
every backfill in its history ran, whatever instance_meta holds (releases before
1.2.6 wrote none). A new version alone replays nothing.

The guard counts every call of every data backfill's `upgrade()` across every way
a database was last opened: a new database, a develop database built by
create_all with and without a stamp, and a release database alembic brought to
each backfill (and to just below the first), with and without the upgrade
markers, holding a stone sold by the carat once the category-unit backfill ran.
"""

from __future__ import annotations

import collections
import uuid

import pytest
from sqlalchemy import create_engine, text

import celerp
from alembic_support import alembic_run, history_by_hand, run_migrations
from celerp.cli import _reconcile_after_migrate
from celerp.migrations._data_reconcile import (
    PENDING_BACKFILLS_KEY,
    _lineage,
    data_backfill_scripts,
    pending_backfills,
    set_meta,
    track_restamp,
)
from celerp.models.base import Base

from .conftest import head_rev

_SCRIPTS = data_backfill_scripts()
_BACKFILLS = [sc.revision for sc in _SCRIPTS]
# The category-unit backfill: a stone it saw as weighed goes from carat to gram.
_CARAT_TO_GRAM = "w8x9y0z1a2b3"


def _start(async_url: str) -> None:
    """What `celerp start` runs on the database: the schema, then the reconcile."""
    run_migrations(async_url)
    _reconcile_after_migrate(async_url)


@pytest.fixture()
def backfill_calls(monkeypatch):
    """Counts each data backfill's upgrade() calls, whoever runs it: alembic
    upgrade and the reconcile both load revision files through
    alembic.util.load_python_file."""
    import alembic.util

    calls: collections.Counter = collections.Counter()
    load = alembic.util.load_python_file

    def counting_load(dir_, filename):
        module = load(dir_, filename)
        rev = getattr(module, "revision", None)
        if rev in _BACKFILLS and not getattr(module.upgrade, "_counted", False):
            upgrade = module.upgrade

            def counted():
                calls[rev] += 1
                return upgrade()

            counted._counted = True
            module.upgrade = counted
        return module

    monkeypatch.setattr(alembic.util, "load_python_file", counting_load)
    return calls


def _company(conn) -> uuid.UUID:
    cid = uuid.uuid4()
    conn.execute(
        text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
             "VALUES (:id, 'Gems', :slug, '{}'::json, true, now())"),
        {"id": cid, "slug": f"gems-{cid.hex[:8]}"},
    )
    return cid


def _carat_stone(conn, cid: uuid.UUID) -> None:
    """A diamond the user sold by the carat after the category-unit backfill ran."""
    conn.execute(
        text("INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
             "VALUES (:c, 'item:1', 'item', CAST(:s AS json), 1, now())"),
        {"c": cid, "s": '{"category": "diamond", "sell_by": "carat", "purchase_unit": "carat", '
                        '"weight_unit": "carat", "quantity": 1.5}'},
    )


def _create_all(sync_url: str, *, stamp: str | None) -> None:
    with history_by_hand(sync_url) as conn:
        Base.metadata.create_all(conn)
        if stamp is not None:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": stamp})
        _company(conn)


def _release(async_url: str, sync_url: str, revision: str, markers: bool) -> None:
    """A database a release brought to `revision` with alembic upgrade, with the
    upgrade markers 1.2.6 and later write (no backfill pending) or without them."""
    alembic_run(async_url, "upgrade", revision)
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            cid = _company(conn)
            if _CARAT_TO_GRAM in _lineage(_script(), revision):
                _carat_stone(conn, cid)
            if markers:
                set_meta(conn, PENDING_BACKFILLS_KEY, "")
    finally:
        eng.dispose()


def _script():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    return ScriptDirectory.from_config(build_alembic_config())


# How a database was last opened, as (build, the stamp whose history ran).
_OPENED = {
    "new_database": (lambda a, s: None, None),
    "create_all_unstamped": (lambda a, s: _create_all(s, stamp=None), None),
    "create_all_stamped_at_head": (lambda a, s: _create_all(s, stamp=head_rev()), head_rev()),
}
for _rev in [_SCRIPTS[0].down_revision, *_BACKFILLS]:
    for _markers in (False, True):
        _OPENED[f"release_at_{_rev}_{'tracked' if _markers else 'untracked'}"] = (
            lambda a, s, r=_rev, m=_markers: _release(a, s, r, m), _rev)


@pytest.mark.parametrize("opened", list(_OPENED))
def test_every_data_backfill_runs_once_per_database(fresh_db, backfill_calls, monkeypatch, opened):
    """A backfill in the history of the stamp the database was opened at ran
    already and runs zero times; every other one runs once. Two more starts under
    other versions run none, and a stone sold by the carat stays carat."""
    async_url, sync_url = fresh_db
    build, ran_to = _OPENED[opened]
    build(async_url, sync_url)
    backfill_calls.clear()  # what the build itself ran is the history the stamp records
    ran = _lineage(_script(), ran_to)
    expected = {rev: 0 if rev in ran else 1 for rev in _BACKFILLS}

    _start(async_url)
    for version in ("99.0.0", "99.0.1"):
        monkeypatch.setattr(celerp, "__version__", version)
        _start(async_url)

    assert {rev: backfill_calls[rev] for rev in _BACKFILLS} == expected
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            assert pending_backfills(conn) == []
            state = conn.execute(text("SELECT state FROM projections WHERE entity_id = 'item:1'")).scalar()
    finally:
        eng.dispose()
    if state is not None:
        assert (state["sell_by"], state["purchase_unit"], state["quantity"]) == ("carat", "carat", 1.5)


def test_a_stamp_moved_back_below_a_backfill_never_runs_it_twice(fresh_db):
    """For every data backfill, a restamp from head back below it: one that ran is
    stamped past by the upgrade, one still pending is run by the upgrade and
    leaves the pending list. Either way it runs once."""
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    _, sync_url = fresh_db
    script = ScriptDirectory.from_config(build_alembic_config())
    head = head_rev()
    held = frozenset(_BACKFILLS)  # a data backfill has no change to miss
    eng = create_engine(sync_url)
    try:
        for sc in data_backfill_scripts():
            below = sc.down_revision
            with eng.begin() as conn:
                set_meta(conn, PENDING_BACKFILLS_KEY, "")
                assert sc.revision in track_restamp(conn, script, ran_to=head, stamped_to=below, held=held)
                set_meta(conn, PENDING_BACKFILLS_KEY, sc.revision)
                assert sc.revision not in track_restamp(conn, script, ran_to=head, stamped_to=below, held=held)
                assert sc.revision not in pending_backfills(conn)
    finally:
        eng.dispose()

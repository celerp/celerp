# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every data backfill runs once per database.

A backfill converts data as it stood when it was written. Run again later, it
converts data a user has set since: the category-unit backfill turns a stone
sold by the carat into one sold by the gram, so 1.5 ct reads as 1.5 g. So each
data backfill runs once: by alembic upgrade, or by the reconcile when a stamp
moved past it without running it. A new version alone replays nothing.

The guard counts every call of every data backfill's `upgrade()` across the
database shapes an upgrade meets (a new database, a develop database built by
create_all with and without a stamp), through a start and two more starts under
another version.
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
    BACKFILL_VERSION_KEY,
    PENDING_BACKFILLS_KEY,
    data_backfill_scripts,
    pending_backfills,
    set_meta,
    track_restamp,
)
from celerp.models.base import Base

from .conftest import head_rev

_BACKFILLS = [sc.revision for sc in data_backfill_scripts()]


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


def _create_all(sync_url: str, *, stamp: str | None) -> None:
    with history_by_hand(sync_url) as conn:
        Base.metadata.create_all(conn)
        if stamp is not None:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": stamp})
        _company(conn)


_SHAPES = {
    "new_database": lambda sync_url: None,
    "create_all_unstamped": lambda sync_url: _create_all(sync_url, stamp=None),
    "create_all_stamped_at_head": lambda sync_url: _create_all(sync_url, stamp=head_rev()),
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_every_data_backfill_runs_once_per_database(fresh_db, backfill_calls, monkeypatch, shape):
    async_url, sync_url = fresh_db
    _SHAPES[shape](sync_url)

    _start(async_url)
    assert {rev: backfill_calls[rev] for rev in _BACKFILLS} == dict.fromkeys(_BACKFILLS, 1)

    for version in ("99.0.0", "99.0.1"):
        monkeypatch.setattr(celerp, "__version__", version)
        _start(async_url)
    assert {rev: backfill_calls[rev] for rev in _BACKFILLS} == dict.fromkeys(_BACKFILLS, 1)


def test_a_stone_sold_by_the_carat_stays_carat_across_an_upgrade(fresh_db):
    """A release database an earlier version reconciled, holding a diamond sold by
    the carat: the start of a new version leaves it carat."""
    async_url, sync_url = fresh_db
    alembic_run(async_url, "upgrade", "head")
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            set_meta(conn, BACKFILL_VERSION_KEY, "2.8.0")
            cid = _company(conn)
            conn.execute(
                text("INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
                     "VALUES (:c, 'item:1', 'item', CAST(:s AS json), 1, now())"),
                {"c": cid, "s": '{"category": "diamond", "sell_by": "carat", "purchase_unit": "carat", '
                                '"weight_unit": "carat", "quantity": 1.5}'},
            )

        _start(async_url)

        with eng.connect() as conn:
            state = conn.execute(text("SELECT state FROM projections WHERE entity_id = 'item:1'")).scalar()
    finally:
        eng.dispose()
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
    eng = create_engine(sync_url)
    try:
        for sc in data_backfill_scripts():
            below = sc.down_revision
            with eng.begin() as conn:
                set_meta(conn, PENDING_BACKFILLS_KEY, "")
                assert sc.revision in track_restamp(conn, script, ran_to=head, stamped_to=below)
                set_meta(conn, PENDING_BACKFILLS_KEY, sc.revision)
                assert sc.revision not in track_restamp(conn, script, ran_to=head, stamped_to=below)
                assert sc.revision not in pending_backfills(conn)
    finally:
        eng.dispose()

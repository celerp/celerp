# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Develop→release upgrade: data-backfill reconcile through the real path.

A develop build creates its database with `Base.metadata.create_all`, so the
schema is at head but no migration `upgrade()` ever ran. The auto-stamp walker
(`_auto_stamp.find_safe_stamp`) then deliberately stamps near head and skips
every migration with no verifiable DDL signature — i.e. all data backfills.
That is fine for throwaway dev data, but the develop→release in-place upgrade
must preserve real data, so `_data_reconcile.replay_data_backfills` re-runs
those backfills.

Probe migration: `n8o9p0q1r2s3_backfill_payment_date`, a pure `op.execute`
backfill on the AUTHORITATIVE `ledger.data` (not a rebuildable projection).
It has zero DDL signatures, so the walker skips it; only the reconcile applies
it. The first test pins the walker's (intended) skip so the reconcile's reason
to exist stays visible; the rest prove the reconcile closes the gap.
"""

from __future__ import annotations

import contextlib
import logging
import types
import uuid

import pytest
from sqlalchemy import create_engine, text

from celerp import __version__
from celerp.cli import _reconcile_after_migrate, _run_migrations
from celerp.migrations._data_reconcile import (
    BACKFILL_VERSION_KEY,
    get_meta,
    replay_data_backfills,
)
from celerp.models.base import Base

from .conftest import head_rev as _head_rev

_PAYMENT_TS = "2026-03-15 10:30:00+00"
_EXPECTED_DATE = "2026-03-15"


def _build_dev_schema(sync_url: str, *, stamp: str | None) -> None:
    """Create the full head schema via create_all (like a develop build) and
    seed one company + one payment event whose data is MISSING payment_date.

    `stamp` sets alembic_version to mimic the develop build's guessed stamp;
    pass None to mimic a freshly create_all'd DB that was never stamped.
    """
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            Base.metadata.create_all(conn)
            cid = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                    "VALUES (:id, 'Adv', :slug, '{}'::json, true, now())"
                ),
                {"id": cid, "slug": f"adv-{cid.hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO ledger "
                    "(company_id, entity_id, entity_type, event_type, data, source, idempotency_key, ts) "
                    "VALUES (:cid, 'doc:INV-1', 'doc', 'doc.payment.received', "
                    "CAST(:data AS json), 'auto_je', 'idem-1', :ts)"
                ),
                {"cid": cid, "data": '{"amount": 100}', "ts": _PAYMENT_TS},
            )
            if stamp is not None:
                conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": stamp})
    finally:
        eng.dispose()


def _payment_date(sync_url: str) -> str | None:
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return conn.execute(
                text("SELECT data->>'payment_date' FROM ledger WHERE event_type = 'doc.payment.received'")
            ).scalar()
    finally:
        eng.dispose()


def _reconcile(sync_url: str) -> list[str]:
    eng = create_engine(sync_url)
    try:
        replayed, failures = replay_data_backfills(eng)
        assert not failures, f"unexpected backfill failures: {failures}"
        return replayed
    finally:
        eng.dispose()


def _marker(sync_url: str) -> str | None:
    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return get_meta(conn, BACKFILL_VERSION_KEY)
    finally:
        eng.dispose()


def _clear_payment_date(sync_url: str) -> None:
    """Remove payment_date from the seeded event (simulate un-backfilled data)."""
    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text("UPDATE ledger SET data = (data::jsonb - 'payment_date')::json "
                              "WHERE event_type = 'doc.payment.received'"))
    finally:
        eng.dispose()


def test_run_migrations_alone_skips_authoritative_backfill(fresh_db):
    """Pins the walker's intended behavior: on a create_all DB, `_run_migrations`
    does NOT apply the payment_date backfill (no DDL signature → stamped past).
    This is why the develop→release reconcile exists."""
    async_url, sync_url = fresh_db
    _build_dev_schema(sync_url, stamp=None)

    _run_migrations(async_url)

    assert _payment_date(sync_url) is None


@pytest.mark.parametrize("stamp_at_head", [False, True], ids=["unstamped", "stamped_at_head"])
def test_reconcile_applies_authoritative_backfill(fresh_db, stamp_at_head):
    """After schema migrate + reconcile, the authoritative ledger backfill is
    applied — for both realistic develop-origin states: a never-stamped
    create_all DB, and one already stamped at head."""
    async_url, sync_url = fresh_db
    _build_dev_schema(sync_url, stamp=_head_rev() if stamp_at_head else None)

    _run_migrations(async_url)
    replayed = _reconcile(sync_url)

    assert "n8o9p0q1r2s3" in replayed
    assert _payment_date(sync_url) == _EXPECTED_DATE


def test_reconcile_is_idempotent(fresh_db):
    """Replaying the backfills twice converges (each backfill is guarded), so a
    second dev→release reconcile is a safe no-op."""
    async_url, sync_url = fresh_db
    _build_dev_schema(sync_url, stamp=None)
    _run_migrations(async_url)

    first = _reconcile(sync_url)
    after_first = _payment_date(sync_url)
    second = _reconcile(sync_url)
    after_second = _payment_date(sync_url)

    assert first == second  # same set of migrations replayed
    assert after_first == after_second == _EXPECTED_DATE


def test_reconcile_after_migrate_applies_and_marks(fresh_db):
    """The migrate-time orchestration applies the backfill and records the
    version marker so it won't redo the work next time."""
    async_url, sync_url = fresh_db
    _build_dev_schema(sync_url, stamp=None)
    _run_migrations(async_url)

    assert _marker(sync_url) is None  # develop build never wrote a marker

    _reconcile_after_migrate(async_url)

    assert _payment_date(sync_url) == _EXPECTED_DATE
    assert _marker(sync_url) == __version__


def _seed_dev_origin(sync_url: str) -> uuid.UUID:
    """Create a develop-origin DB: head schema (no stamp), one module-owned
    item.created event, and one authoritative payment event lacking payment_date.
    Returns the company id. No projections — the release rebuild creates them."""
    eng = create_engine(sync_url)
    cid = uuid.uuid4()
    try:
        with eng.begin() as conn:
            Base.metadata.create_all(conn)
            conn.execute(
                text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                     "VALUES (:id, 'Golden', :slug, '{}'::json, true, now())"),
                {"id": cid, "slug": f"golden-{cid.hex[:8]}"},
            )
            for eid, etype, evt, data in [
                ("item:1", "item", "item.created", '{"sku": "S", "name": "A", "quantity": 1}'),
                ("doc:INV-1", "doc", "doc.payment.received", '{"amount": 100}'),
            ]:
                conn.execute(
                    text("INSERT INTO ledger (company_id, entity_id, entity_type, event_type, "
                         "data, source, idempotency_key, ts) VALUES "
                         "(:cid, :eid, :et, :evt, CAST(:d AS json), 'test', :idem, :ts)"),
                    {"cid": cid, "eid": eid, "et": etype, "evt": evt, "d": data,
                     "idem": f"idem-{eid}", "ts": _PAYMENT_TS},
                )
    finally:
        eng.dispose()
    return cid


@pytest.mark.asyncio
async def test_golden_dev_to_release_end_to_end(fresh_db):
    """Capstone, mirroring production: a develop-origin (create_all) DB goes
    through schema migrate → migrate-time backfill replay (sync, psycopg2) →
    lifespan projection rebuild (async, asyncpg). Asserts the authoritative
    ledger backfill applied AND the doctor's independent oracle finds zero stale
    projections. Covers use case 2 (a module event replays via its handler)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from celerp.routers.doctor import _check_stale_projections
    from celerp.services.dev_release_guard import run_upgrade_guard

    async_url, sync_url = fresh_db
    cid = _seed_dev_origin(sync_url)

    # Production launcher path: `celerp migrate` = schema + grants + reconcile.
    _run_migrations(async_url)
    _reconcile_after_migrate(async_url)
    assert _payment_date(sync_url) == _EXPECTED_DATE  # authoritative backfill ran

    # Production API-boot path: the lifespan guard rebuilds projections.
    engine = create_async_engine(async_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            guard = await run_upgrade_guard(s)
            await s.commit()
            assert guard["rebuilt"] is True
            stale = await _check_stale_projections(s, cid, None, fix=False)
            assert stale["found"] == 0, stale["details"]
    finally:
        await engine.dispose()


def test_reconcile_after_migrate_gated_by_version(fresh_db):
    """Once reconciled for a version, a second call is skipped (gated by the
    marker). Proven by corrupting the data and confirming it is NOT re-fixed."""
    async_url, sync_url = fresh_db
    _build_dev_schema(sync_url, stamp=None)
    _run_migrations(async_url)
    _reconcile_after_migrate(async_url)
    assert _payment_date(sync_url) == _EXPECTED_DATE

    # Marker now == current version. Corrupt the data; a gated rerun must NOT
    # touch it (same version → skip), proving the gate actually short-circuits.
    _clear_payment_date(sync_url)
    _reconcile_after_migrate(async_url)

    assert _payment_date(sync_url) is None


# ── Hardening: signature-bearing schema excluded, poison isolation, restore lock ──


def test_schema_migration_excluded_from_replay():
    """The rewritten `e7c9a1b3d5f2` carries verifiable DDL signatures, so the
    reconcile's backfill set now EXCLUDES it (only the signature-less child data
    migration replays), while its `f8a9b0c1d2e3` child IS included."""
    from celerp.migrations._data_reconcile import data_backfill_scripts

    revs = {sc.revision for sc in data_backfill_scripts()}
    assert "e7c9a1b3d5f2" not in revs
    assert "f8a9b0c1d2e3" in revs


class _FakeScript:
    """Minimal stand-in for an alembic Script: a revision id and a module whose
    upgrade() the replay invokes under an Operations context."""

    def __init__(self, revision: str, upgrade_fn):
        self.revision = revision
        self.module = types.SimpleNamespace(upgrade=upgrade_fn)


def _poison_upgrade():
    from alembic import op

    op.execute("CREATE TABLE IF NOT EXISTS poison_probe (n integer)")
    op.execute("INSERT INTO poison_probe (n) VALUES (99)")
    raise RuntimeError("poison backfill blew up")


def _good_upgrade():
    from alembic import op

    op.execute("CREATE TABLE IF NOT EXISTS poison_probe (n integer)")
    op.execute("INSERT INTO poison_probe (n) VALUES (42)")


def test_poison_backfill_isolated_and_durable(fresh_db, monkeypatch, caplog):
    """Two backfills where the FIRST raises: its own transaction rolls back (no
    row 99), the SECOND still commits durably (row 42), the failure is logged at
    ERROR, and the version marker is NOT advanced so the next boot retries."""
    async_url, sync_url = fresh_db

    import celerp.migrations._data_reconcile as dr

    monkeypatch.setattr(
        dr,
        "data_backfill_scripts",
        lambda: [_FakeScript("poison", _poison_upgrade), _FakeScript("good", _good_upgrade)],
    )

    with caplog.at_level(logging.ERROR, logger="celerp.migrations._data_reconcile"):
        _reconcile_after_migrate(async_url)  # must not raise

    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text("SELECT n FROM poison_probe ORDER BY n")).fetchall()
    finally:
        eng.dispose()

    assert [r[0] for r in rows] == [42]  # good durable, poison rolled back
    assert _marker(sync_url) is None  # failure -> marker unset -> retried next boot
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_restore_reconcile_takes_migration_lock(monkeypatch):
    """The HTTP/CLI restore reconcile (`_reconcile_schema`) runs its migration
    steps inside the shared `_migration_lock`, so it serializes with a concurrent
    boot migrate or a second restore."""
    from celerp import cli
    from celerp.services import backup_import

    events: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def _fake_lock(db_url):
        events.append(("lock-enter", db_url))
        try:
            yield
        finally:
            events.append(("lock-exit", db_url))

    monkeypatch.setattr(cli, "_migration_lock", _fake_lock, raising=False)
    monkeypatch.setattr(cli, "_apply_migrations", lambda url: events.append(("apply", url)))
    monkeypatch.setattr(cli, "_post_migration_grants", lambda url: events.append(("grants", url)))
    monkeypatch.setattr(cli, "_reconcile_after_migrate", lambda url: events.append(("reconcile", url)))

    result = await backup_import._reconcile_schema()

    assert result is None  # success path
    kinds = [e[0] for e in events]
    assert "lock-enter" in kinds, "restore reconcile did not take the migration lock"
    # the migration steps ran INSIDE the lock (enter before apply, exit after)
    assert kinds.index("lock-enter") < kinds.index("apply") < kinds.index("lock-exit")

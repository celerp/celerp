# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Connectors set a direction for each kind of record instead of one for the whole
connector. Upgrading turns each existing connection's single direction into the
same per-record directions, so every connection that already syncs keeps running
exactly the syncs it ran before (WooCommerce no longer imports its customer list),
and a connection that already synced counts its first sync as done.
"""

from __future__ import annotations

import importlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from .conftest import run_migration_ops

MODULE = "k5e6f7a8b9c0_connector_entity_directions"
FIRST_SYNC_MODULE = "k6e7f8a9b0c1_connector_execution_lease"

# What each connector ran before, per old direction: inbound pulls, then outbound pushes.
OLD_PLANS = {
    "woocommerce": {"inbound": ["products", "orders", "contacts"],
                    "outbound": ["products_out", "inventory_out"]},
    "shopify": {"inbound": ["products", "orders", "contacts"],
                "outbound": ["products_out", "inventory_out"]},
    "quickbooks": {"inbound": ["products", "orders", "contacts"], "outbound": ["invoices_out"]},
    "xero": {"inbound": ["products", "orders", "contacts"], "outbound": ["invoices_out"]},
    "celerp": {"inbound": ["journals", "period_lock"],
               "outbound": ["products_out", "contacts_out", "journals_out", "documents_out",
                            "inventory_out", "attachments_out"]},
}


def _old_plan(connector: str, direction: str) -> list[str]:
    plans = OLD_PLANS[connector]
    if direction == "both":
        return plans["inbound"] + plans["outbound"]
    return list(plans[direction])


@pytest.fixture()
def cfg_db():
    base_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"dirmig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE connector_configs (
                id SERIAL PRIMARY KEY,
                company_id VARCHAR(64) NOT NULL,
                connector VARCHAR(32) NOT NULL,
                direction VARCHAR(16) NOT NULL DEFAULT 'both',
                sync_frequency VARCHAR(16) NOT NULL DEFAULT 'realtime',
                daily_sync_hour INTEGER NOT NULL DEFAULT 2,
                webhook_ids_json TEXT,
                webhook_secret VARCHAR(128),
                last_daily_sync_at TIMESTAMPTZ
            )
        """))
        conn.execute(text("""
            CREATE TABLE sync_runs (
                id SERIAL PRIMARY KEY,
                company_id VARCHAR(64) NOT NULL,
                connector VARCHAR(32) NOT NULL,
                entity VARCHAR(32) NOT NULL,
                direction VARCHAR(16) NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                status VARCHAR(16) NOT NULL
            )
        """))
    yield engine
    engine.dispose()
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _add_config(conn, connector: str, direction: str) -> str:
    company_id = str(uuid.uuid4())
    conn.execute(text(
        "INSERT INTO connector_configs (company_id, connector, direction) VALUES (:c, :n, :d)"
    ), {"c": company_id, "n": connector, "d": direction})
    return company_id


def _add_run(conn, company_id: str, connector: str, entity: str, at: datetime,
             status: str = "success") -> None:
    conn.execute(text(
        "INSERT INTO sync_runs (company_id, connector, entity, direction, started_at, status)"
        " VALUES (:c, :n, :e, 'inbound', :at, :s)"
    ), {"c": company_id, "n": connector, "e": entity, "at": at, "s": status})


def _directions(conn, company_id: str) -> dict:
    raw = conn.execute(text(
        "SELECT entity_directions FROM connector_configs WHERE company_id = :c"
    ), {"c": company_id}).scalar_one()
    return raw if isinstance(raw, dict) else json.loads(raw)


def test_backfill_preserves_behaviour(cfg_db):
    from celerp.connectors.registry import get
    from celerp.connectors.sync_runner import sync_plan

    rows = {}
    with cfg_db.begin() as conn:
        for connector in OLD_PLANS:
            for direction in ("inbound", "outbound", "both"):
                rows[(connector, direction)] = _add_config(conn, connector, direction)

    run_migration_ops(cfg_db, MODULE)

    with cfg_db.connect() as conn:
        columns = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'connector_configs' AND table_schema = current_schema()"))}
        assert "direction" not in columns
        for (connector, direction), company_id in rows.items():
            directions = _directions(conn, company_id)
            expected = _old_plan(connector, direction)
            if connector == "woocommerce":
                # The store's customer list is no longer imported; customers arrive with their orders.
                expected = [e for e in expected if e != "contacts"]
                assert directions["customers"] == "off"
            assert sync_plan(get(connector), directions) == expected, (connector, direction)


def test_restored_inbound_row_stays_inbound_only(cfg_db):
    """A restored company's connection was inbound only; nothing it holds is pushed out."""
    from celerp.connectors.registry import get
    from celerp.connectors.sync_runner import sync_plan

    with cfg_db.begin() as conn:
        company_id = _add_config(conn, "woocommerce", "inbound")
    run_migration_ops(cfg_db, MODULE)
    with cfg_db.connect() as conn:
        directions = _directions(conn, company_id)
    assert directions == {"products": "inbound", "stock": "off", "orders": "inbound", "customers": "off"}
    assert all(not e.endswith("_out") for e in sync_plan(get("woocommerce"), directions))


def test_first_sync_counts_as_done_for_a_connection_that_already_synced(cfg_db):
    """The first-sync record (i3b4) of a connection made before reviewed first syncs:
    completed when it synced since its last reset or needs no review, else pending."""
    early = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with cfg_db.begin() as conn:
        synced = _add_config(conn, "woocommerce", "both")
        _add_run(conn, synced, "woocommerce", "orders", early + timedelta(days=2))
        _add_run(conn, synced, "woocommerce", "products", early)
        never = _add_config(conn, "shopify", "both")
        reset = _add_config(conn, "quickbooks", "both")
        # Runs before the last disconnect belong to an earlier connection.
        _add_run(conn, reset, "quickbooks", "orders", early)
        _add_run(conn, reset, "quickbooks", "__connector_reset__", early + timedelta(days=1))

    run_migration_ops(cfg_db, MODULE)
    run_migration_ops(cfg_db, FIRST_SYNC_MODULE)

    with cfg_db.connect() as conn:
        first = {row[0]: row[1:] for row in conn.execute(text(
            "SELECT company_id, state, started_at FROM connector_first_syncs")).all()}
    assert first[synced] == ("completed", early)
    assert first[never] == ("pending", None)
    assert first[reset] == ("completed", None)


def test_a_store_disconnected_before_the_upgrade_waits_for_its_review(cfg_db):
    """A reviewed connector's store disconnected since its last run has synced nothing
    since: it reviews a first sync when it reconnects. One that reconnected and ran again
    completed its first sync at that run, and a connection whose runs all failed was still
    syncing each cycle, so it carries on as it did."""
    early = datetime(2026, 3, 1, tzinfo=timezone.utc)
    with cfg_db.begin() as conn:
        disconnected = _add_config(conn, "woocommerce", "both")
        _add_run(conn, disconnected, "woocommerce", "products", early)
        _add_run(conn, disconnected, "woocommerce", "__connector_reset__", early + timedelta(days=1))
        reconnected = _add_config(conn, "shopify", "both")
        _add_run(conn, reconnected, "shopify", "products", early)
        _add_run(conn, reconnected, "shopify", "__connector_reset__", early + timedelta(days=1))
        _add_run(conn, reconnected, "shopify", "orders", early + timedelta(days=2))
        failing = _add_config(conn, "woocommerce", "both")
        _add_run(conn, failing, "woocommerce", "products", early, status="error")

    run_migration_ops(cfg_db, MODULE)
    run_migration_ops(cfg_db, FIRST_SYNC_MODULE)

    with cfg_db.connect() as conn:
        first = {row[0]: row[1:] for row in conn.execute(text(
            "SELECT company_id, state, started_at FROM connector_first_syncs")).all()}
    assert first[disconnected] == ("pending", None)
    assert first[reconnected] == ("completed", early + timedelta(days=2))
    assert first[failing] == ("completed", early)


def test_the_first_sync_backfill_names_every_reviewed_connector():
    import importlib

    from celerp import connectors

    mig = importlib.import_module(f"celerp.migrations.versions.{FIRST_SYNC_MODULE}")
    assert set(mig._REVIEWED) == {c.name for c in connectors.all_connectors() if c.reviewed_first_sync}


def test_downgrade_restores_a_single_direction(cfg_db):
    with cfg_db.begin() as conn:
        rows = {d: _add_config(conn, "woocommerce", d) for d in ("inbound", "outbound", "both")}
    run_migration_ops(cfg_db, MODULE)
    run_migration_ops(cfg_db, MODULE, "downgrade")
    with cfg_db.connect() as conn:
        back = dict(conn.execute(text("SELECT company_id, direction FROM connector_configs")).all())
    assert {d: back[c] for d, c in rows.items()} == {"inbound": "inbound", "outbound": "outbound",
                                                       "both": "both"}


def _combinations(connector: str):
    """Every per-record setting a connection of ``connector`` can hold."""
    import itertools

    mig = importlib.import_module(f"celerp.migrations.versions.{MODULE}")
    kinds = mig._FLOWS[connector]
    choices = {
        kind: ["off", *ways, *(["both"] if len(ways) == 2 else [])] for kind, ways in kinds.items()
    }
    for values in itertools.product(*choices.values()):
        yield dict(zip(choices, values))


@pytest.mark.parametrize("connector", sorted(OLD_PLANS))
def test_a_downgrade_takes_back_only_what_one_direction_says_exactly(connector):
    """Every setting a connection can hold: one old direction gives back exactly that
    setting, or the downgrade refuses it; it never widens it (an all-off connection
    never becomes both)."""
    mig = importlib.import_module(f"celerp.migrations.versions.{MODULE}")
    exact = {json.dumps(mig._per_record(connector, d), sort_keys=True): d
             for d in ("inbound", "outbound", "both")}
    for directions in _combinations(connector):
        expected = exact.get(json.dumps(directions, sort_keys=True))
        if expected is None:
            with pytest.raises(RuntimeError):
                mig._old_direction(connector, directions)
        else:
            assert mig._old_direction(connector, directions) == expected, directions


def test_a_downgrade_with_a_setting_one_direction_cannot_hold_changes_nothing(cfg_db):
    with cfg_db.begin() as conn:
        kept = _add_config(conn, "shopify", "both")
        stopped = _add_config(conn, "woocommerce", "both")
    run_migration_ops(cfg_db, MODULE)
    off = {"products": "off", "stock": "off", "orders": "off", "customers": "off"}
    with cfg_db.begin() as conn:
        conn.execute(text("UPDATE connector_configs SET entity_directions = CAST(:d AS json) "
                          "WHERE company_id = :c"), {"d": json.dumps(off), "c": stopped})

    with pytest.raises(RuntimeError, match="woocommerce"):
        run_migration_ops(cfg_db, MODULE, "downgrade")

    with cfg_db.connect() as conn:
        columns = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'connector_configs' AND table_schema = current_schema()"))}
        assert "direction" not in columns and "entity_directions" in columns
        assert _directions(conn, stopped) == off
        assert _directions(conn, kept)["products"] == "both"


@pytest.mark.parametrize("state", ["pending", "starting", "running", "failed"])
def test_a_downgrade_refuses_a_first_sync_that_is_not_done(cfg_db, state):
    """The old schema has no first sync: a connection waiting on its review would start
    syncing unreviewed, and one part-way through (or failed with its import kept) would
    lose what it needs to resume or undo. The downgrade stops before changing anything."""
    with cfg_db.begin() as conn:
        done = _add_config(conn, "woocommerce", "both")
        _add_run(conn, done, "woocommerce", "products", datetime(2026, 3, 1, tzinfo=timezone.utc))
        waiting = _add_config(conn, "shopify", "both")
    run_migration_ops(cfg_db, MODULE)
    run_migration_ops(cfg_db, FIRST_SYNC_MODULE)
    with cfg_db.begin() as conn:
        conn.execute(text("UPDATE connector_first_syncs SET state = :s WHERE company_id = :c"),
                     {"s": state, "c": waiting})

    with pytest.raises(RuntimeError, match="shopify"):
        run_migration_ops(cfg_db, FIRST_SYNC_MODULE, "downgrade")

    with cfg_db.connect() as conn:
        assert conn.execute(text("SELECT state FROM connector_first_syncs WHERE company_id = :c"),
                            {"c": waiting}).scalar_one() == state


def test_a_downgrade_refuses_while_connector_work_holds_its_lease(cfg_db):
    with cfg_db.begin() as conn:
        done = _add_config(conn, "quickbooks", "both")
    run_migration_ops(cfg_db, MODULE)
    run_migration_ops(cfg_db, FIRST_SYNC_MODULE)
    with cfg_db.begin() as conn:
        conn.execute(text("INSERT INTO connector_execution_leases (company_id, connector, resource, holder) "
                          "VALUES (:c, 'quickbooks', 'invoices', :h)"), {"c": done, "h": str(uuid.uuid4())})

    with pytest.raises(RuntimeError, match="quickbooks"):
        run_migration_ops(cfg_db, FIRST_SYNC_MODULE, "downgrade")


def test_settled_connections_downgrade_to_their_one_direction(cfg_db):
    """Completed first syncs and no running work: both revisions go back, and each
    connection holds the direction it was upgraded from."""
    with cfg_db.begin() as conn:
        rows = {d: _add_config(conn, "quickbooks", d) for d in ("inbound", "outbound", "both")}
    run_migration_ops(cfg_db, MODULE)
    run_migration_ops(cfg_db, FIRST_SYNC_MODULE)

    run_migration_ops(cfg_db, FIRST_SYNC_MODULE, "downgrade")
    run_migration_ops(cfg_db, MODULE, "downgrade")

    with cfg_db.connect() as conn:
        back = dict(conn.execute(text("SELECT company_id, direction FROM connector_configs")).all())
    assert {d: back[c] for d, c in rows.items()} == {d: d for d in rows}

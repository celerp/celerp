# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The barcode uniqueness migration: creates the partial unique index on clean data,
and refuses (without altering anything) when incompatible data exists."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from celerp.inventory_codes import BARCODE_UNIQUE_INDEX, LEGACY_BARCODE_UNIQUE_INDEX

from .conftest import run_migration_ops

MODULE = "bc0d1e2f3a4b_barcode_unique_index"
INDEX = BARCODE_UNIQUE_INDEX
LEGACY_INDEX = LEGACY_BARCODE_UNIQUE_INDEX


def _count(mig_db) -> int:
    with mig_db.engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM projections")).scalar_one()


def _index_exists(mig_db, name: str = INDEX) -> bool:
    # Scope to this fixture's isolated schema: pg_indexes spans every schema, so an
    # unscoped name match would see the identically-named index another parallel
    # worker built in its own schema (or the app schema's create_all copy).
    with mig_db.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM pg_indexes "
                "WHERE indexname = :n AND schemaname = current_schema()"
            ),
            {"n": name},
        ).scalar_one() > 0


def test_creates_index_and_enforces_uniqueness_on_clean_data(mig_db):
    cid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "barcode": "12345"})
    mig_db.insert_item(cid, "item:2", {"sku": "B", "barcode": "67890"})
    mig_db.insert_item(cid, "item:3", {"sku": "C", "barcode": ""})       # empty barcode: exempt
    mig_db.insert_item(cid, "item:4", {"sku": "D"})                        # no barcode: exempt
    # Same barcode in a different company is allowed (index is per company).
    mig_db.insert_item(other, "item:5", {"sku": "E", "barcode": "12345"})

    run_migration_ops(mig_db.engine, MODULE)
    assert _index_exists(mig_db)

    # A duplicate non-empty barcode within the company is now rejected by the DB.
    with pytest.raises(IntegrityError):
        mig_db.insert_item(cid, "item:6", {"sku": "F", "barcode": "12345"})

    # Empty/absent barcodes remain insertable without collision.
    mig_db.insert_item(cid, "item:7", {"sku": "G", "barcode": ""})


def test_preflight_blocks_duplicate_barcodes_without_altering(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "barcode": "12345"})
    mig_db.insert_item(cid, "item:2", {"sku": "B", "barcode": "12345"})

    with pytest.raises(RuntimeError, match="duplicate barcode"):
        run_migration_ops(mig_db.engine, MODULE)

    assert not _index_exists(mig_db)
    assert _count(mig_db) == 2  # nothing renamed, cleared, or dropped
    assert mig_db.get_state(cid, "item:1")["barcode"] == "12345"
    assert mig_db.get_state(cid, "item:2")["barcode"] == "12345"


def test_preflight_reports_oversized_and_comma_values(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:long-bc", {"sku": "A", "barcode": "1" * 65})
    mig_db.insert_item(cid, "item:long-sku", {"sku": "S" * 256, "barcode": "222"})
    mig_db.insert_item(cid, "item:comma", {"sku": "X,Y", "barcode": "333"})

    with pytest.raises(RuntimeError) as exc:
        run_migration_ops(mig_db.engine, MODULE)

    message = str(exc.value)
    assert "over 64 chars" in message          # oversized barcode
    assert "over 255 chars" in message          # oversized SKU
    assert "comma-bearing SKU" in message
    assert not _index_exists(mig_db)
    assert _count(mig_db) == 3


def test_preflight_allows_live_plus_merged_historical_duplicate(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:live", {"sku": "LIVE", "barcode": "12345", "status": "available"})
    mig_db.insert_item(cid, "item:old", {"sku": "OLD", "barcode": "12345", "status": "MERGED"})

    run_migration_ops(mig_db.engine, MODULE)
    assert _index_exists(mig_db)

    # A merged historical row cannot become resolvable while colliding with live.
    with pytest.raises(IntegrityError):
        with mig_db.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE projections SET state = jsonb_set(state::jsonb, '{status}', "
                    "'\"available\"'::jsonb)::json "
                    "WHERE company_id = :cid AND entity_id = 'item:old'"
                ),
                {"cid": cid},
            )


def test_replaces_legacy_all_status_index_and_replays_idempotently(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "barcode": "12345"})
    with mig_db.engine.begin() as conn:
        conn.execute(text(
            f"CREATE UNIQUE INDEX {LEGACY_INDEX} "
            "ON projections (company_id, (state ->> 'barcode')) "
            "WHERE entity_type = 'item' AND NULLIF(state ->> 'barcode', '') IS NOT NULL"
        ))

    run_migration_ops(mig_db.engine, MODULE)
    assert _index_exists(mig_db)
    assert not _index_exists(mig_db, LEGACY_INDEX)

    run_migration_ops(mig_db.engine, MODULE)
    assert _index_exists(mig_db)
    assert not _index_exists(mig_db, LEGACY_INDEX)


def _create_sql_ascii_projections(engine) -> None:
    with engine.begin() as conn:
        assert conn.execute(text("SHOW server_encoding")).scalar() == "SQL_ASCII"
        conn.execute(text("""
            CREATE TABLE projections (
                entity_id TEXT NOT NULL,
                company_id UUID NOT NULL,
                entity_type TEXT NOT NULL,
                state JSON NOT NULL,
                PRIMARY KEY (company_id, entity_id)
            )
        """))


def _insert_sql_ascii_item(conn, cid: str, entity_id: str, state: dict) -> None:
    conn.execute(
        text(
            "INSERT INTO projections VALUES "
            "(:entity_id, CAST(:cid AS uuid), 'item', CAST(:state AS json))"
        ),
        {
            "entity_id": entity_id,
            "cid": cid,
            "state": json.dumps(state),
        },
    )


def test_sql_ascii_preflight_blocks_duplicate_with_unrelated_unicode(sql_ascii_fresh_db):
    _, sync_url = sql_ascii_fresh_db
    engine = create_engine(sync_url)
    try:
        _create_sql_ascii_projections(engine)
        cid = str(uuid.uuid4())
        with engine.begin() as conn:
            _insert_sql_ascii_item(
                conn, cid, "item:1",
                {"sku": "A", "barcode": "12345", "name": "Crème Brûlée"},
            )
            _insert_sql_ascii_item(
                conn, cid, "item:2",
                {"sku": "B", "barcode": "12345", "status": "available"},
            )

        with pytest.raises(RuntimeError, match="duplicate barcode"):
            run_migration_ops(engine, MODULE)

        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM projections")).scalar_one() == 2
    finally:
        engine.dispose()


def test_sql_ascii_preflight_allows_live_plus_merged_duplicate(sql_ascii_fresh_db):
    _, sync_url = sql_ascii_fresh_db
    engine = create_engine(sync_url)
    try:
        _create_sql_ascii_projections(engine)
        cid = str(uuid.uuid4())
        with engine.begin() as conn:
            _insert_sql_ascii_item(
                conn, cid, "item:live",
                {"sku": "LIVE", "barcode": "12345", "status": "available", "name": "Müller"},
            )
            _insert_sql_ascii_item(
                conn, cid, "item:old",
                {"sku": "OLD", "barcode": "12345", "status": "MERGED"},
            )

        run_migration_ops(engine, MODULE)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM projections")).scalar_one() == 2
    finally:
        engine.dispose()


def test_sql_ascii_preflight_reports_oversized_and_comma_values(sql_ascii_fresh_db):
    _, sync_url = sql_ascii_fresh_db
    engine = create_engine(sync_url)
    try:
        _create_sql_ascii_projections(engine)
        cid = str(uuid.uuid4())
        with engine.begin() as conn:
            _insert_sql_ascii_item(
                conn, cid, "item:long-bc",
                {"sku": "A", "barcode": "1" * 65, "name": "José"},
            )
            _insert_sql_ascii_item(
                conn, cid, "item:long-sku",
                {"sku": "S" * 256, "barcode": "222"},
            )
            _insert_sql_ascii_item(
                conn, cid, "item:comma",
                {"sku": "X,Y", "barcode": "333"},
            )

        with pytest.raises(RuntimeError) as exc:
            run_migration_ops(engine, MODULE)

        message = str(exc.value)
        assert "over 64 chars" in message
        assert "over 255 chars" in message
        assert "comma-bearing SKU" in message
    finally:
        engine.dispose()


def test_sql_ascii_removes_barcode_expression_indexes(sql_ascii_fresh_db):
    _, sync_url = sql_ascii_fresh_db
    engine = create_engine(sync_url)
    try:
        _create_sql_ascii_projections(engine)
        with engine.begin() as conn:
            cid = str(uuid.uuid4())
            _insert_sql_ascii_item(
                conn, cid, "item:1",
                {"sku": "A", "barcode": "12345", "status": "available"},
            )
            conn.execute(text(
                f"CREATE UNIQUE INDEX {LEGACY_INDEX} "
                "ON projections (company_id, (state ->> 'barcode')) "
                "WHERE entity_type = 'item' AND NULLIF(state ->> 'barcode', '') IS NOT NULL"
            ))
            conn.execute(text(
                f"CREATE UNIQUE INDEX {INDEX} "
                "ON projections (company_id, (state ->> 'barcode')) "
                "WHERE entity_type = 'item' AND NULLIF(state ->> 'barcode', '') IS NOT NULL "
                "AND lower(COALESCE(state ->> 'status', '')) <> 'merged'"
            ))

        run_migration_ops(engine, MODULE)

        with engine.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
                ))
            }
            assert INDEX not in names
            assert LEGACY_INDEX not in names
            assert conn.execute(text("SELECT count(*) FROM projections")).scalar_one() == 1
    finally:
        engine.dispose()

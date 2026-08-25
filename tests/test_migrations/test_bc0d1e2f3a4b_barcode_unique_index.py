# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The barcode uniqueness migration: creates the partial unique index on clean data,
and refuses (without altering anything) when incompatible data exists."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .conftest import run_migration_ops

MODULE = "bc0d1e2f3a4b_barcode_unique_index"
INDEX = "uq_projection_company_item_barcode"


def _count(mig_db) -> int:
    with mig_db.engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM projections")).scalar_one()


def _index_exists(mig_db) -> bool:
    # Scope to this fixture's isolated schema: pg_indexes spans every schema, so an
    # unscoped name match would see the identically-named index another parallel
    # worker built in its own schema (or the app schema's create_all copy).
    with mig_db.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM pg_indexes "
                "WHERE indexname = :n AND schemaname = current_schema()"
            ),
            {"n": INDEX},
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

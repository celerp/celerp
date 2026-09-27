# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The head revision removes every physical-code unique index an earlier upgrade built."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from celerp.inventory_codes import (
    BARCODE_UNIQUE_INDEX,
    LEGACY_BARCODE_UNIQUE_INDEX,
    RFID_EPC_UNIQUE_INDEX,
)

from .conftest import run_migration_ops

MODULE = "k8f9a0b1c2d3_drop_physical_code_unique_indexes"
ALL_INDEXES = {BARCODE_UNIQUE_INDEX, LEGACY_BARCODE_UNIQUE_INDEX, RFID_EPC_UNIQUE_INDEX}


def _index_names(engine) -> set[str]:
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            ))
        }


def _create_all_indexes(engine) -> None:
    with engine.begin() as conn:
        for name, field in (
            (BARCODE_UNIQUE_INDEX, "barcode"),
            (LEGACY_BARCODE_UNIQUE_INDEX, "barcode"),
            (RFID_EPC_UNIQUE_INDEX, "rfid_epc"),
        ):
            conn.execute(text(
                f"CREATE UNIQUE INDEX {name} ON projections (company_id, (state ->> '{field}')) "
                f"WHERE entity_type = 'item' AND NULLIF(state ->> '{field}', '') IS NOT NULL"
            ))


def test_upgrade_drops_all_three_indexes_idempotently(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "barcode": "7508", "rfid_epc": "E1"})
    _create_all_indexes(mig_db.engine)
    assert ALL_INDEXES <= _index_names(mig_db.engine)

    run_migration_ops(mig_db.engine, MODULE)
    assert _index_names(mig_db.engine).isdisjoint(ALL_INDEXES)

    run_migration_ops(mig_db.engine, MODULE)
    assert _index_names(mig_db.engine).isdisjoint(ALL_INDEXES)
    assert mig_db.get_state(cid, "item:1") == {"sku": "A", "barcode": "7508", "rfid_epc": "E1"}

    # Duplicates are now storable.
    mig_db.insert_item(cid, "item:2", {"sku": "B", "barcode": "7508", "rfid_epc": "E1"})


def test_downgrade_does_not_recreate_indexes(mig_db):
    cid = str(uuid.uuid4())
    mig_db.insert_item(cid, "item:1", {"sku": "A", "barcode": "7508"})
    mig_db.insert_item(cid, "item:2", {"sku": "B", "barcode": "7508"})

    run_migration_ops(mig_db.engine, MODULE, "downgrade")

    assert _index_names(mig_db.engine).isdisjoint(ALL_INDEXES)

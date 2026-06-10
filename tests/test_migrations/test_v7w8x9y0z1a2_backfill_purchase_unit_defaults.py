# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration v7w8x9y0z1a2: backfill purchase_conversion_factor and
purchase_unit defaults (Postgres jsonb path)."""

from __future__ import annotations

import uuid

_MOD = "v7w8x9y0z1a2_backfill_purchase_unit_defaults"


def test_backfills_conversion_factor(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"sku": "A"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid)["purchase_conversion_factor"] == 1


def test_backfills_purchase_unit_from_sell_by(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"sku": "B", "sell_by": "gram"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid)["purchase_unit"] == "gram"


def test_does_not_overwrite_existing_purchase_unit(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"sku": "C", "sell_by": "gram", "purchase_unit": "piece"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid)["purchase_unit"] == "piece"


def test_no_sell_by_leaves_purchase_unit_absent(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"sku": "D"})

    mig_db.run_upgrade(_MOD)

    state = mig_db.get_state(cid, eid)
    assert "purchase_unit" not in state
    assert state["purchase_conversion_factor"] == 1
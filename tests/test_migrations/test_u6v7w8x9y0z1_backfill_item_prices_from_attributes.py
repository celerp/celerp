# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration u6v7w8x9y0z1: promote *_price keys from state.attributes
to top-level (Postgres jsonb path)."""

from __future__ import annotations

import uuid

_MOD = "u6v7w8x9y0z1_backfill_item_prices_from_attributes"


def test_promotes_price_from_attributes(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"sku": "A", "attributes": {"cost_price": 8500.0}})

    mig_db.run_upgrade(_MOD)

    state = mig_db.get_state(cid, eid)
    assert state["cost_price"] == 8500.0
    assert state["attributes"] == {"cost_price": 8500.0}  # attributes retained


def test_does_not_overwrite_existing_toplevel_price(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"cost_price": 999.0, "attributes": {"cost_price": 100.0}})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid)["cost_price"] == 999.0


def test_ignores_non_price_attributes(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"sku": "B", "attributes": {"color": "red", "weight": 5}})

    mig_db.run_upgrade(_MOD)

    state = mig_db.get_state(cid, eid)
    assert state == {"sku": "B", "attributes": {"color": "red", "weight": 5}}


def test_item_without_attributes_untouched(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"sku": "C", "retail_price": 750.0})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid) == {"sku": "C", "retail_price": 750.0}
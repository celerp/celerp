# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration w8x9y0z1a2b3: backfill category-specific sell_by /
purchase_unit / weight_unit defaults (Postgres jsonb path).

Uses 'diamond' — a CARAT_TO_GRAM gem category whose defaults are
sell_by/purchase_unit/weight_unit = gram.
"""

from __future__ import annotations

import uuid

_MOD = "w8x9y0z1a2b3_backfill_category_unit_defaults"


def test_carat_sell_by_becomes_gram(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"category": "diamond", "sell_by": "carat"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid)["sell_by"] == "gram"


def test_backfills_purchase_unit_from_category(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"category": "diamond", "sell_by": "gram"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid)["purchase_unit"] == "gram"


def test_overrides_purchase_unit_equal_to_sell_by(mig_db):
    """The old generic fallback set purchase_unit == sell_by; category default wins."""
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    # jewelry: sell_by=piece, purchase_unit=piece, weight_unit=gram. Seed the
    # stale fallback purchase_unit == sell_by and check it is overridden to the
    # category default (here both are 'piece', so use a category where they differ).
    mig_db.insert_item(cid, eid, {"category": "gold_bullion", "sell_by": "piece", "purchase_unit": "piece"})

    mig_db.run_upgrade(_MOD)

    # gold_bullion purchase_unit default is 'gram'
    assert mig_db.get_state(cid, eid)["purchase_unit"] == "gram"


def test_backfills_weight_unit(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"category": "diamond", "sell_by": "gram", "purchase_unit": "gram"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid)["weight_unit"] == "gram"


def test_unknown_category_untouched(mig_db):
    cid, eid = str(uuid.uuid4()), f"item:{uuid.uuid4()}"
    mig_db.insert_item(cid, eid, {"category": "widget", "sell_by": "carat"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid) == {"category": "widget", "sell_by": "carat"}
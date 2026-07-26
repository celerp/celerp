# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The 6960 backfill for companies that predate foreign-currency entries.

The acc_db fixture in conftest builds the accounts table this migration writes
to, in an isolated schema, with a MagicMock standing in for alembic.op.
"""

from __future__ import annotations

import uuid

MODULE = "f6a7b8c9d0e1_seed_fx_rounding_account"


def test_backfills_account_for_company_with_operating_expenses_parent(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.run_upgrade(MODULE)

    row = acc_db.get(cid, "6960")
    assert row is not None
    assert row.name == "Foreign Exchange Rounding"
    assert row.account_type == "expense"
    assert row.parent_code == "6000"


def test_skips_company_that_already_has_the_account(acc_db):
    """A company that already has 6960 keeps whatever it named it."""
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.add_account(cid, "6960", "FX Rounding (renamed)", parent_code="6000")
    acc_db.run_upgrade(MODULE)

    assert acc_db.get(cid, "6960").name == "FX Rounding (renamed)"


def test_skips_company_without_the_parent_account(acc_db):
    """No 6000 means a chart this migration does not understand; leave it be."""
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "1111", "Bank", account_type="asset")
    acc_db.run_upgrade(MODULE)

    assert acc_db.get(cid, "6960") is None


def test_downgrade_removes_the_seeded_row(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.run_upgrade(MODULE)
    acc_db.run_downgrade(MODULE)

    assert acc_db.get(cid, "6960") is None

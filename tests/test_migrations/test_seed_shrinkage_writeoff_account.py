# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The 6970 shrinkage/write-off account backfill for companies that predate the default.

The acc_db fixture in conftest builds the accounts table this migration writes
to, in an isolated schema, with a MagicMock standing in for alembic.op.
"""

from __future__ import annotations

import uuid

MODULE = "c9d8e7f6a5b4_seed_shrinkage_writeoff_account"


def test_migration_seeds_6970_for_existing_company(acc_db):
    """A company with a chart (6000 parent) but no 6970 gets it seeded as expense/parent 6000, and a
    second run is idempotent (no duplicate, name preserved)."""
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.run_upgrade(MODULE)

    row = acc_db.get(cid, "6970")
    assert row is not None
    assert row.name == "Inventory Shrinkage & Write-offs"
    assert row.account_type == "expense"
    assert row.parent_code == "6000"

    # Idempotent on re-run.
    acc_db.run_upgrade(MODULE)
    assert acc_db.get(cid, "6970").name == "Inventory Shrinkage & Write-offs"


def test_skips_company_that_already_has_the_account(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.add_account(cid, "6970", "Shrinkage (renamed)", parent_code="6000")
    acc_db.run_upgrade(MODULE)

    assert acc_db.get(cid, "6970").name == "Shrinkage (renamed)"


def test_skips_company_without_the_parent_account(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "1111", "Bank", account_type="asset")
    acc_db.run_upgrade(MODULE)

    assert acc_db.get(cid, "6970") is None


def test_downgrade_is_noop_preserves_existing_6970(acc_db):
    """downgrade() is a no-op: a company's pre-existing 6970 (which the upgrade cannot distinguish
    from a seeded row) survives the reversal with its user-chosen name intact."""
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6000", "Operating Expenses")
    acc_db.add_account(cid, "6970", "My Own Shrinkage", parent_code="6000")
    acc_db.run_upgrade(MODULE)    # skips: already present
    acc_db.run_downgrade(MODULE)  # no-op: preserves it
    row = acc_db.get(cid, "6970")
    assert row is not None
    assert row.name == "My Own Shrinkage"

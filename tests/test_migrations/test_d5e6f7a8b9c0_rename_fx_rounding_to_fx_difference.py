# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Renaming 6960 from Rounding to Difference on charts that already carry it.

The account is no longer posted to automatically, so "Rounding" stopped
describing what lands there. The rename is only the seeded name's to make: a
company that named the account itself keeps its own name.
"""

from __future__ import annotations

import uuid

MODULE = "d5e6f7a8b9c0_rename_fx_rounding_to_fx_difference"

OLD_NAME = "Foreign Exchange Rounding"
NEW_NAME = "Foreign Exchange Difference"


def test_renames_the_seeded_account(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6960", OLD_NAME, parent_code="6000")
    acc_db.run_upgrade(MODULE)

    assert acc_db.get(cid, "6960").name == NEW_NAME


def test_leaves_a_company_that_named_the_account_itself(acc_db):
    """Someone chose that name. A rename migration is not the place to overrule it."""
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6960", "Exchange gains and losses", parent_code="6000")
    acc_db.run_upgrade(MODULE)

    assert acc_db.get(cid, "6960").name == "Exchange gains and losses"


def test_leaves_other_accounts_alone(acc_db):
    """The old name is matched on 6960 only, not wherever the string appears."""
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6961", OLD_NAME, parent_code="6000")
    acc_db.run_upgrade(MODULE)

    assert acc_db.get(cid, "6961").name == OLD_NAME


def test_downgrade_restores_the_old_name(acc_db):
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "6960", OLD_NAME, parent_code="6000")
    acc_db.run_upgrade(MODULE)
    acc_db.run_downgrade(MODULE)

    assert acc_db.get(cid, "6960").name == OLD_NAME

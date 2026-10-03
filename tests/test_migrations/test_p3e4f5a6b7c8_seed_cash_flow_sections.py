# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Existing seeded charts take the cash flow sections the statement used to infer
from account numbers: non-current assets investing, non-current liabilities and
equity financing. Only the untouched seeded headers are filled in."""

from __future__ import annotations

import uuid

from sqlalchemy import text

MODULE = "p3e4f5a6b7c8_seed_cash_flow_sections"


def test_revision_is_the_single_head():
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_heads() == ["p3e4f5a6b7c8"]
    assert script.get_revision("p3e4f5a6b7c8").down_revision == "o2d3e4f5a6b7"


def _with_category_column(acc_db) -> None:
    with acc_db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE accounts ADD COLUMN cash_flow_category VARCHAR(16)"))


def _category(acc_db, cid: str, code: str):
    with acc_db.engine.connect() as conn:
        return conn.execute(text(
            "SELECT cash_flow_category FROM accounts WHERE company_id = :cid AND code = :code"),
            {"cid": cid, "code": code}).scalar()


def _seeded_headers(acc_db, cid: str) -> None:
    acc_db.add_account(cid, "1000", "Assets", "asset")
    acc_db.add_account(cid, "1200", "Non-Current Assets", "asset", "1000")
    acc_db.add_account(cid, "2000", "Liabilities", "liability")
    acc_db.add_account(cid, "2200", "Non-Current Liabilities", "liability", "2000")
    acc_db.add_account(cid, "3000", "Equity", "equity")


def test_seeded_headers_take_their_sections_and_a_second_run_changes_nothing(acc_db):
    _with_category_column(acc_db)
    cid = str(uuid.uuid4())
    _seeded_headers(acc_db, cid)
    acc_db.run_upgrade(MODULE)
    acc_db.run_upgrade(MODULE)
    assert [_category(acc_db, cid, c) for c in ("1000", "1200", "2000", "2200", "3000")] == [
        None, "investing", None, "financing", "financing"]


def test_a_header_the_company_changed_or_classified_itself_is_left_alone(acc_db):
    _with_category_column(acc_db)
    cid = str(uuid.uuid4())
    acc_db.add_account(cid, "1200", "Non-Current Assets", "asset", "1100")
    acc_db.add_account(cid, "2200", "Non-Current Liabilities", "liability", "2000")
    acc_db.add_account(cid, "3000", "Equity", "liability")
    with acc_db.engine.begin() as conn:
        conn.execute(text("UPDATE accounts SET cash_flow_category = 'operating' WHERE code = '2200'"))
    acc_db.run_upgrade(MODULE)
    assert [_category(acc_db, cid, c) for c in ("1200", "2200", "3000")] == [None, "operating", None]


def test_a_fresh_install_without_the_accounts_table_is_a_no_op(mig_db):
    mig_db.run_upgrade(MODULE)

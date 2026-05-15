# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""seed 1130-P and 1130-OB inventory sub-accounts for existing companies

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-05-15

Adds two sub-accounts under 1130 (Inventory):
  1130-P  Inventory - Purchased    (for PO/bill JEs going forward)
  1130-OB Inventory - Opening Balance  (for one-time opening balance JE)

New companies already get these via THAI_CHART_OF_ACCOUNTS on creation.
This migration backfills existing companies.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "t5u6v7w8x9y0"
down_revision = "s4t5u6v7w8x9"
branch_labels = None
depends_on = None

_NEW_ACCOUNTS = [
    ("1130-P",  "Inventory - Purchased",        "asset", "1130"),
    ("1130-OB", "Inventory - Opening Balance",  "asset", "1130"),
]


def upgrade() -> None:
    conn = op.get_bind()
    # Get all company IDs that have a 1130 account but not yet the sub-accounts
    companies = conn.execute(
        sa.text(
            "SELECT DISTINCT company_id FROM accounts WHERE code = '1130'"
        )
    ).fetchall()

    for (company_id,) in companies:
        for code, name, acc_type, parent_code in _NEW_ACCOUNTS:
            exists = conn.execute(
                sa.text("SELECT 1 FROM accounts WHERE company_id = :cid AND code = :code"),
                {"cid": company_id, "code": code},
            ).fetchone()
            if not exists:
                conn.execute(
                    sa.text(
                        "INSERT INTO accounts (id, company_id, code, name, account_type, parent_code, is_active, created_at) "
                        "VALUES (gen_random_uuid(), :cid, :code, :name, :acc_type, :parent, TRUE, NOW())"
                    ),
                    {"cid": company_id, "code": code, "name": name, "acc_type": acc_type, "parent": parent_code},
                )


def downgrade() -> None:
    conn = op.get_bind()
    for code, _, _, _ in _NEW_ACCOUNTS:
        conn.execute(sa.text("DELETE FROM accounts WHERE code = :code"), {"code": code})

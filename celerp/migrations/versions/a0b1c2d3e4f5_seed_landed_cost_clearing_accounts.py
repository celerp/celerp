# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""seed landed-cost clearing sub-accounts under 1130 for existing companies

Revision ID: a0b1c2d3e4f5
Revises: z9a0b1c2d3e4
Create Date: 2026-06-15

Adds four landed-cost clearing sub-accounts under 1130 (Inventory):
  1130-FRT  Inventory - Freight Clearing
  1130-INS  Inventory - Insurance Clearing
  1130-DTY  Inventory - Import Duty Clearing
  1130-IVT  Inventory - Non-Recoverable Import VAT Clearing

Capitalisable import charges (freight/insurance/duty + non-recoverable import VAT) land in
these clearing accounts at bill posting and capitalise into inventory on receipt.
New companies already get these via THAI_CHART_OF_ACCOUNTS on creation; this backfills existing.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a0b1c2d3e4f5"
down_revision = "z9a0b1c2d3e4"
branch_labels = None
depends_on = None

_NEW_ACCOUNTS = [
    ("1130-FRT", "Inventory - Freight Clearing",                    "asset", "1130"),
    ("1130-INS", "Inventory - Insurance Clearing",                  "asset", "1130"),
    ("1130-DTY", "Inventory - Import Duty Clearing",                "asset", "1130"),
    ("1130-IVT", "Inventory - Non-Recoverable Import VAT Clearing", "asset", "1130"),
]


def upgrade() -> None:
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'accounts'"
        )
    ).fetchone()
    if not table_exists:
        return  # Nothing to backfill on fresh install

    companies = conn.execute(
        sa.text("SELECT DISTINCT company_id FROM accounts WHERE code = '1130'")
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

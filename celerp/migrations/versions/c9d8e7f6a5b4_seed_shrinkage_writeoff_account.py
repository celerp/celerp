# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""seed the inventory shrinkage/write-off account 6970 for existing companies

Revision ID: c9d8e7f6a5b4
Revises: bc0d1e2f3a4b
Create Date: 2026-08-31

Inventory write-offs and audit shrinkage post to 6970. New companies get the
account from THAI_CHART_OF_ACCOUNTS on creation; this backfills existing ones,
which would otherwise be refused at posting time.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c9d8e7f6a5b4"
down_revision = "bc0d1e2f3a4b"
branch_labels = None
depends_on = None

_CODE = "6970"
_NAME = "Inventory Shrinkage & Write-offs"
_TYPE = "expense"
_PARENT = "6000"


def upgrade() -> None:
    conn = op.get_bind()
    # to_regclass resolves against the connection's search_path rather than
    # assuming public, so the guard is correct for any schema the migration is
    # actually run in.
    table_exists = conn.execute(sa.text("SELECT to_regclass('accounts')")).scalar()
    if not table_exists:
        return  # Nothing to backfill on fresh install

    companies = conn.execute(
        sa.text("SELECT DISTINCT company_id FROM accounts WHERE code = :parent"),
        {"parent": _PARENT},
    ).fetchall()

    for (company_id,) in companies:
        exists = conn.execute(
            sa.text("SELECT 1 FROM accounts WHERE company_id = :cid AND code = :code"),
            {"cid": company_id, "code": _CODE},
        ).fetchone()
        if not exists:
            conn.execute(
                sa.text(
                    "INSERT INTO accounts (id, company_id, code, name, account_type, parent_code, is_active, created_at) "
                    "VALUES (gen_random_uuid(), :cid, :code, :name, :acc_type, :parent, TRUE, NOW())"
                ),
                {"cid": company_id, "code": _CODE, "name": _NAME,
                 "acc_type": _TYPE, "parent": _PARENT},
            )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM accounts WHERE code = :code"), {"code": _CODE})

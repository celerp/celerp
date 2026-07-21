# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""seed the foreign-exchange rounding account 6960 for existing companies

Revision ID: f6a7b8c9d0e1
Revises: d0e1f2a3b4c5
Create Date: 2026-07-20

A manual journal entry recorded in a foreign currency converts each line
independently, so foreign amounts that balance exactly can convert to local
amounts that do not. The difference posts to 6960 as its own visible line.
New companies get the account from THAI_CHART_OF_ACCOUNTS on creation; this
backfills existing ones, which would otherwise be refused at posting time.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None

_CODE = "6960"
_NAME = "Foreign Exchange Rounding"
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

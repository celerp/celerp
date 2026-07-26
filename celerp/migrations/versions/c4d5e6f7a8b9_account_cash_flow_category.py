# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Add accounts.cash_flow_category (cash flow statement classification).

Revision ID: c4d5e6f7a8b9
Revises: f6a7b8c9d0e1
Create Date: 2026-07-25

The cash flow statement sorts each cash movement into operating, investing or
financing by looking at the accounts on the other side of the entry. A default is
derived from account_type and code range, which is correct for the seeded chart, so
this column stays null unless a company's own chart needs to disagree. Nullable with
a derived fallback also means a backup taken before this revision restores cleanly.

Only an existing install needs this. The accounts table belongs to the accounting
module and is created from its models when the API loads its modules, which is
after core migrations run, so on a new install there is no table here to alter and
the model already carries the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def _has_accounts_table() -> bool:
    """Whether the module-owned accounts table exists yet.

    to_regclass resolves against the connection's search_path rather than assuming
    public, so the guard is correct for any schema the migration is actually run in.
    """
    return bool(op.get_bind().execute(sa.text("SELECT to_regclass('accounts')")).scalar())


def upgrade() -> None:
    if not _has_accounts_table():
        return  # New install: the module creates the table with this column already
    op.add_column("accounts", sa.Column("cash_flow_category", sa.String(16), nullable=True))


def downgrade() -> None:
    if not _has_accounts_table():
        return
    op.drop_column("accounts", "cash_flow_category")

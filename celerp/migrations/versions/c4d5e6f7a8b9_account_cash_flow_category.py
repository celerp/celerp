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
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("cash_flow_category", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "cash_flow_category")

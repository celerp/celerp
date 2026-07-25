# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Add accounts.cash_flow_category (cash flow statement classification).

Revision ID: a1b2c3d4e5f6
Revises: z9a0b1c2d3e4
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

revision = "a1b2c3d4e5f6"
down_revision = "z9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("cash_flow_category", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "cash_flow_category")

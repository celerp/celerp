# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""bank_accounts.relabelled_from: the currency label the books-currency correction
replaced, so that correction can be undone. No account has been corrected under this
rule yet, so every existing row starts empty.

Revision ID: j5d6e7f8a9b0
Revises: i3b4c5d6e7f8
"""
import sqlalchemy as sa
from alembic import op

revision = "j5d6e7f8a9b0"
down_revision = "i3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_accounts", sa.Column("relabelled_from", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_accounts", "relabelled_from")

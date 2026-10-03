# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Record on each import whether Undo can return the company to its state before it.

Imports recorded before this revision cannot be shown to have had no other effect,
so they are recorded as not reversible.

Revision ID: t7i8d9e0f1a2
Revises: s6h7c8d9e0f1
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa

revision = "t7i8d9e0f1a2"
down_revision = "s6h7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_batches", sa.Column("reversible", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("import_batches", "reversible")

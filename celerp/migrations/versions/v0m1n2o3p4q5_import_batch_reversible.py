# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Record on each import whether Undo can return the company to its state before it.

Imports recorded before this revision cannot be shown to have had no other effect,
so they are recorded as not reversible.

Revision ID: v0m1n2o3p4q5
Revises: u8j9k0l1m2n3
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa

revision = "v0m1n2o3p4q5"
down_revision = "u8j9k0l1m2n3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_batches", sa.Column("reversible", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("import_batches", "reversible")

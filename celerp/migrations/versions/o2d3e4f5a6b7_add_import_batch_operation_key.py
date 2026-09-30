# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Add import_batches.operation_key so one logical import is one history entry.

Revision ID: o2d3e4f5a6b7
Revises: n1c2d3e4f5a6
Create Date: 2026-09-30

An item import is written in bounded chunks. Every chunk of one import now adds
its created items to the same history entry, found by the import's operation
key. The key is unique per company while the entry is active; Undo clears it so
the same source can be imported again as a new entry. Existing entries keep a
NULL key.
"""

from alembic import op
import sqlalchemy as sa

revision = "o2d3e4f5a6b7"
down_revision = "n1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_batches", sa.Column("operation_key", sa.Text(), nullable=True))
    op.create_index(
        "uq_import_batch_company_operation", "import_batches", ["company_id", "operation_key"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_import_batch_company_operation", table_name="import_batches")
    op.drop_column("import_batches", "operation_key")

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""ai_usage.reconcile_at: when a pending row may be read back from the AI service.

Every send under a question's id writes it (the send time plus the call's whole
budget plus the service's clock guard), so a row is read back only after the
service has ended and settled that send. A row from before this revision belongs to
a run the upgrade's restart already ended, so it is due at once: reconcile_at =
created_at.

Revision ID: i3b4c5d6e7f8
Revises: h2a3b4c5d6e7
"""
import sqlalchemy as sa
from alembic import op

revision = "i3b4c5d6e7f8"
down_revision = "h2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_usage", sa.Column("reconcile_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE ai_usage SET reconcile_at = created_at")
    op.alter_column("ai_usage", "reconcile_at", nullable=False)


def downgrade() -> None:
    op.drop_column("ai_usage", "reconcile_at")

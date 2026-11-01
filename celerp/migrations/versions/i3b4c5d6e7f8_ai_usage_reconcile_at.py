# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""ai_usage.reconcile_at: when a pending row may be read back from the AI service;
ai_usage.unresolved: a pending row the service can no longer confirm.

Every send under a question's id writes it (the send time plus the call's whole
budget plus the service's clock guard), so a row is read back only after the
service has ended and settled that send. A row from before this revision belongs to
a run the upgrade's restart already ended, so it is due at once: reconcile_at =
created_at. No row is unresolved yet: the reconcile pass marks the ones past the
service's retry window.

Revision ID: i3b4c5d6e7f8
Revises: h4c5d6e7f8a9
"""
import sqlalchemy as sa
from alembic import op
from celerp.migrations._locks import lock_tables

revision = "i3b4c5d6e7f8"
down_revision = "h4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_usage", sa.Column("reconcile_at", sa.DateTime(timezone=True), nullable=True))
    lock_tables(op.get_bind(), "ai_usage")
    op.execute("UPDATE ai_usage SET reconcile_at = created_at")
    op.alter_column("ai_usage", "reconcile_at", nullable=False)
    op.add_column("ai_usage", sa.Column("unresolved", sa.Boolean(), nullable=False,
                                        server_default=sa.false()))
    op.create_check_constraint("ck_ai_usage_unresolved_pending", "ai_usage", "pending OR NOT unresolved")


def downgrade() -> None:
    op.drop_constraint("ck_ai_usage_unresolved_pending", "ai_usage", type_="check")
    op.drop_column("ai_usage", "unresolved")
    op.drop_column("ai_usage", "reconcile_at")

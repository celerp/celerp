# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Store in-flight subscription email payloads outside document projections.

Revision ID: n9b0c1d2e3f4
Revises: m8a9b0c1d2e3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "n9b0c1d2e3f4"
down_revision = "m8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("subscription_email_attempts"):
        op.create_table(
            "subscription_email_attempts",
            sa.Column("company_id", sa.Uuid(as_uuid=True),
                      sa.ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("operation_id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("doc_id", sa.Text(), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "doc_id", "attempt_number",
                                name="uq_subscription_email_document_attempt"),
        )


def downgrade() -> None:
    if inspect(op.get_bind()).has_table("subscription_email_attempts"):
        op.drop_table("subscription_email_attempts")

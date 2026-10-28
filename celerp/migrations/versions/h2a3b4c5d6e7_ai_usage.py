# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The ai_usage table: one row per question the AI service was asked, keyed by the
question's operation id, with what the service charged for it.

Earlier questions carry over from where they were counted before: the credits on a
user message, and the credits of a receipts reading job. The assistant's own tables
are created at startup, not by a revision, so a database that has never started the
app has none to carry over. Carried-over charges are final (not pending), and carrying them over
again finds them already there.

Revision ID: h2a3b4c5d6e7
Revises: g1z2a3b4c5d6
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "h2a3b4c5d6e7"
down_revision = "g1z2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "user_id", "operation_id", name="uq_ai_usage_operation"),
    )
    op.create_index("ix_ai_usage_company_id", "ai_usage", ["company_id"])
    inspector = inspect(op.get_bind())
    if inspector.has_table("ai_messages") and inspector.has_table("ai_conversations"):
        op.execute(
            "INSERT INTO ai_usage (id, company_id, user_id, operation_id, credits, pending, created_at) "
            "SELECT m.id, c.company_id, c.user_id, m.id, m.credits_used, false, m.created_at "
            "FROM ai_messages m JOIN ai_conversations c ON c.id = m.conversation_id "
            "WHERE m.role = 'user' AND m.credits_used > 0 "
            "ON CONFLICT ON CONSTRAINT uq_ai_usage_operation DO NOTHING"
        )
    # A jobs table created at startup by a release that already keeps this record has
    # no charge counter, and nothing to carry over.
    if inspector.has_table("ai_batch_jobs") and any(
            c["name"] == "credits_consumed" for c in inspector.get_columns("ai_batch_jobs")):
        op.execute(
            "INSERT INTO ai_usage (id, company_id, user_id, operation_id, credits, pending, created_at) "
            "SELECT j.id, j.company_id, j.user_id, j.id, j.credits_consumed, false, j.created_at "
            "FROM ai_batch_jobs j WHERE j.credits_consumed > 0 "
            "ON CONFLICT ON CONSTRAINT uq_ai_usage_operation DO NOTHING"
        )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_company_id", table_name="ai_usage")
    op.drop_table("ai_usage")

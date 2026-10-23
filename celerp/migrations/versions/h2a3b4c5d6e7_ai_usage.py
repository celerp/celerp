# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The ai_usage table: every charge the AI service reported, one row per charge.

Earlier questions carry over from where they were counted before: the credits on a
user message, and the credits of a receipts reading job. The assistant's own tables
are created at startup, not by a revision, so a database that has never started the
app has none to carry over.

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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ai_usage_company_id", "ai_usage", ["company_id"])
    inspector = inspect(op.get_bind())
    if inspector.has_table("ai_messages") and inspector.has_table("ai_conversations"):
        op.execute(
            "INSERT INTO ai_usage (id, company_id, user_id, operation_id, credits, created_at) "
            "SELECT m.id, c.company_id, c.user_id, m.id, m.credits_used, m.created_at "
            "FROM ai_messages m JOIN ai_conversations c ON c.id = m.conversation_id "
            "WHERE m.role = 'user' AND m.credits_used > 0"
        )
    if inspector.has_table("ai_batch_jobs"):
        op.execute(
            "INSERT INTO ai_usage (id, company_id, user_id, operation_id, credits, created_at) "
            "SELECT j.id, j.company_id, j.user_id, j.id, j.credits_consumed, j.created_at "
            "FROM ai_batch_jobs j WHERE j.credits_consumed > 0"
        )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_company_id", table_name="ai_usage")
    op.drop_table("ai_usage")

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""ai_usage: one row per AI service operation of a company, not per user and operation.

Two users can send one service operation (the same bank suggestion request retried by
both), which the service charges once; a row per user counted that charge once per
user. The usage of an operation is now one row, attributed to the user who sent it
first. Existing rows of one operation merge into its earliest row (by creation time,
then id), which keeps its user; when that row is still pending and another row of the
operation is settled, it takes the settled charge, since every settled row of an
operation names the same whole charge.

Revision ID: x7c8d9e0f1a2
Revises: w6x7b8s9l0n1
"""
from alembic import op

from celerp.migrations._locks import lock_tables

revision = "x7c8d9e0f1a2"
down_revision = "w6x7b8s9l0n1"
branch_labels = None
depends_on = None

_EARLIEST = (
    "SELECT DISTINCT ON (company_id, operation_id) id, company_id, operation_id FROM ai_usage "
    "ORDER BY company_id, operation_id, created_at, id"
)


def upgrade() -> None:
    lock_tables(op.get_bind(), "ai_usage")
    op.execute(
        "UPDATE ai_usage u SET credits = s.credits, pending = false, unresolved = false "
        f"FROM ({_EARLIEST}) e, (SELECT DISTINCT ON (company_id, operation_id) company_id, operation_id, credits "
        "FROM ai_usage WHERE NOT pending ORDER BY company_id, operation_id, created_at, id) s "
        "WHERE u.id = e.id AND u.pending AND s.company_id = e.company_id AND s.operation_id = e.operation_id")
    op.execute(f"DELETE FROM ai_usage u WHERE u.id NOT IN (SELECT id FROM ({_EARLIEST}) e)")
    op.drop_constraint("uq_ai_usage_operation", "ai_usage", type_="unique")
    op.create_unique_constraint("uq_ai_usage_operation", "ai_usage", ["company_id", "operation_id"])


def downgrade() -> None:
    op.drop_constraint("uq_ai_usage_operation", "ai_usage", type_="unique")
    op.create_unique_constraint("uq_ai_usage_operation", "ai_usage", ["company_id", "user_id", "operation_id"])

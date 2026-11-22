# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""ai_usage: one row per AI service operation of a company, not per user and operation.

Two users can send one service operation (the same bank suggestion request retried by
both), which the service charges once; a row per user counted that charge once per
user. The usage of an operation is now one row, attributed to the user who sent it
first. The rows of an operation merge into its earliest row (by creation time, then id),
which keeps its id and user and takes the strongest charge evidence of the group:

- a settled row present: its charge, settled. Every settled row of one operation names
  the same whole charge; should two disagree, the larger is kept, so no charge the
  service named is lost.
- every row pending: the largest charge reported, still pending (the pending rows of
  one operation report the same replies, so a sum would count them twice), unresolved
  only when every row is, so the reconciler still asks while any row could be confirmed.

The merged row's reconcile_at is the latest of the group, the last time any of its rows
was to be asked about. Operations with a single row are left as they are.

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
        "UPDATE ai_usage u SET credits = g.credits, pending = g.pending, unresolved = g.unresolved, "
        "reconcile_at = g.reconcile_at "
        f"FROM ({_EARLIEST}) e, (SELECT company_id, operation_id, "
        "COALESCE(MAX(credits) FILTER (WHERE NOT pending), MAX(credits)) AS credits, "
        "bool_and(pending) AS pending, bool_and(unresolved) AS unresolved, MAX(reconcile_at) AS reconcile_at "
        "FROM ai_usage GROUP BY company_id, operation_id HAVING count(*) > 1) g "
        "WHERE u.id = e.id AND g.company_id = e.company_id AND g.operation_id = e.operation_id")
    op.execute(f"DELETE FROM ai_usage u WHERE u.id NOT IN (SELECT id FROM ({_EARLIEST}) e)")
    op.drop_constraint("uq_ai_usage_operation", "ai_usage", type_="unique")
    op.create_unique_constraint("uq_ai_usage_operation", "ai_usage", ["company_id", "operation_id"])


def downgrade() -> None:
    op.drop_constraint("uq_ai_usage_operation", "ai_usage", type_="unique")
    op.create_unique_constraint("uq_ai_usage_operation", "ai_usage", ["company_id", "user_id", "operation_id"])

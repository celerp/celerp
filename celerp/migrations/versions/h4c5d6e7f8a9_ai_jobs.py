# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One job table for every AI task: ai_batch_jobs gains what a question job needs.

kind (documents | question), mode (interactive | background), question_id,
started_at, promoted_at and cancel_requested_at. The steps of a job are kept in
ai_job_steps, which is created at startup with the assistant's other tables. The
assistant's tables are created at startup, not by a revision, so a database that
has never started the app has no ai_batch_jobs to change. Existing rows are reading
jobs that run in the background.

Revision ID: h4c5d6e7f8a9
Revises: h2a3b4c5d6e7
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "h4c5d6e7f8a9"
down_revision = "h2a3b4c5d6e7"
branch_labels = None
depends_on = None

def _columns() -> list[sa.Column]:
    return [
        sa.Column("kind", sa.String(16), nullable=False, server_default="documents"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="background"),
        sa.Column("question_id", sa.Uuid(as_uuid=True), nullable=True),
        *(sa.Column(name, sa.DateTime(timezone=True), nullable=True)
          for name in ("started_at", "promoted_at", "cancel_requested_at")),
    ]


def upgrade() -> None:
    """Adds the columns a table created at startup from the current model lacks, so
    running it on either kind of table leaves the same schema."""
    inspector = inspect(op.get_bind())
    if not inspector.has_table("ai_batch_jobs"):
        return
    present = {c["name"] for c in inspector.get_columns("ai_batch_jobs")}
    for column in _columns():
        if column.name not in present:
            op.add_column("ai_batch_jobs", column)


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if inspector.has_table("ai_job_steps"):
        op.drop_table("ai_job_steps")
    if not inspector.has_table("ai_batch_jobs"):
        return
    present = {c["name"] for c in inspector.get_columns("ai_batch_jobs")}
    for column in _columns():
        if column.name in present:
            op.drop_column("ai_batch_jobs", column.name)

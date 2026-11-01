# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""ai_batch_jobs.lang: the language of whoever last asked the job, which its messages
are written in, so a job resumed after a restart keeps it. Every existing job starts
in English, the language its messages were written in until now. The assistant's
tables are created at startup, not by a revision, so a database that has never
started the app has no ai_batch_jobs to change, and one created from the current
model already has the column.

Revision ID: m8a9b0c1d2e3
Revises: l7f8a9b0c1d2
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "m8a9b0c1d2e3"
down_revision = "l7f8a9b0c1d2"
branch_labels = None
depends_on = None


def _has_lang() -> bool | None:
    """Whether ai_batch_jobs has the column, or None when there is no table."""
    inspector = inspect(op.get_bind())
    if not inspector.has_table("ai_batch_jobs"):
        return None
    return any(c["name"] == "lang" for c in inspector.get_columns("ai_batch_jobs"))


def upgrade() -> None:
    if _has_lang() is False:
        op.add_column("ai_batch_jobs", sa.Column("lang", sa.Text(), nullable=False, server_default="en"))


def downgrade() -> None:
    if _has_lang():
        op.drop_column("ai_batch_jobs", "lang")

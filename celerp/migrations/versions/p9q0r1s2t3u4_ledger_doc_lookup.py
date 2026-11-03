# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Index per-document journal references without blocking ordinary ledger writes.

Revision ID: p9q0r1s2t3u4
Revises: l7f8a9b0c1d2
Create Date: 2026-11-03
"""
from alembic import op
import sqlalchemy as sa

revision = "p9q0r1s2t3u4"
down_revision = "l7f8a9b0c1d2"
branch_labels = None
depends_on = None

_NAME = "ix_ledger_je_doc_refs_gin"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        valid = connection.execute(sa.text(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
        ), {"name": _NAME}).scalar()
        if valid is False:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NAME}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_NAME} "
            "ON ledger USING gin ((metadata::jsonb) jsonb_path_ops) "
            "WHERE event_type = 'acc.journal_entry.created'"
        )
        valid = connection.execute(sa.text(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
        ), {"name": _NAME}).scalar()
        if valid is not True:
            raise RuntimeError("Ledger document reference index was not created successfully")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NAME}")

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Index per-document journal references without blocking ledger writes.

Revision ID: p9q0r1s2t3u4
Revises: s2t3u4v5w6x7
Create Date: 2026-11-03
"""
from alembic import op
import sqlalchemy as sa

revision = "p9q0r1s2t3u4"
down_revision = "s2t3u4v5w6x7"
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
            op.drop_index(_NAME, table_name="ledger", postgresql_concurrently=True)
        if valid is not True:
            op.create_index(
                "ix_ledger_je_doc_refs_gin", "ledger",
                [sa.text("(metadata::jsonb) jsonb_path_ops")],
                postgresql_using="gin",
                postgresql_where=sa.text("event_type = 'acc.journal_entry.created'"),
                postgresql_concurrently=True,
            )
        valid = connection.execute(sa.text(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
        ), {"name": _NAME}).scalar()
        if valid is not True:
            raise RuntimeError("Ledger document reference index was not created successfully")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        present = op.get_bind().execute(sa.text(
            "SELECT to_regclass(:name)"
        ), {"name": _NAME}).scalar()
        if present is not None:
            op.drop_index(_NAME, table_name="ledger", postgresql_concurrently=True)

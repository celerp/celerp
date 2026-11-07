# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Indexes on the numbers a Document or List shows, keeps and is stored under, ignoring case,
so numbering finds who holds a number without reading every record of the company.

A start of this version before the upgrade ran has already created the indexes from the
models, so each is created only when missing.

Revision ID: q0r1s2t3u4v5
Revises: p9q0r1s2t3u4
Create Date: 2026-11-07
"""

from alembic import op

revision = "q0r1s2t3u4v5"
down_revision = "p9q0r1s2t3u4"
branch_labels = None
depends_on = None

_INDEXES = {
    "ix_projections_number_shown": "lower(state ->> 'ref_id')",
    "ix_projections_number_kept": "lower(state ->> 'doc_number')",
    "ix_projections_number_key": "lower(entity_id)",
}


def upgrade() -> None:
    for name, expression in _INDEXES.items():
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON projections (company_id, ({expression}))")


def downgrade() -> None:
    for name in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")

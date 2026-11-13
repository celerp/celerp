# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Indexes on the numbers a Document or List shows, keeps and is stored under, ignoring case,
so numbering finds who holds a number without reading every record of the company.

A start of this version before the upgrade ran has already created the indexes from the
models, so a valid index is kept. Each is built on its own without blocking writes to projections,
and an INVALID index of the same name, what a cancelled concurrent build leaves, is rebuilt: a
query never uses an invalid index, so keeping it would leave numbering reading every record. A
build that fails drops only that index and the upgrade finishes (build_lookup_index).

Revision ID: q0r1s2t3u4v5
Revises: p9q0r1s2t3u4
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa

from celerp.migrations._lookup_index import build_lookup_index

revision = "q0r1s2t3u4v5"
down_revision = "p9q0r1s2t3u4"
branch_labels = None
depends_on = None

_NAMES = ("ix_projections_number_shown", "ix_projections_number_kept", "ix_projections_number_key")


def upgrade() -> None:
    # Each index is written out so the restamp walker proves the revision by its indexes.
    with op.get_context().autocommit_block():
        build_lookup_index("ix_projections_number_shown", "projections", lambda: op.create_index(
            "ix_projections_number_shown", "projections",
            ["company_id", sa.text("lower(state ->> 'ref_id')")], postgresql_concurrently=True))
        build_lookup_index("ix_projections_number_kept", "projections", lambda: op.create_index(
            "ix_projections_number_kept", "projections",
            ["company_id", sa.text("lower(state ->> 'doc_number')")], postgresql_concurrently=True))
        build_lookup_index("ix_projections_number_key", "projections", lambda: op.create_index(
            "ix_projections_number_key", "projections",
            ["company_id", sa.text("lower(entity_id)")], postgresql_concurrently=True))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _NAMES:
            op.drop_index(name, table_name="projections", postgresql_concurrently=True, if_exists=True)

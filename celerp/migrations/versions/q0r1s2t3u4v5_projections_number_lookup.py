# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Indexes on the numbers a Document or List shows, keeps and is stored under, ignoring case,
so numbering finds who holds a number without reading every record of the company.

A start of this version before the upgrade ran has already created the indexes from the
models, so a valid index is kept. Each is built without blocking writes to projections, and an
INVALID index of the same name, what a cancelled concurrent build leaves, is rebuilt: a query
never uses an invalid index, so keeping it would leave numbering reading every record.

Revision ID: q0r1s2t3u4v5
Revises: p9q0r1s2t3u4
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa

revision = "q0r1s2t3u4v5"
down_revision = "p9q0r1s2t3u4"
branch_labels = None
depends_on = None

_VALID = sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)")
_NAMES = ("ix_projections_number_shown", "ix_projections_number_kept", "ix_projections_number_key")


def _missing(name: str) -> bool:
    """Whether *name* has to be built: no valid index of that name. An invalid one is dropped."""
    valid = op.get_bind().execute(_VALID, {"name": name}).scalar()
    if valid is False:
        op.drop_index(name, table_name="projections", postgresql_concurrently=True)
    return valid is not True


def upgrade() -> None:
    # Each index is written out so the restamp walker proves the revision by its indexes.
    with op.get_context().autocommit_block():
        if _missing("ix_projections_number_shown"):
            op.create_index("ix_projections_number_shown", "projections",
                            ["company_id", sa.text("lower(state ->> 'ref_id')")], postgresql_concurrently=True)
        if _missing("ix_projections_number_kept"):
            op.create_index("ix_projections_number_kept", "projections",
                            ["company_id", sa.text("lower(state ->> 'doc_number')")], postgresql_concurrently=True)
        if _missing("ix_projections_number_key"):
            op.create_index("ix_projections_number_key", "projections",
                            ["company_id", sa.text("lower(entity_id)")], postgresql_concurrently=True)
        unbuilt = [name for name in _NAMES if op.get_bind().execute(_VALID, {"name": name}).scalar() is not True]
        if unbuilt:
            raise RuntimeError(f"Indexes {unbuilt} were not created successfully")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _NAMES:
            op.drop_index(name, table_name="projections", postgresql_concurrently=True, if_exists=True)

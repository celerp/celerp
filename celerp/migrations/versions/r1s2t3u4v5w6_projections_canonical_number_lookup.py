# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Numbering compares numbers in their canonical form (lowercase, without ASCII spaces around
them, and a storage key by the number after its colon), so a record stored with a padded number
still holds it: every Document and List number an earlier version saved with spaces of any kind
around it is first stored stripped, a key it saved so with the number it holds kept stripped in
the record (strip_numbers), then the number lookup indexes are rebuilt on the canonical
expressions.

A start of this version before the upgrade ran has already created the new indexes from the
models, so a valid one is kept. Each is built on its own without blocking writes to projections,
and an INVALID index of the same name, what a cancelled concurrent build leaves, is rebuilt. Once
all three are valid, the indexes on the plain lowercase expressions are dropped: no query reads
them any more. A build that fails drops only that index and the upgrade finishes
(build_lookup_index); the old indexes then stay, so numbering keeps an index until the next start
builds the missing one.

Revision ID: r1s2t3u4v5w6
Revises: o0c1d2e3f4a5
Create Date: 2026-11-10
"""

import logging

from alembic import op
import sqlalchemy as sa

from celerp.migrations._json_compat import update_projection_state
from celerp.migrations._lookup_index import build_lookup_index
from celerp.models.projections import NUMBER_KEPT, NUMBER_KEYED, NUMBER_SHOWN, strip_numbers

revision = "r1s2t3u4v5w6"
down_revision = "o0c1d2e3f4a5"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

_UNTRIMMED = ("ix_projections_number_shown", "ix_projections_number_kept", "ix_projections_number_key")
_CANONICAL = ("ix_projections_canonical_number_shown", "ix_projections_canonical_number_kept",
              "ix_projections_canonical_number_key")


def _strip_numbers(state: dict, row) -> bool:
    """Store each number of ``state`` as a replay of its history stores it (strip_numbers)."""
    return strip_numbers(state, row["entity_id"])


def upgrade() -> None:
    update_projection_state(op.get_bind(), _strip_numbers, where="entity_type IN ('doc', 'list')",
                            extra_cols=("entity_id",))
    # Each index is written out so the restamp walker proves the revision by its indexes.
    with op.get_context().autocommit_block():
        built = [
            build_lookup_index("ix_projections_canonical_number_shown", "projections", lambda: op.create_index(
                "ix_projections_canonical_number_shown", "projections", ["company_id", NUMBER_SHOWN],
                postgresql_concurrently=True)),
            build_lookup_index("ix_projections_canonical_number_kept", "projections", lambda: op.create_index(
                "ix_projections_canonical_number_kept", "projections", ["company_id", NUMBER_KEPT],
                postgresql_concurrently=True)),
            build_lookup_index("ix_projections_canonical_number_key", "projections", lambda: op.create_index(
                "ix_projections_canonical_number_key", "projections", ["company_id", NUMBER_KEYED],
                postgresql_concurrently=True)),
        ]
        if not all(built):
            return
        # The superseded indexes go only once their replacements are valid. One that cannot be
        # dropped costs index upkeep, never a lookup, so it is logged and the upgrade finishes.
        for name in _UNTRIMMED:
            try:
                op.drop_index(name, table_name="projections", postgresql_concurrently=True, if_exists=True)
            except Exception as exc:
                log.warning("Could not drop the superseded index %s (%s)", name, exc)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index("ix_projections_number_shown", "projections",
                        ["company_id", sa.text("lower(state ->> 'ref_id')")], postgresql_concurrently=True,
                        if_not_exists=True)
        op.create_index("ix_projections_number_kept", "projections",
                        ["company_id", sa.text("lower(state ->> 'doc_number')")], postgresql_concurrently=True,
                        if_not_exists=True)
        op.create_index("ix_projections_number_key", "projections",
                        ["company_id", sa.text("lower(entity_id)")], postgresql_concurrently=True,
                        if_not_exists=True)
        for name in _CANONICAL:
            op.drop_index(name, table_name="projections", postgresql_concurrently=True, if_exists=True)

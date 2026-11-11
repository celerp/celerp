# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Numbering compares numbers in their canonical form (lowercase, without ASCII spaces around
them, and a storage key by the number after its colon), so a record stored with a padded number
still holds it: every Document and List number an earlier version saved with spaces of any kind
around it is first stored stripped, a key it saved so with the number it holds kept stripped in
the record (strip_numbers), then the number lookup indexes are rebuilt on the canonical
expressions.

A start of this version before the upgrade ran has already created the new indexes from the
models, so a valid one is kept. Each is built without blocking writes to projections, and an
INVALID index of the same name, what a cancelled concurrent build leaves, is rebuilt. Once all
three are valid, the indexes on the plain lowercase expressions are dropped: no query reads them
any more. A build that fails is logged and dropped and the upgrade finishes (_abandon); the old
indexes then stay, so numbering keeps an index until the next start builds the new ones.

Revision ID: r1s2t3u4v5w6
Revises: o0c1d2e3f4a5
Create Date: 2026-11-10
"""

import logging

from alembic import op
import sqlalchemy as sa

from celerp.migrations._json_compat import update_projection_state
from celerp.models.projections import NUMBER_KEPT, NUMBER_KEYED, NUMBER_SHOWN, strip_numbers

revision = "r1s2t3u4v5w6"
down_revision = "o0c1d2e3f4a5"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

_VALID = sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)")
_NAMES = ("ix_projections_canonical_number_shown", "ix_projections_canonical_number_kept",
          "ix_projections_canonical_number_key")
_UNTRIMMED = ("ix_projections_number_shown", "ix_projections_number_kept", "ix_projections_number_key")


def _missing(name: str) -> bool:
    """Whether *name* has to be built: no valid index of that name. An invalid one is dropped."""
    valid = op.get_bind().execute(_VALID, {"name": name}).scalar()
    if valid is False:
        op.drop_index(name, table_name="projections", postgresql_concurrently=True)
    return valid is not True


def _abandon(reason: str) -> None:
    """A lookup index never stops Celerp starting: log why the build stopped, drop what it left
    (each drop on its own, so one that fails leaves the others tried) and let the upgrade finish.
    Lookups read without the indexes (slower, same rows) until the next start builds them again."""
    log.warning("Indexes %s were not built (%s); they are built again at the next start", list(_NAMES), reason)
    for name in _NAMES:
        try:
            op.drop_index(name, table_name="projections", postgresql_concurrently=True, if_exists=True)
        except Exception as exc:
            log.warning("Could not drop the unfinished index %s (%s)", name, exc)


def _strip_numbers(state: dict, row) -> bool:
    """Store each number of ``state`` as a replay of its history stores it (strip_numbers)."""
    return strip_numbers(state, row["entity_id"])


def upgrade() -> None:
    update_projection_state(op.get_bind(), _strip_numbers, where="entity_type IN ('doc', 'list')",
                            extra_cols=("entity_id",))
    # Each index is written out so the restamp walker proves the revision by its indexes.
    with op.get_context().autocommit_block():
        try:
            if _missing("ix_projections_canonical_number_shown"):
                op.create_index("ix_projections_canonical_number_shown", "projections",
                                ["company_id", NUMBER_SHOWN],
                                postgresql_concurrently=True)
            if _missing("ix_projections_canonical_number_kept"):
                op.create_index("ix_projections_canonical_number_kept", "projections",
                                ["company_id", NUMBER_KEPT],
                                postgresql_concurrently=True)
            if _missing("ix_projections_canonical_number_key"):
                op.create_index("ix_projections_canonical_number_key", "projections",
                                ["company_id", NUMBER_KEYED], postgresql_concurrently=True)
            unbuilt = [name for name in _NAMES if op.get_bind().execute(_VALID, {"name": name}).scalar() is not True]
        except Exception as exc:
            _abandon(repr(exc))
            return
        if unbuilt:
            _abandon(f"left invalid: {unbuilt}")
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
        for name in _NAMES:
            op.drop_index(name, table_name="projections", postgresql_concurrently=True, if_exists=True)

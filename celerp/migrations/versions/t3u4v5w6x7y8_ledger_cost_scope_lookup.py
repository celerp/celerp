# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Index the journal entries a record's cost postings are keyed under (``je:auto:<id>:cost:...``),
so GL Impact finds them by prefix instead of reading every journal entry of the company.

``text_pattern_ops`` lets the prefix match use the index under any collation: a database made
with the machine's locale (embedded PostgreSQL) cannot serve LIKE from a plain btree. A start of
this version before the upgrade ran has already created the index from the model, so a valid one
is kept. It is built without blocking ledger writes, and an INVALID index of the same name, what
a cancelled concurrent build leaves, is rebuilt. A build that fails is logged and dropped and the
upgrade finishes: the lookup reads without it (slower, same rows) until the next start builds it.

Revision ID: t3u4v5w6x7y8
Revises: r1s2t3u4v5w6
Create Date: 2026-11-11
"""

import logging

from alembic import op
import sqlalchemy as sa

revision = "t3u4v5w6x7y8"
down_revision = "r1s2t3u4v5w6"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

_NAME = "ix_ledger_je_cost_scope"
_VALID = sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)")


def _missing(name: str) -> bool:
    """Whether *name* has to be built: no valid index of that name. An invalid one is dropped."""
    valid = op.get_bind().execute(_VALID, {"name": name}).scalar()
    if valid is False:
        op.drop_index(name, table_name="ledger", postgresql_concurrently=True)
    return valid is not True


def upgrade() -> None:
    # The index is written out so the restamp walker proves the revision by it.
    with op.get_context().autocommit_block():
        try:
            if _missing("ix_ledger_je_cost_scope"):
                op.create_index("ix_ledger_je_cost_scope", "ledger", ["company_id", "entity_id"],
                                postgresql_ops={"entity_id": "text_pattern_ops"},
                                postgresql_where=sa.text("event_type = 'acc.journal_entry.created'"),
                                postgresql_concurrently=True)
            reason = None if op.get_bind().execute(_VALID, {"name": _NAME}).scalar() is True \
                else "the build left it invalid"
        except Exception as exc:
            reason = repr(exc)
        if reason is None:
            return
        log.warning("Index %s was not built (%s); it is built again at the next start", _NAME, reason)
        try:
            op.drop_index(_NAME, table_name="ledger", postgresql_concurrently=True, if_exists=True)
        except Exception as exc:
            log.warning("Could not drop the unfinished index %s (%s)", _NAME, exc)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(_NAME, table_name="ledger", postgresql_concurrently=True, if_exists=True)

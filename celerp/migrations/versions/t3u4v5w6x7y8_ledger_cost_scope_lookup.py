# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Index the journal entries a record's cost postings are keyed under (``je:auto:<id>:cost:...``),
so GL Impact finds them by prefix instead of reading every journal entry of the company.

``text_pattern_ops`` lets the prefix match use the index under any collation: a database made
with the machine's locale (embedded PostgreSQL) cannot serve LIKE from a plain btree. A start of
this version before the upgrade ran has already created the index from the model, so a valid one
is kept. It is built without blocking ledger writes, and an INVALID index of the same name, what
a cancelled concurrent build leaves, is rebuilt. A build that fails is dropped and the upgrade
finishes (build_lookup_index): the lookup reads without it (slower, same rows) until the next
start builds it.

Revision ID: t3u4v5w6x7y8
Revises: r1s2t3u4v5w6
Create Date: 2026-11-11
"""

from alembic import op
import sqlalchemy as sa

from celerp.migrations._lookup_index import build_lookup_index

revision = "t3u4v5w6x7y8"
down_revision = "r1s2t3u4v5w6"
branch_labels = None
depends_on = None

_NAME = "ix_ledger_je_cost_scope"


def upgrade() -> None:
    # The index is written out so the restamp walker proves the revision by it.
    with op.get_context().autocommit_block():
        build_lookup_index("ix_ledger_je_cost_scope", "ledger", lambda: op.create_index(
            "ix_ledger_je_cost_scope", "ledger", ["company_id", "entity_id"],
            postgresql_ops={"entity_id": "text_pattern_ops"},
            postgresql_where=sa.text("event_type = 'acc.journal_entry.created'"),
            postgresql_concurrently=True))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(_NAME, table_name="ledger", postgresql_concurrently=True, if_exists=True)

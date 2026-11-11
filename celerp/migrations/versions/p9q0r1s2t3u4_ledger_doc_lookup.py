# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Index per-document journal references without blocking ledger writes.

Revision ID: p9q0r1s2t3u4
Revises: s2t3u4v5w6x7
Create Date: 2026-11-03
"""
import logging

from alembic import op
import sqlalchemy as sa

revision = "p9q0r1s2t3u4"
down_revision = "s2t3u4v5w6x7"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)

_NAME = "ix_ledger_je_doc_refs_gin"
_VALID = sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)")


def upgrade() -> None:
    # A lookup index never stops Celerp starting: without it the lookup reads every journal entry
    # of the company (slower, same rows), and the next start finds it missing and builds it again.
    # The index is written out so the restamp walker proves the revision by it.
    with op.get_context().autocommit_block():
        try:
            connection = op.get_bind()
            valid = connection.execute(_VALID, {"name": _NAME}).scalar()
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
            reason = None if connection.execute(_VALID, {"name": _NAME}).scalar() is True \
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
        present = op.get_bind().execute(sa.text(
            "SELECT to_regclass(:name)"
        ), {"name": _NAME}).scalar()
        if present is not None:
            op.drop_index(_NAME, table_name="ledger", postgresql_concurrently=True)

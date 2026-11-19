# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Index a conversation's messages by (conversation_id, created_at, id), the order every read of
a conversation walks newest first, so a page of older actions reads its slice of a long
conversation from the index instead of sorting the whole conversation.

The composite replaces the index on conversation_id alone, which it also serves. It is built
without blocking message writes; a valid one a start of this version already built from the
model is kept. The old index is dropped only once the new one is valid: a build that fails
leaves the old index in place and the next start builds the new one (build_lookup_index). A
database without the AI tables yet has nothing to index: the startup creates them from the model.

Revision ID: y8d9e0f1a2b3
Revises: x7c8d9e0f1a2
Create Date: 2026-11-19
"""

from alembic import op
from sqlalchemy import inspect

from celerp.migrations._lookup_index import build_lookup_index

revision = "y8d9e0f1a2b3"
down_revision = "x7c8d9e0f1a2"
branch_labels = None
depends_on = None

_NAME = "ix_ai_messages_conversation_created"
_OLD = "ix_ai_messages_conversation_id"


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("ai_messages"):
        return
    # The index is written out so the restamp walker proves the revision by it.
    with op.get_context().autocommit_block():
        if build_lookup_index(_NAME, "ai_messages", lambda: op.create_index(
                "ix_ai_messages_conversation_created", "ai_messages", ["conversation_id", "created_at", "id"],
                postgresql_concurrently=True)):
            op.drop_index(_OLD, table_name="ai_messages", postgresql_concurrently=True, if_exists=True)


def downgrade() -> None:
    if not inspect(op.get_bind()).has_table("ai_messages"):
        return
    with op.get_context().autocommit_block():
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_OLD} ON ai_messages (conversation_id)")
        op.drop_index(_NAME, table_name="ai_messages", postgresql_concurrently=True, if_exists=True)

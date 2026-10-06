# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Each user reads notices for themselves: a read receipt per user replaces the one
read flag every user shared.

A personal notice its user had read keeps that state as a receipt. The shared flag
cannot say who read a company-wide notice, so those start unread for everyone once.

The notices table is created from the models at start, not by a revision, so a new
installation has nothing to convert here. A start of this version before the upgrade
ran has already created the receipts table from the models, so it is created only when
missing, and the old flag is converted wherever it still exists. The DDL is plain SQL
on purpose: the stamp repair (celerp.migrations._auto_stamp) must never take a
receipts table made at start as proof this revision ran.

Revision ID: u8j9f0a1b2c3
Revises: t7i8d9e0f1a2
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa

revision = "u8j9f0a1b2c3"
down_revision = "t7i8d9e0f1a2"
branch_labels = None
depends_on = None


def _exists(conn, table: str) -> bool:
    return conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None


def _has_read_flag(conn) -> bool:
    return conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'notifications' AND column_name = 'read'")).first() is not None


def upgrade() -> None:
    conn = op.get_bind()
    if not _exists(conn, "notifications"):
        return
    op.execute("""
        CREATE TABLE IF NOT EXISTS notification_reads (
            notification_id UUID NOT NULL REFERENCES notifications (id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            read_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (notification_id, user_id)
        )
    """)
    if _has_read_flag(conn):
        op.execute(
            "INSERT INTO notification_reads (notification_id, user_id) "
            "SELECT id, user_id FROM notifications WHERE read AND user_id IS NOT NULL "
            "ON CONFLICT DO NOTHING"
        )
        op.execute("ALTER TABLE notifications DROP COLUMN read")


def downgrade() -> None:
    conn = op.get_bind()
    if not _exists(conn, "notification_reads"):
        return
    op.execute("ALTER TABLE notifications ADD COLUMN read BOOLEAN DEFAULT false NOT NULL")
    op.execute(
        "UPDATE notifications SET read = true WHERE user_id IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM notification_reads r WHERE r.notification_id = notifications.id "
        "AND r.user_id = notifications.user_id)"
    )
    op.execute("DROP TABLE notification_reads")

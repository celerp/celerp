# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Each user reads notices for themselves: a read receipt per user replaces the one
read flag every user shared.

A personal notice its user had read keeps that state as a receipt. A company-wide
notice already read stays read for every current member of its company, so old
notices do not come back as unread.

The notices table is created from the models at start, not by a revision, so a new
installation has nothing to convert here. A start of this version before the upgrade
ran has already created the receipts table from the models, so it is created only when
missing, and the old flag is converted wherever it still exists. The stamp repair
(celerp.migrations._auto_stamp) reads the dropped flag: while it is still there this
revision has not run, whatever tables a start created.

Revision ID: w1n2o3p4q5r6
Revises: v0m1n2o3p4q5
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa

revision = "w1n2o3p4q5r6"
down_revision = "v0m1n2o3p4q5"
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
    if not _exists(conn, "notification_reads"):
        op.create_table(
            "notification_reads",
            sa.Column("notification_id", sa.Uuid(),
                      sa.ForeignKey("notifications.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("read_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if _has_read_flag(conn):
        op.execute(
            "INSERT INTO notification_reads (notification_id, user_id) "
            "SELECT id, user_id FROM notifications WHERE read AND user_id IS NOT NULL "
            "ON CONFLICT DO NOTHING"
        )
        op.execute(
            "INSERT INTO notification_reads (notification_id, user_id) "
            "SELECT n.id, uc.user_id FROM notifications n "
            "JOIN user_companies uc ON uc.company_id = n.company_id AND uc.is_active "
            "WHERE n.read AND n.user_id IS NULL "
            "ON CONFLICT DO NOTHING"
        )
        op.drop_column("notifications", "read")


def downgrade() -> None:
    conn = op.get_bind()
    if not _exists(conn, "notification_reads"):
        return
    op.add_column("notifications", sa.Column("read", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute(
        "UPDATE notifications SET read = true WHERE user_id IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM notification_reads r WHERE r.notification_id = notifications.id "
        "AND r.user_id = notifications.user_id)"
    )
    op.drop_table("notification_reads")

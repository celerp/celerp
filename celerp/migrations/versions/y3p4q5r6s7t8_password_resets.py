# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Record every password reset and who still has to be told about it, and drop the
reset-link columns: a password is now reset by a code Celerp Cloud emails, from a
terminal on this computer or by an owner, never by an emailed link.

The two tables are created from the models at start too, so a start of this version
before the upgrade ran has already made them; each is created only when missing. The
columns are dropped wherever they still exist, so running it again does nothing. A
downgrade puts the columns back empty: a reset link sent before the upgrade does not
come back.

Revision ID: y3p4q5r6s7t8
Revises: x2o3p4q5r6s7
Create Date: 2026-10-17
"""

from alembic import op
import sqlalchemy as sa

revision = "y3p4q5r6s7t8"
down_revision = "x2o3p4q5r6s7"
branch_labels = None
depends_on = None

_RESET_COLUMNS = ("reset_token", "reset_token_expires")


def _exists(conn, table: str) -> bool:
    return conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None


def _user_columns(conn) -> set[str]:
    return set(conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'users'")).scalars())


def upgrade() -> None:
    conn = op.get_bind()
    if not _exists(conn, "password_resets"):
        op.create_table(
            "password_resets",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("method", sa.String(16), nullable=False),
            sa.Column("by_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("os_user", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("method IN ('email_code', 'terminal', 'owner')", name="ck_password_resets_method"),
        )
        op.create_index("ix_password_resets_user_id", "password_resets", ["user_id"])
    if not _exists(conn, "password_reset_notices"):
        op.create_table(
            "password_reset_notices",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("reset_id", sa.Uuid(), sa.ForeignKey("password_resets.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("recipient_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.UniqueConstraint("reset_id", "recipient_id"),
        )
        op.create_index("ix_password_reset_notices_recipient_id", "password_reset_notices", ["recipient_id"])
    present = _user_columns(conn)
    for column in _RESET_COLUMNS:
        if column in present:
            op.drop_column("users", column)


def downgrade() -> None:
    op.add_column("users", sa.Column("reset_token", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("reset_token_expires", sa.DateTime(timezone=True), nullable=True))
    op.drop_table("password_reset_notices")
    op.drop_table("password_resets")

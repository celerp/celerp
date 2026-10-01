# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Record the company of every signed-in session.

Revision ID: p3e4f5a6b7c8
Revises: o2d3e4f5a6b7
Create Date: 2026-10-01

Each row of ``session_registry`` now names the company its access token is for, so
removing a company ends exactly that company's sessions and no others. Existing rows
cannot be attributed to a company and are cleared; they only count who is signed in,
and each session registers again when its token is next renewed.
"""

from alembic import op
import sqlalchemy as sa

revision = "p3e4f5a6b7c8"
down_revision = "o2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM session_registry")
    op.add_column("session_registry", sa.Column("company_id", sa.Uuid(), nullable=False))
    op.create_foreign_key("fk_session_registry_company_id", "session_registry", "companies",
                          ["company_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_session_registry_company_id", "session_registry", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_session_registry_company_id", table_name="session_registry")
    op.drop_constraint("fk_session_registry_company_id", "session_registry", type_="foreignkey")
    op.drop_column("session_registry", "company_id")

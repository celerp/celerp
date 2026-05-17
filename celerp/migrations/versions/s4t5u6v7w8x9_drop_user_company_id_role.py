# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""drop user.company_id and user.role - UserCompany is single source of truth

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-05-15

Background: User.company_id + User.role were a redundant copy of UserCompany.
This caused MultipleResultsFound crashes when two companies shared the same
email address. UserCompany is now the sole source of role and company membership.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "s4t5u6v7w8x9"
down_revision = "r3s4t5u6v7w8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Ensure every user has a UserCompany row (migrate any orphans that exist
    #    only in users.company_id/role but lack a user_companies row).
    op.execute(
        """
        INSERT INTO user_companies (id, user_id, company_id, role, is_active)
        SELECT gen_random_uuid(), u.id, u.company_id, u.role, TRUE
        FROM users u
        WHERE NOT EXISTS (
            SELECT 1 FROM user_companies uc
            WHERE uc.user_id = u.id AND uc.company_id = u.company_id
        )
        """
    )

    # 2. Drop the per-company unique constraint on (company_id, email) - will be
    #    replaced with a global unique on email alone.
    op.drop_constraint("uq_user_company_email", "users", type_="unique")

    # 3. Drop the company_id FK + column and the role column.
    op.drop_constraint("users_company_id_fkey", "users", type_="foreignkey")
    op.drop_column("users", "company_id")
    op.drop_column("users", "role")

    # 4. Add global unique constraint on email.
    op.create_unique_constraint("uq_user_email", "users", ["email"])


def downgrade() -> None:
    op.drop_constraint("uq_user_email", "users", type_="unique")
    op.add_column("users", sa.Column("role", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=True))
    # Restore data from user_companies (best-effort: pick first active link per user)
    op.execute(
        """
        UPDATE users u
        SET company_id = uc.company_id,
            role = uc.role
        FROM (
            SELECT DISTINCT ON (user_id) user_id, company_id, role
            FROM user_companies
            ORDER BY user_id, is_active DESC
        ) uc
        WHERE u.id = uc.user_id
        """
    )
    op.alter_column("users", "company_id", nullable=False)
    op.alter_column("users", "role", nullable=False, server_default="user")
    op.create_foreign_key("users_company_id_fkey", "users", "companies", ["company_id"], ["id"])
    op.create_unique_constraint("uq_user_company_email", "users", ["company_id", "email"])

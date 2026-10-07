# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Give the seeded chart's section headers their cash flow sections.

Revision ID: c7f1a2b3d4e5
Revises: u8j9k0l1m2n3
Create Date: 2026-10-01

The cash flow statement used to infer an account's section from its number. It
now reads the account's own section, else the nearest parent account's, else
operating. Seeded charts get the sections the numbers used to imply on the three
headers that carry them, so their statements read as before: non-current assets
investing, non-current liabilities and equity financing. A header the company has
moved, retyped or classified itself is left alone.

Only an existing install needs this; a new chart is seeded with the sections.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7f1a2b3d4e5"
down_revision = "u8j9k0l1m2n3"
branch_labels = None
depends_on = None

# (code, account_type, parent_code, section) as the seeded chart created them.
_HEADERS = (
    ("1200", "asset", "1000", "investing"),
    ("2200", "liability", "2000", "financing"),
    ("3000", "equity", None, "financing"),
)


def upgrade() -> None:
    conn = op.get_bind()
    # to_regclass resolves against the connection's search_path rather than
    # assuming public, so the guard is correct for any schema the migration runs in.
    if not conn.execute(sa.text("SELECT to_regclass('accounts')")).scalar():
        return  # New install: the module seeds the chart with its sections
    for code, account_type, parent_code, section in _HEADERS:
        conn.execute(sa.text(
            "UPDATE accounts SET cash_flow_category = :section"
            " WHERE code = :code AND account_type = :type"
            " AND parent_code IS NOT DISTINCT FROM :parent AND cash_flow_category IS NULL"
        ), {"section": section, "code": code, "type": account_type, "parent": parent_code})


def downgrade() -> None:
    # The earlier statement derived exactly these sections from the numbers, so
    # clearing them restores its behavior without changing any statement.
    conn = op.get_bind()
    if not conn.execute(sa.text("SELECT to_regclass('accounts')")).scalar():
        return
    for code, account_type, parent_code, section in _HEADERS:
        conn.execute(sa.text(
            "UPDATE accounts SET cash_flow_category = NULL"
            " WHERE code = :code AND account_type = :type"
            " AND parent_code IS NOT DISTINCT FROM :parent AND cash_flow_category = :section"
        ), {"section": section, "code": code, "type": account_type, "parent": parent_code})

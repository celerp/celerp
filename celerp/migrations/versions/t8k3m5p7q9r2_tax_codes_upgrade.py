# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Tax codes for companies from before them

Revision ID: t8k3m5p7q9r2
Revises: c4d1e7a2f9b3
Create Date: 2026-11-27

Every company's tax lists gain ids, rate versions and a category; a negative purchase rate
becomes a withholding code; documents that withheld through a negative tax keep what they owe
and show it as withheld; item tax code names become code ids; the company is told what needs
its attention (celerp/localization/upgrade.py). A company already upgraded is left as it is,
so the step can run again. Forward-only data step.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from celerp.localization.upgrade import upgrade_connection
from celerp.migrations._locks import lock_tables

revision = "t8k3m5p7q9r2"
down_revision = "c4d1e7a2f9b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    lock_tables(conn, "companies", "ledger", "projections")
    for (company_id,) in conn.execute(sa.text("SELECT id FROM companies ORDER BY id")).all():
        upgrade_connection(conn, company_id)


def downgrade() -> None:
    pass

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Scope ledger idempotency uniqueness to the company.

Revision ID: y8z9a0b1c2d3
Revises: x7y8z9a0b1c2
Create Date: 2026-06-11

The ledger idempotency_key was globally unique (uq_ledger_idempotency). But
idempotency keys are inherently per-company — different companies legitimately
reuse the same key (e.g. auto-JE keys derived from per-company doc numbers like
"je:doc:PF-2606-0008:invoice.finalized:c"). With a global unique index, the
second company to finalize the same doc number collided, which (combined with
emit_event's rollback-on-conflict) silently rolled back the finalize.

Replace the global unique index with a per-company unique constraint. This is a
safe loosening: the existing data already satisfied the stricter global unique,
so no row conflicts and nothing is rewritten.
"""

from __future__ import annotations

from alembic import op

revision = "y8z9a0b1c2d3"
down_revision = "x7y8z9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_ledger_idempotency", table_name="ledger")
    op.create_unique_constraint(
        "uq_ledger_company_idempotency", "ledger", ["company_id", "idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_ledger_company_idempotency", "ledger", type_="unique")
    op.create_index(
        "uq_ledger_idempotency", "ledger", ["idempotency_key"], unique=True
    )
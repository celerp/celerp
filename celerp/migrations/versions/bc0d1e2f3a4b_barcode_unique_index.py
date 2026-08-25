# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Enforce at-most-one item per (company, barcode) with a partial unique index.

Revision ID: bc0d1e2f3a4b
Revises: b3c4d5e6f7a8
Create Date: 2026-08-25

Context
-------
A barcode identifies at most one physical-lot item per company. Until now that was
only enforced by an application check under a lock; any writer bypassing the lock
(imports, connectors, a future writer) could create duplicates, and legacy databases
may already hold some.

This migration is the final defense: a PostgreSQL partial expression unique index on
``(company_id, state ->> 'barcode')`` for item projections with a non-empty barcode.

It refuses to create the index while incompatible data exists rather than silently
renaming or clearing anything. A preflight scans for duplicate non-empty barcodes,
barcodes over the length limit, SKUs over the length limit, and comma-bearing SKUs,
and stops with an actionable report (company, value, affected entity ids) so an
operator can remediate and re-run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from celerp.inventory_codes import BARCODE_UNIQUE_INDEX, MAX_BARCODE_LEN, MAX_SKU_LEN

revision = "bc0d1e2f3a4b"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def _preflight(conn) -> list[str]:
    """Return a list of human-readable remediation problems; empty means clean."""
    problems: list[str] = []

    dups = conn.execute(
        sa.text(
            "SELECT company_id, state ->> 'barcode' AS barcode, "
            "       array_agg(entity_id ORDER BY entity_id) AS entities "
            "FROM projections "
            "WHERE entity_type = 'item' AND NULLIF(state ->> 'barcode', '') IS NOT NULL "
            "GROUP BY company_id, state ->> 'barcode' "
            "HAVING count(*) > 1"
        )
    ).fetchall()
    for row in dups:
        problems.append(
            f"duplicate barcode {row.barcode!r} in company {row.company_id}: entities {list(row.entities)}"
        )

    long_barcodes = conn.execute(
        sa.text(
            "SELECT company_id, entity_id, state ->> 'barcode' AS barcode "
            "FROM projections "
            "WHERE entity_type = 'item' AND length(state ->> 'barcode') > :n"
        ),
        {"n": MAX_BARCODE_LEN},
    ).fetchall()
    for row in long_barcodes:
        problems.append(
            f"barcode {row.barcode!r} over {MAX_BARCODE_LEN} chars in company {row.company_id} entity {row.entity_id}"
        )

    long_skus = conn.execute(
        sa.text(
            "SELECT company_id, entity_id "
            "FROM projections "
            "WHERE entity_type = 'item' AND length(state ->> 'sku') > :n"
        ),
        {"n": MAX_SKU_LEN},
    ).fetchall()
    for row in long_skus:
        problems.append(
            f"SKU over {MAX_SKU_LEN} chars in company {row.company_id} entity {row.entity_id}"
        )

    comma_skus = conn.execute(
        sa.text(
            "SELECT company_id, entity_id, state ->> 'sku' AS sku "
            "FROM projections "
            "WHERE entity_type = 'item' AND position(',' in state ->> 'sku') > 0"
        )
    ).fetchall()
    for row in comma_skus:
        problems.append(
            f"comma-bearing SKU {row.sku!r} in company {row.company_id} entity {row.entity_id}"
        )

    return problems


def upgrade() -> None:
    conn = op.get_bind()

    problems = _preflight(conn)
    if problems:
        raise RuntimeError(
            "Cannot create the barcode uniqueness index: incompatible item data exists. "
            "Resolve each of the following (rename or clear the barcode/SKU on the affected "
            "items), then re-run the migration:\n  - " + "\n  - ".join(problems)
        )

    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {BARCODE_UNIQUE_INDEX} "
        "ON projections (company_id, (state ->> 'barcode')) "
        "WHERE entity_type = 'item' AND NULLIF(state ->> 'barcode', '') IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {BARCODE_UNIQUE_INDEX}")

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Enforce at-most-one resolvable item per (company, barcode).

Revision ID: bc0d1e2f3a4b
Revises: b3c4d5e6f7a8
Create Date: 2026-08-25

Historical sources with status merged retain their original barcode but are
excluded by the canonical resolver, so they are also excluded from the DB index.
Legacy SQL_ASCII databases omit this JSON expression index because PostgreSQL
cannot safely evaluate it when unrelated escaped Unicode exists in the same JSON.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from celerp.inventory_codes import (
    BARCODE_UNIQUE_INDEX,
    BARCODE_UNIQUE_WHERE,
    LEGACY_BARCODE_UNIQUE_INDEX,
    MAX_BARCODE_LEN,
    MAX_SKU_LEN,
)
from celerp.migrations._json_compat import decode_json_value, is_sql_ascii

revision = "bc0d1e2f3a4b"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def _preflight(conn) -> list[str]:
    problems: list[str] = []
    dups = conn.execute(sa.text(
        "SELECT company_id, state ->> 'barcode' AS barcode, "
        "array_agg(entity_id ORDER BY entity_id) AS entities "
        "FROM projections "
        f"WHERE {BARCODE_UNIQUE_WHERE} "
        "GROUP BY company_id, state ->> 'barcode' HAVING count(*) > 1"
    )).fetchall()
    for row in dups:
        problems.append(
            f"duplicate barcode {row.barcode!r} in company {row.company_id}: entities {list(row.entities)}"
        )

    long_barcodes = conn.execute(sa.text(
        "SELECT company_id, entity_id, state ->> 'barcode' AS barcode FROM projections "
        "WHERE entity_type = 'item' AND length(state ->> 'barcode') > :n"
    ), {"n": MAX_BARCODE_LEN}).fetchall()
    for row in long_barcodes:
        problems.append(
            f"barcode {row.barcode!r} over {MAX_BARCODE_LEN} chars in company {row.company_id} entity {row.entity_id}"
        )

    long_skus = conn.execute(sa.text(
        "SELECT company_id, entity_id FROM projections "
        "WHERE entity_type = 'item' AND length(state ->> 'sku') > :n"
    ), {"n": MAX_SKU_LEN}).fetchall()
    for row in long_skus:
        problems.append(
            f"SKU over {MAX_SKU_LEN} chars in company {row.company_id} entity {row.entity_id}"
        )

    comma_skus = conn.execute(sa.text(
        "SELECT company_id, entity_id, state ->> 'sku' AS sku FROM projections "
        "WHERE entity_type = 'item' AND position(',' in state ->> 'sku') > 0"
    )).fetchall()
    for row in comma_skus:
        problems.append(
            f"comma-bearing SKU {row.sku!r} in company {row.company_id} entity {row.entity_id}"
        )
    return problems


def _json_text(value) -> str | None:
    """Match PostgreSQL json ->> text semantics for the scalar item-code fields."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _preflight_sql_ascii(conn) -> list[str]:
    """The normal preflight without server-side JSON operators."""
    rows = conn.execute(sa.text(
        "SELECT company_id, entity_id, state FROM projections "
        "WHERE entity_type = 'item' ORDER BY company_id, entity_id"
    )).mappings().all()

    barcode_entities: dict[tuple[object, str], list[str]] = {}
    long_barcodes: list[tuple[object, str, str]] = []
    long_skus: list[tuple[object, str]] = []
    comma_skus: list[tuple[object, str, str]] = []

    for row in rows:
        state = decode_json_value(row["state"])
        if not isinstance(state, dict):
            continue

        barcode = _json_text(state.get("barcode"))
        sku = _json_text(state.get("sku"))
        status = (_json_text(state.get("status")) or "").lower()

        if barcode not in (None, "") and status != "merged":
            barcode_entities.setdefault(
                (row["company_id"], barcode), []
            ).append(row["entity_id"])
        if barcode is not None and len(barcode) > MAX_BARCODE_LEN:
            long_barcodes.append((row["company_id"], row["entity_id"], barcode))
        if sku is not None and len(sku) > MAX_SKU_LEN:
            long_skus.append((row["company_id"], row["entity_id"]))
        if sku is not None and "," in sku:
            comma_skus.append((row["company_id"], row["entity_id"], sku))

    problems: list[str] = []
    for (company_id, barcode), entities in barcode_entities.items():
        if len(entities) > 1:
            problems.append(
                f"duplicate barcode {barcode!r} in company {company_id}: entities {entities}"
            )
    for company_id, entity_id, barcode in long_barcodes:
        problems.append(
            f"barcode {barcode!r} over {MAX_BARCODE_LEN} chars in company "
            f"{company_id} entity {entity_id}"
        )
    for company_id, entity_id in long_skus:
        problems.append(
            f"SKU over {MAX_SKU_LEN} chars in company {company_id} entity {entity_id}"
        )
    for company_id, entity_id, sku in comma_skus:
        problems.append(
            f"comma-bearing SKU {sku!r} in company {company_id} entity {entity_id}"
        )
    return problems


def _raise_preflight(problems: list[str]) -> None:
    if problems:
        raise RuntimeError(
            "Cannot create the barcode uniqueness index: incompatible item data exists. "
            "Resolve each of the following (rename or clear the barcode/SKU on the affected "
            "items), then re-run the migration:\n  - " + "\n  - ".join(problems)
        )


def upgrade() -> None:
    conn = op.get_bind()
    if is_sql_ascii(conn):
        _raise_preflight(_preflight_sql_ascii(conn))
        op.execute(f"DROP INDEX IF EXISTS {BARCODE_UNIQUE_INDEX}")
        op.execute(f"DROP INDEX IF EXISTS {LEGACY_BARCODE_UNIQUE_INDEX}")
        return

    _raise_preflight(_preflight(conn))

    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {BARCODE_UNIQUE_INDEX} "
        "ON projections (company_id, (state ->> 'barcode')) "
        f"WHERE {BARCODE_UNIQUE_WHERE}"
    )
    op.execute(f"DROP INDEX IF EXISTS {LEGACY_BARCODE_UNIQUE_INDEX}")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {BARCODE_UNIQUE_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {LEGACY_BARCODE_UNIQUE_INDEX}")

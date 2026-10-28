# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Connectors set a direction for each kind of record instead of one for the whole
connector, and a connection's first sync is something a person starts.

Each existing connection's single direction becomes the same direction for every
record its connector can carry that way, so it keeps running exactly the syncs it
ran before; WooCommerce stops importing its customer list. A connection that has
synced since it was last connected counts its first sync as done from its earliest
run, so its scheduled syncs carry on.

The record kinds are written out here as they stood when this revision was made,
so the upgrade does not change if a connector later carries more.

Revision ID: h2a3b4c5d6e7
Revises: g1z2a3b4c5d6
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "h2a3b4c5d6e7"
down_revision = "g1z2a3b4c5d6"
branch_labels = None
depends_on = None

_IN, _OUT = "inbound", "outbound"
# connector -> record kind -> the ways it can sync.
_FLOWS = {
    "woocommerce": {"products": (_IN, _OUT), "stock": (_OUT,), "orders": (_IN,), "customers": ()},
    "shopify": {"products": (_IN, _OUT), "stock": (_OUT,), "orders": (_IN,), "customers": (_IN,)},
    "quickbooks": {"products": (_IN,), "invoices": (_IN, _OUT), "contacts": (_IN,)},
    "xero": {"products": (_IN,), "invoices": (_IN, _OUT), "contacts": (_IN,)},
    "celerp": {"contacts": (_OUT,), "products": (_OUT,), "documents": (_OUT,),
               "inventory": (_OUT,), "journals": (_IN, _OUT), "attachments": (_OUT,),
               "period_lock": (_IN,)},
}


def _per_record(connector: str, direction: str) -> dict[str, str]:
    wanted = {_IN, _OUT} if direction == "both" else {direction}
    out = {}
    for kind, ways in _FLOWS.get(connector, {}).items():
        kept = wanted & set(ways)
        out[kind] = "both" if kept == {_IN, _OUT} else (next(iter(kept)) if kept else "off")
    return out


def _single(directions: dict) -> str:
    ways = set()
    for value in directions.values():
        ways |= {_IN, _OUT} if value == "both" else ({value} if value in (_IN, _OUT) else set())
    if ways == {_IN}:
        return _IN
    if ways == {_OUT}:
        return _OUT
    return "both"


def upgrade() -> None:
    op.add_column("connector_configs", sa.Column("entity_directions", sa.JSON(), nullable=True))
    op.add_column("connector_configs", sa.Column("orders_from", sa.DateTime(timezone=True), nullable=True))
    op.add_column("connector_configs", sa.Column("connect_lapsed_at", sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    for row_id, connector, direction in bind.execute(sa.text(
            "SELECT id, connector, direction FROM connector_configs")).all():
        bind.execute(sa.text(
            "UPDATE connector_configs SET entity_directions = CAST(:d AS json) WHERE id = :id"
        ), {"d": json.dumps(_per_record(connector, direction)), "id": row_id})
    op.alter_column("connector_configs", "entity_directions", nullable=False)
    op.drop_column("connector_configs", "direction")


def downgrade() -> None:
    op.add_column("connector_configs", sa.Column(
        "direction", sa.String(16), nullable=False, server_default="both"))
    bind = op.get_bind()
    for row_id, directions in bind.execute(sa.text(
            "SELECT id, entity_directions FROM connector_configs")).all():
        values = directions if isinstance(directions, dict) else json.loads(directions)
        bind.execute(sa.text("UPDATE connector_configs SET direction = :d WHERE id = :id"),
                     {"d": _single(values), "id": row_id})
    op.drop_column("connector_configs", "connect_lapsed_at")
    op.drop_column("connector_configs", "orders_from")
    op.drop_column("connector_configs", "entity_directions")

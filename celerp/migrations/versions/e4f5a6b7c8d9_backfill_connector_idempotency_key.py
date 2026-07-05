# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Backfill connector idempotency_key on legacy doc/contact rows + add lookup index.

Revision ID: e4f5a6b7c8d9
Revises: c2d3e4f5a6b7
Create Date: 2026-07-04

Context
-------
`connector_upsert` now resolves the projection a re-import maps to by the stable
``state.idempotency_key`` (rather than minting a fresh entity_id), so an edited
record UPDATES the existing projection instead of creating a duplicate.

Records imported before this scheme stored a random-uuid entity_id:
  - items already carried ``state.idempotency_key`` (e.g. ``shopify:123:456``) — no
    work needed, they resolve as-is;
  - docs and contacts did NOT — but they did record the platform id
    (``state.shopify_order_id`` / ``state.attributes.shopify_id`` etc.).

Reconstruct ``idempotency_key`` from that platform id on both the projection and its
originating ledger event (so a projection rebuild stays consistent), matching exactly
the key format the connectors emit. Then add the functional index the lookup uses.
Without this, the first post-upgrade re-import of a pre-existing Shopify order/customer
would duplicate it.
"""

from __future__ import annotations

from alembic import op

# In-Python edits avoid json->jsonb casts that fail on SQL_ASCII clusters (issue #189).
from celerp.migrations._json_compat import update_ledger_data, update_projection_state

revision = "e4f5a6b7c8d9"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


# Marker field in state -> idempotency_key prefix. Mirrors the idem keys the connectors
# emit (doc_service.upsert_*  /  contacts.services.upsert_contact_*).
_DOC_MARKERS = (
    ("shopify_order_id", "shopify:order:"),
    ("woocommerce_order_id", "woocommerce:order:"),
    ("quickbooks_invoice_id", "quickbooks:invoice:"),
    ("xero_invoice_id", "xero:invoice:"),
)
_CONTACT_MARKERS = (
    ("shopify_id", "shopify:customer:"),
    ("woocommerce_id", "woocommerce:customer:"),
    ("quickbooks_id", "quickbooks:customer:"),
    ("xero_id", "xero:contact:"),
)


def _doc_key(data: dict, _row) -> bool:
    if data.get("idempotency_key"):
        return False
    for marker, prefix in _DOC_MARKERS:
        val = data.get(marker)
        if val not in (None, ""):
            data["idempotency_key"] = f"{prefix}{val}"
            return True
    return False


def _contact_key(data: dict, _row) -> bool:
    if data.get("idempotency_key"):
        return False
    attrs = data.get("attributes") or {}
    for marker, prefix in _CONTACT_MARKERS:
        val = attrs.get(marker)
        if val not in (None, ""):
            data["idempotency_key"] = f"{prefix}{val}"
            return True
    return False


def upgrade() -> None:
    conn = op.get_bind()

    # Backfill the stable key on the projection AND its ledger event (rebuild-safe).
    update_projection_state(conn, _doc_key, where="entity_type = 'doc'")
    update_ledger_data(conn, _doc_key, where="event_type = 'doc.created'")
    update_projection_state(conn, _contact_key, where="entity_type = 'contact'")
    update_ledger_data(conn, _contact_key, where="event_type = 'crm.contact.created'")

    # Index the lookup connector_upsert does on every imported record.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_projections_connector_key "
        "ON projections (company_id, entity_type, (state ->> 'idempotency_key'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_projections_connector_key")

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company-scoped integration grants.

Revision ID: x2p3q4r5s6t7
Revises: y3p4q5r6s7t8
"""
from alembic import op
import sqlalchemy as sa

revision = "x2p3q4r5s6t7"
down_revision = "y3p4q5r6s7t8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_peer_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_company_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_company_peer_grant_hash_length"),
    )
    op.create_index("ix_company_peer_grants_company_id", "company_peer_grants", ["company_id"])
    op.create_table(
        "company_peer_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_company_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("source_entity_id", sa.Text(), nullable=False),
        sa.Column("source_key_hash", sa.String(64), nullable=False),
        sa.Column("target_entity_id", sa.Text(), nullable=False),
        sa.Column("last_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_payload_hash", sa.String(64), nullable=True),
        sa.UniqueConstraint("company_id", "source_company_id", "entity_type", "source_key_hash",
                            name="uq_company_peer_external_identity"),
    )
    op.create_index("ix_company_peer_identities_company", "company_peer_identities", ["company_id"])



def downgrade_refusal(bind) -> str | None:
    # This is a durable identity ledger. An older release cannot preserve the
    # mappings or revoke active grants, so refuse a destructive downgrade.
    for table in ("company_peer_identities", "company_peer_grants"):
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            return "Cannot downgrade while company integrations exist"
    return None


def downgrade() -> None:
    refusal = downgrade_refusal(op.get_bind())
    if refusal:
        raise RuntimeError(refusal)
    op.drop_index("ix_company_peer_identities_company", table_name="company_peer_identities")
    op.drop_table("company_peer_identities")
    op.drop_index("ix_company_peer_grants_company_id", table_name="company_peer_grants")
    op.drop_table("company_peer_grants")

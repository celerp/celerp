"""add bank_statement_lines, reconciliation_rules, extend reconciliation_sessions

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-03-21 08:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "k5l6m7n8o9p0"
down_revision = "j4k5l6m7n8o9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extend reconciliation_sessions with new columns
    op.add_column("reconciliation_sessions", sa.Column("csv_filename", sa.String(255), nullable=True))
    op.add_column("reconciliation_sessions", sa.Column("csv_row_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("reconciliation_sessions", sa.Column("auto_matched_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("reconciliation_sessions", sa.Column("manual_matched_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("reconciliation_sessions", sa.Column("created_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("reconciliation_sessions", sa.Column("tolerance", sa.Numeric(20, 4), nullable=False, server_default="1.00"))
    op.add_column("reconciliation_sessions", sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))

    # Create bank_statement_lines table
    op.create_table(
        "bank_statement_lines",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("reconciliation_id", sa.Uuid(as_uuid=True), sa.ForeignKey("reconciliation_sessions.id"), nullable=False),
        sa.Column("line_date", sa.String(16), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("raw_balance", sa.Numeric(20, 4), nullable=True),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="unmatched"),
        sa.Column("matched_je_id", sa.String(128), nullable=True),
        sa.Column("attachment_ids", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("raw_csv_row", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_stmt_line_recon", "bank_statement_lines", ["reconciliation_id"])
    op.create_index("idx_stmt_line_company", "bank_statement_lines", ["company_id"])

    # Create reconciliation_rules table
    op.create_table(
        "reconciliation_rules",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("bank_account_id", sa.Uuid(as_uuid=True), sa.ForeignKey("bank_accounts.id"), nullable=False),
        sa.Column("match_field", sa.String(32), nullable=False, server_default="description"),
        sa.Column("match_pattern", sa.String(256), nullable=False),
        sa.Column("match_type", sa.String(16), nullable=False, server_default="contains"),
        sa.Column("target_account_code", sa.String(32), nullable=False),
        sa.Column("default_memo", sa.String(256), nullable=True),
        sa.Column("default_tax", sa.String(32), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("times_applied", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_recon_rule_bank", "reconciliation_rules", ["bank_account_id"])
    op.create_index("idx_recon_rule_company", "reconciliation_rules", ["company_id"])


def downgrade() -> None:
    op.drop_index("idx_recon_rule_company", table_name="reconciliation_rules")
    op.drop_index("idx_recon_rule_bank", table_name="reconciliation_rules")
    op.drop_table("reconciliation_rules")

    op.drop_index("idx_stmt_line_company", table_name="bank_statement_lines")
    op.drop_index("idx_stmt_line_recon", table_name="bank_statement_lines")
    op.drop_table("bank_statement_lines")

    for col in ("csv_filename", "csv_row_count", "auto_matched_count", "manual_matched_count",
                "created_count", "tolerance", "imported_at"):
        op.drop_column("reconciliation_sessions", col)

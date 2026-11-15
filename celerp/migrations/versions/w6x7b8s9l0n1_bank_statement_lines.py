# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Bank statement lines that know their bank, their identity and what they may become.

Revision ID: w6x7b8s9l0n1
Revises: v5w6x7y8z9a0

A statement line now belongs to its bank as well as its statement, carries a fingerprint (its
identity on the bank, so a line read twice is refused), the counterparty its description names,
one suggestion, a list of the entries that settle it and the question a firm asked its client
about it. Existing lines get their bank from their statement and a fingerprint numbered among
identical lines of the same bank, so the unique index cannot collide. A line the old matcher
only suggested becomes an open line holding that suggestion; a line whose entry was created
becomes posted; each single matched entry becomes a one-item list.

A statement records the date its period starts, its opening balance and the file it came from.
A rule may hold for every bank, match an amount, name a contact or a target bank, and records
whether a person or an answer made it; the unused default tax goes. A bank account keeps the
column layout its statements were last read with.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from celerp.json_text import dump_json
from celerp.migrations._json_compat import decode_json_value
from celerp.migrations._locks import lock_tables
from celerp.services.field_fingerprints import statement_line, statement_line_ident

revision = "w6x7b8s9l0n1"
down_revision = "v5w6x7y8z9a0"
branch_labels = None
depends_on = None

_LINES = "bank_statement_lines"
_BATCH = 1000


def backfill_fingerprints(rows: list[dict]) -> dict:
    """Each row's fingerprint by id. Identical lines on one bank are numbered in the order of
    their statement date, creation time and id, so no two rows of a bank share one."""
    seen: dict[tuple, int] = {}
    out = {}
    for r in sorted(rows, key=lambda r: (str(r["statement_date"]), str(r["created_at"]), str(r["id"]))):
        base = (str(r["bank_account_id"]), r["line_date"], r["amount"], r["description"], r["reference"], r["raw_balance"])
        ident = (base[0], *statement_line_ident(*base[1:]))
        n = seen.get(ident, 0)
        seen[ident] = n + 1
        out[r["id"]] = statement_line(*base, n)
    return out


def migrated_line(row: dict) -> dict:
    """The status, entry list and suggestion an existing line moves to."""
    je = row.get("matched_je_id")
    if row["status"] == "suggested":
        return {"status": "unmatched", "matched_je_ids": [],
                "suggestion": {"kind": "match", "je_ids": [je] if je else [], "source": "match"}}
    status = "posted" if row["status"] == "created" else row["status"]
    return {"status": status, "matched_je_ids": [je] if je else [], "suggestion": None}


def _backfill_lines(bind) -> None:
    rows = [dict(r) for r in bind.execute(sa.text(
        f"SELECT l.id, l.status, l.matched_je_id, l.line_date, l.amount, l.description, l.reference, l.raw_balance, "
        f"l.created_at, s.bank_account_id, s.statement_date FROM {_LINES} l "
        "JOIN reconciliation_sessions s ON s.id = l.reconciliation_id")).mappings()]
    fps = backfill_fingerprints(rows)
    write = sa.text(
        f"UPDATE {_LINES} SET bank_account_id = :bank, fingerprint = :fp, status = :status, "
        "matched_je_ids = CAST(:je_ids AS json), suggestion = CAST(:suggestion AS json) WHERE id = :id")
    params = []
    for r in rows:
        m = migrated_line(r)
        params.append({"id": r["id"], "bank": r["bank_account_id"], "fp": fps[r["id"]], "status": m["status"],
                       "je_ids": dump_json(m["matched_je_ids"]),
                       "suggestion": dump_json(m["suggestion"]) if m["suggestion"] else None})
    for i in range(0, len(params), _BATCH):
        bind.execute(write, params[i:i + _BATCH])


def upgrade() -> None:
    bind = op.get_bind()
    # The backfill's bank id is a foreign key: its check takes each bank account row.
    lock_tables(bind, _LINES, "reconciliation_rules", "bank_accounts")

    op.add_column("bank_statement_lines", sa.Column("bank_account_id", sa.Uuid(as_uuid=True), sa.ForeignKey("bank_accounts.id"), nullable=True))
    op.add_column("bank_statement_lines", sa.Column("fingerprint", sa.String(16), nullable=True))
    op.add_column("bank_statement_lines", sa.Column("counterparty_key", sa.String(128), nullable=True))
    op.add_column("bank_statement_lines", sa.Column("counterparty_account", sa.String(64), nullable=True))
    op.add_column("bank_statement_lines", sa.Column("suggestion", sa.JSON, nullable=True))
    op.add_column("bank_statement_lines", sa.Column("matched_je_ids", sa.JSON, nullable=False, server_default="[]"))
    op.add_column("bank_statement_lines", sa.Column("client_query", sa.JSON, nullable=True))
    _backfill_lines(bind)
    op.alter_column("bank_statement_lines", "bank_account_id", nullable=False)
    op.alter_column("bank_statement_lines", "fingerprint", nullable=False)
    op.drop_column("bank_statement_lines", "matched_je_id")
    op.create_index("uq_stmt_line_fingerprint", "bank_statement_lines", ["company_id", "bank_account_id", "fingerprint"], unique=True)

    op.add_column("reconciliation_sessions", sa.Column("period_start", sa.String(16), nullable=True))
    op.add_column("reconciliation_sessions", sa.Column("opening_balance", sa.Numeric(20, 4), nullable=True))
    op.add_column("reconciliation_sessions", sa.Column("source_file_id", sa.String(128), nullable=True))
    op.drop_column("reconciliation_sessions", "auto_matched_count")

    op.alter_column("reconciliation_rules", "bank_account_id", nullable=True)
    op.add_column("reconciliation_rules", sa.Column("match_amount", sa.Numeric(20, 4), nullable=True))
    op.add_column("reconciliation_rules", sa.Column("contact_id", sa.String(128), nullable=True))
    op.add_column("reconciliation_rules", sa.Column("target_bank_id", sa.Uuid(as_uuid=True), sa.ForeignKey("bank_accounts.id"), nullable=True))
    op.add_column("reconciliation_rules", sa.Column("origin", sa.String(16), nullable=False, server_default="user"))
    op.drop_column("reconciliation_rules", "default_tax")

    op.add_column("bank_accounts", sa.Column("statement_column_map", sa.JSON, nullable=True))


def _restore_lines(bind) -> None:
    """The single entry, and the suggested and created statuses, as the earlier build reads them."""
    rows = bind.execute(sa.text(f"SELECT id, status, matched_je_ids, suggestion FROM {_LINES}")).mappings().all()
    write = sa.text(f"UPDATE {_LINES} SET status = :status, matched_je_id = :je WHERE id = :id")
    params = []
    for r in rows:
        je_ids = decode_json_value(r["matched_je_ids"]) or []
        suggestion = decode_json_value(r["suggestion"]) or {}
        status, je = r["status"], (je_ids[0] if je_ids else None)
        if status == "posted":
            status = "created"
        elif status == "unmatched" and suggestion.get("kind") == "match" and suggestion.get("je_ids"):
            status, je = "suggested", suggestion["je_ids"][0]
        params.append({"id": r["id"], "status": status, "je": je})
    for i in range(0, len(params), _BATCH):
        bind.execute(write, params[i:i + _BATCH])


def downgrade_refusal(bind) -> str | None:
    """The earlier schema holds one bank per rule and one entry per line, so a rule for every
    bank or a line settled by several entries would be lost: either stops the downgrade."""
    if bind.execute(sa.text("SELECT 1 FROM reconciliation_rules WHERE bank_account_id IS NULL LIMIT 1")).first():
        return "Cannot downgrade while a reconciliation rule holds for every bank"
    if bind.execute(sa.text(f"SELECT 1 FROM {_LINES} WHERE json_array_length(matched_je_ids) > 1 LIMIT 1")).first():
        return "Cannot downgrade while a statement line is settled by several entries"
    return None


def downgrade() -> None:
    bind = op.get_bind()
    refusal = downgrade_refusal(bind)
    if refusal:
        raise RuntimeError(refusal)
    lock_tables(bind, _LINES, "reconciliation_rules")
    op.drop_column("bank_accounts", "statement_column_map")

    op.add_column("reconciliation_rules", sa.Column("default_tax", sa.String(32), nullable=True))
    for col in ("origin", "target_bank_id", "contact_id", "match_amount"):
        op.drop_column("reconciliation_rules", col)
    op.alter_column("reconciliation_rules", "bank_account_id", nullable=False)

    op.add_column("reconciliation_sessions", sa.Column("auto_matched_count", sa.Integer, nullable=False, server_default="0"))
    for col in ("source_file_id", "opening_balance", "period_start"):
        op.drop_column("reconciliation_sessions", col)

    op.drop_index("uq_stmt_line_fingerprint", table_name="bank_statement_lines")
    op.add_column("bank_statement_lines", sa.Column("matched_je_id", sa.String(128), nullable=True))
    _restore_lines(bind)
    for col in ("client_query", "matched_je_ids", "suggestion", "counterparty_account", "counterparty_key",
                "fingerprint", "bank_account_id"):
        op.drop_column("bank_statement_lines", col)

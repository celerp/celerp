# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Statement lines from before the bank statement revision upgrade with their bank, a
fingerprint unique on the bank, their entry as a list and a suggested match kept as a
suggestion, and go back down to the shape the earlier build reads."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import create_engine, text

from .conftest import throwaway_db, upgrade_to

_REV, _PREV = "w6x7b8s9l0n1", "v5w6x7y8z9a0"


def _seed(conn) -> tuple[str, list[str]]:
    cid, bank, sess = (str(uuid.uuid4()) for _ in range(3))
    conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
                      "VALUES (:id, 'Co', :slug, CAST('{}' AS json), true, now())"), {"id": cid, "slug": f"co-{cid[:8]}"})
    conn.execute(text("INSERT INTO bank_accounts (id, company_id, chart_account_code, bank_name, account_number, "
                      "bank_type, currency, opening_balance, is_active, created_at) "
                      "VALUES (:id, :c, '1111', 'Bank', '****1234', 'checking', 'USD', 0, true, now())"), {"id": bank, "c": cid})
    conn.execute(text("INSERT INTO reconciliation_sessions (id, company_id, bank_account_id, statement_date, "
                      "statement_balance, status, reconciled_je_ids, created_at) "
                      "VALUES (:id, :c, :b, '2026-01-31', 0, 'open', CAST('[]' AS json), now())"),
                 {"id": sess, "c": cid, "b": bank})
    ids = []
    for status, je in (("unmatched", None), ("unmatched", None), ("suggested", "je:7"), ("created", "je:9")):
        lid = str(uuid.uuid4())
        ids.append(lid)
        conn.execute(text("INSERT INTO bank_statement_lines (id, company_id, reconciliation_id, line_date, description, "
                          "amount, status, matched_je_id, created_at) VALUES (:id, :c, :s, '2026-01-02', 'ATM FEE', -3, "
                          ":st, :je, now())"), {"id": lid, "c": cid, "s": sess, "st": status, "je": je})
    return bank, ids


def _lines(conn, ids: list[str], cols: str) -> list:
    return [conn.execute(text(f"SELECT {cols} FROM bank_statement_lines WHERE id = :id"), {"id": i}).one() for i in ids]


def test_lines_upgrade_and_downgrade():
    with throwaway_db("bankstmt") as (_, sync_url):
        upgrade_to(sync_url, _PREV)
        eng = create_engine(sync_url)
        try:
            with eng.begin() as conn:
                bank, ids = _seed(conn)
            upgrade_to(sync_url, _REV)
            with eng.connect() as conn:
                up = _lines(conn, ids, "bank_account_id, fingerprint, status, matched_je_ids, suggestion")
            from alembic_support import alembic_run
            alembic_run(sync_url, "downgrade", _PREV)
            with eng.connect() as conn:
                down = _lines(conn, ids, "status, matched_je_id")
        finally:
            eng.dispose()

    def js(v):
        return json.loads(v) if isinstance(v, str) else v
    assert {str(r[0]) for r in up} == {bank}
    assert len({r[1] for r in up}) == 4
    assert [(r[2], js(r[3])) for r in up] == [("unmatched", []), ("unmatched", []), ("unmatched", []), ("posted", ["je:9"])]
    assert js(up[2][4]) == {"kind": "match", "je_ids": ["je:7"], "source": "match"}
    assert [tuple(r) for r in down] == [("unmatched", None), ("unmatched", None), ("suggested", "je:7"), ("created", "je:9")]

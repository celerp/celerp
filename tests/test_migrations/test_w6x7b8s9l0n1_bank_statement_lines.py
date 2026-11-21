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


def _whole_table_numbering(rows: list[dict]) -> dict:
    """The numbering read over every line at once: by statement date, creation time and id, each
    line numbered among identical lines of its bank."""
    from celerp.services.field_fingerprints import statement_line, statement_line_ident

    seen, out = {}, {}
    for r in sorted(rows, key=lambda r: (r["statement_date"], r["created_at"], r["id"])):
        base = (r["line_date"], r["amount"], r["description"], r["reference"], None)
        same = (r["bank"], *statement_line_ident(*base))
        out[r["id"]] = statement_line(*base, seen.get(same, 0))
        seen[same] = seen.get(same, 0) + 1
    return out


def test_lines_upgrade_streams_in_bounded_batches(monkeypatch):
    """The line backfill and its downgrade hold at most BATCH lines at a time, so an install
    with a long statement history upgrades in flat memory: with a batch of 7 over 60 lines no
    statement on the lines returns or writes more than 7, every line is written once each way,
    and each fingerprint equals the numbering read over the whole table at once, including
    identical lines spread over statements, banks, signs and batches."""
    import datetime as dt

    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from alembic_support import alembic_run
    from celerp.migrations import _json_compat

    monkeypatch.setattr(_json_compat, "BATCH", 7)
    with throwaway_db("bankbatch") as (_, sync_url):
        upgrade_to(sync_url, _PREV)
        eng = create_engine(sync_url)
        rows = []
        try:
            with eng.begin() as conn:
                cid = str(uuid.uuid4())
                conn.execute(text("INSERT INTO companies (id, name, slug, settings, is_active, created_at) VALUES "
                                  "(:id, 'Co', :slug, CAST('{}' AS json), true, now())"), {"id": cid, "slug": f"co-{cid[:8]}"})
                t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
                for b in range(2):
                    bank = str(uuid.uuid4())
                    conn.execute(text("INSERT INTO bank_accounts (id, company_id, chart_account_code, bank_name, "
                                      "account_number, bank_type, currency, opening_balance, is_active, created_at) VALUES "
                                      "(:id, :c, '1111', 'Bank', '****1234', 'checking', 'USD', 0, true, now())"),
                                 {"id": bank, "c": cid})
                    for s, date in enumerate(("2026-03-31", "2026-01-31", "2026-02-28")):
                        sess = str(uuid.uuid4())
                        conn.execute(text("INSERT INTO reconciliation_sessions (id, company_id, bank_account_id, "
                                          "statement_date, statement_balance, status, reconciled_je_ids, created_at) VALUES "
                                          "(:id, :c, :b, :d, 0, 'open', CAST('[]' AS json), now())"),
                                     {"id": sess, "c": cid, "b": bank, "d": date})
                        for i in range(10):
                            # Most lines repeat across statements and banks; the sign flips on some.
                            amount = (-3 if i % 2 else 3) if i < 6 else 10 + i
                            r = {"id": str(uuid.uuid4()), "bank": bank, "statement_date": date, "line_date": "2026-01-02",
                                 "amount": amount, "description": " atm  fee" if i % 3 else "ATM FEE", "reference": None,
                                 "created_at": t0 + dt.timedelta(minutes=(9 - i) + 10 * s)}
                            rows.append(r)
                            conn.execute(text(
                                "INSERT INTO bank_statement_lines (id, company_id, reconciliation_id, line_date, "
                                "description, amount, status, matched_je_id, created_at) VALUES "
                                "(:id, :c, :s, :ld, :d, :a, 'unmatched', NULL, :t)"),
                                {"id": r["id"], "c": cid, "s": sess, "ld": r["line_date"], "d": r["description"],
                                 "a": r["amount"], "t": r["created_at"]})
            taken: list[tuple[str, int]] = []

            def note(_conn, cursor, statement, parameters, _context, executemany):
                if "bank_statement_lines" in statement or statement.startswith("FETCH"):
                    verb = statement.split()[0].upper()
                    taken.append((verb, len(parameters) if executemany else max(cursor.rowcount, 0)))
            event.listen(Engine, "after_cursor_execute", note)
            try:
                upgrade_to(sync_url, _REV)
                up = list(taken)
                with eng.connect() as conn:
                    fps = dict(conn.execute(text("SELECT CAST(id AS text), fingerprint FROM bank_statement_lines")).all())
                taken.clear()
                alembic_run(sync_url, "downgrade", _PREV)
                down = list(taken)
            finally:
                event.remove(Engine, "after_cursor_execute", note)
        finally:
            eng.dispose()
    for phase in (up, down):
        assert max(n for verb, n in phase if verb in ("FETCH", "UPDATE", "SELECT")) <= 7, phase
        assert sum(n for verb, n in phase if verb == "UPDATE") == 60, phase
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    assert fps == _whole_table_numbering(rows)
    assert len(set(fps.values())) < 60  # identical lines across banks share a fingerprint, as before

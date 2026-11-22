# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""How a bank statement line stored before lines knew their bank moves to the current shape.

The schema upgrade and a restore of an older backup both use it, so a line reaches the same
fingerprint, status, entry list and suggestion either way.
"""

from __future__ import annotations

from decimal import Decimal

from celerp.services.field_fingerprints import statement_line, statement_line_ident

# The columns of a line its fingerprint is made from.
BASE = ("line_date", "amount", "description", "reference", "raw_balance")


def _block(row) -> tuple:
    """Lines that can be identical share a block: the same bank, date and amount magnitude."""
    amount = row["amount"]
    return (str(row["bank_account_id"]), row["line_date"], None if amount is None else abs(Decimal(str(amount))))


def order(row) -> tuple:
    """The order ``fingerprints`` reads lines in: block by block, and inside a block by
    statement date, creation time and id. The schema upgrade reads the database in the same
    order (its ORDER BY)."""
    bank, date, size = _block(row)
    return (bank, str(date), size is None, size or 0, str(row["statement_date"]), str(row["created_at"]), str(row["id"]))


def ident(row) -> tuple:
    """What identical lines of one bank share (``statement_line_ident``)."""
    return statement_line_ident(*(row[k] for k in BASE))


def fingerprint_of(row, n: int) -> str:
    """The fingerprint of a line that is the ``n``-th of its identical lines."""
    return statement_line(*(row[k] for k in BASE), n)


def places(rows, ident_of=ident):
    """Each row of ``rows`` (read in ``order``) with its place among identical lines of its
    bank, numbered by statement date, creation time and id, so no two rows of a bank share a
    fingerprint. Only the current block is remembered, so memory stays flat however many lines
    there are."""
    current, seen = None, {}
    for r in rows:
        if (b := _block(r)) != current:
            current, seen = b, {}
        same = ident_of(r)
        n = seen.get(same, 0)
        seen[same] = n + 1
        yield r, n


def migrated_line(row: dict) -> dict:
    """The status, entry list and suggestion an existing line moves to."""
    je = row.get("matched_je_id")
    if row.get("status") == "suggested":
        return {"status": "unmatched", "matched_je_ids": [],
                "suggestion": {"kind": "match", "je_ids": [je] if je else [], "source": "match"}}
    status = "posted" if row.get("status") == "created" else row.get("status")
    return {"status": status, "matched_je_ids": [je] if je else [], "suggestion": None}

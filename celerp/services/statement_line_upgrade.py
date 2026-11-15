# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""How a bank statement line stored before lines knew their bank moves to the current shape.

The schema upgrade and a restore of an older backup both use it, so a line reaches the same
fingerprint, status, entry list and suggestion either way.
"""

from __future__ import annotations

from celerp.services.field_fingerprints import statement_line, statement_line_ident


def backfill_fingerprints(rows: list[dict]) -> dict:
    """Each row's fingerprint by id. Identical lines on one bank are numbered in the order of
    their statement date, creation time and id, so no two rows of a bank share one."""
    seen: dict[tuple, int] = {}
    out = {}
    for r in sorted(rows, key=lambda r: (str(r["statement_date"]), str(r["created_at"]), str(r["id"]))):
        base = (r["line_date"], r["amount"], r["description"], r["reference"], r["raw_balance"])
        ident = (str(r["bank_account_id"]), *statement_line_ident(*base))
        n = seen.get(ident, 0)
        seen[ident] = n + 1
        out[r["id"]] = statement_line(*base, n)
    return out


def migrated_line(row: dict) -> dict:
    """The status, entry list and suggestion an existing line moves to."""
    je = row.get("matched_je_id")
    if row.get("status") == "suggested":
        return {"status": "unmatched", "matched_je_ids": [],
                "suggestion": {"kind": "match", "je_ids": [je] if je else [], "source": "match"}}
    status = "posted" if row.get("status") == "created" else row.get("status")
    return {"status": status, "matched_je_ids": [je] if je else [], "suggestion": None}

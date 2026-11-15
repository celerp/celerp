# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Pure auto-matching engine for bank reconciliation. No DB access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher


@dataclass
class _Line:
    id: str
    line_date: str
    description: str
    amount: Decimal


@dataclass
class _Entry:
    id: str
    ts: str  # ISO date prefix YYYY-MM-DD
    memo: str
    amount: Decimal  # net (debit - credit)


def _to_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _name_similarity(a: str, b: str) -> float:
    a_lower = a.lower()
    b_lower = b.lower()
    return SequenceMatcher(None, a_lower, b_lower).ratio()


def _looks_like_reference(desc: str, memo: str) -> bool:
    """True if description contains keywords from memo (e.g. INV-0042, PO-001)."""
    desc_upper = desc.upper()
    return any(tok in desc_upper for tok in _reference_tokens(memo))


def amount_buckets(rows: list) -> dict[Decimal, list]:
    """Rows by amount, so a line looks only at entries of an equal or near amount."""
    out: dict[Decimal, list] = {}
    for r in rows:
        out.setdefault(r.amount, []).append(r)
    return out


def _reference_tokens(text: str) -> list[str]:
    """Identifier-like tokens (INV-123, PO-123, 8+ digit runs) in ``text``."""
    import re
    return re.findall(r"[A-Z]{2,}-\d+|\d{8,}", (text or "").upper())


def auto_match(
    statement_lines: list[dict],
    book_entries: list[dict],
    tolerance_pct: float = 0.02,
    date_window_days: int = 7,
) -> list[tuple[str, str, str]]:
    """Auto-match bank statement lines to book entries.

    Returns list of (line_id, entry_id, confidence) where
    confidence is "high" | "medium" | "low".

    Rules (priority order):
    1. Exact amount + exact date match -> high
    2. Exact amount + date within 3-day window -> high
    3. Exact amount + date within date_window_days -> medium
    4. Description contains reference/invoice number from memo -> high with the
       exact amount, otherwise low
    5. Amount within tolerance + name similarity > 0.6 -> medium
    6. Recurring pattern (same amount + description similarity > 0.8) -> low

    Entries are read through their amount buckets: a line compares only entries of an
    equal amount, within the tolerance, or naming a reference the line carries.
    """
    from bisect import bisect_left, bisect_right

    lines = [
        _Line(
            id=l["id"],
            line_date=l.get("line_date", ""),
            description=l.get("description", ""),
            amount=Decimal(str(l.get("amount", 0))),
        )
        for l in statement_lines
        if l.get("status", "unmatched") == "unmatched"
    ]
    entries = [
        _Entry(
            id=e["je_id"],
            ts=e.get("date") or e.get("ts", ""),
            memo=e.get("memo", ""),
            amount=Decimal(str(e.get("amount", 0))),
        )
        for e in book_entries
    ]
    order = {e.id: i for i, e in enumerate(entries)}
    buckets = amount_buckets(entries)
    amounts = sorted(buckets)
    by_token: dict[str, list[_Entry]] = {}
    for entry in entries:
        for tok in _reference_tokens(entry.memo):
            by_token.setdefault(tok, []).append(entry)

    used_entry_ids: set[str] = set()
    results: list[tuple[str, str, str]] = []

    for line in lines:
        line_dt = _to_date(line.line_date)
        best: tuple[str, str] | None = None
        best_rule = 99
        band = abs(line.amount) * Decimal(str(tolerance_pct)) / (1 - Decimal(str(tolerance_pct)))
        near = amounts[bisect_left(amounts, line.amount - band):bisect_right(amounts, line.amount + band)]
        desc_upper = line.description.upper()
        candidates = {e.id: e for a in near for e in buckets[a]}
        for tok, hits in by_token.items():
            if tok in desc_upper:
                candidates.update((e.id, e) for e in hits)

        for entry in sorted(candidates.values(), key=lambda e: order[e.id]):
            if entry.id in used_entry_ids:
                continue
            entry_dt = _to_date(entry.ts)
            amt_match = line.amount == entry.amount

            # Rule 1: exact amount + exact date
            if amt_match and line_dt and entry_dt and line_dt == entry_dt:
                if best_rule > 1:
                    best = (entry.id, "high")
                    best_rule = 1
                continue

            # Rule 2: exact amount + 3-day window
            if amt_match and line_dt and entry_dt:
                delta = abs((line_dt - entry_dt).days)
                if delta <= 3:
                    if best_rule > 2:
                        best = (entry.id, "high")
                        best_rule = 2
                    continue
                # Rule 3: exact amount + wider window
                if delta <= date_window_days:
                    if best_rule > 3:
                        best = (entry.id, "medium")
                        best_rule = 3
                    continue

            # Rule 4: reference match (e.g. INV-0042 in bank description); high only
            # when the amount agrees too
            if _looks_like_reference(line.description, entry.memo):
                if best_rule > 4:
                    best = (entry.id, "high" if amt_match else "low")
                    best_rule = 4
                continue

            # Rule 5: tolerance amount + name similarity
            if entry.amount != 0:
                diff_pct = abs(float(line.amount - entry.amount)) / abs(float(entry.amount))
                if diff_pct <= tolerance_pct:
                    sim = _name_similarity(line.description, entry.memo)
                    if sim > 0.6:
                        if best_rule > 5:
                            best = (entry.id, "medium")
                            best_rule = 5
                        continue

            # Rule 6: recurring (same amount, high description similarity)
            if amt_match:
                sim = _name_similarity(line.description, entry.memo)
                if sim > 0.8:
                    if best_rule > 6:
                        best = (entry.id, "low")
                        best_rule = 6

        if best:
            results.append((line.id, best[0], best[1]))
            used_entry_ids.add(best[0])

    return results


def apply_rules(
    statement_lines: list[dict],
    rules: list[dict],
    bank_id: str | None = None,
) -> list[tuple[str, str]]:
    """Apply reconciliation rules to unmatched statement lines.

    A rule reads the line's description, reference, counterparty account or counterparty
    key; one with an amount matches only that amount, and one with a bank only that
    bank's lines. Returns (line_id, rule_id) for the first rule each line matches.
    """
    results: list[tuple[str, str]] = []
    for line in statement_lines:
        if line.get("status", "unmatched") != "unmatched":
            continue
        for rule in rules:
            if not rule.get("is_active", True):
                continue
            if rule.get("bank_account_id") and bank_id and str(rule["bank_account_id"]) != str(bank_id):
                continue
            if rule.get("match_amount") is not None and \
                    abs(Decimal(str(rule["match_amount"]))) != abs(Decimal(str(line.get("amount", 0)))):
                continue
            field = rule.get("match_field", "description")
            pattern = (rule.get("match_pattern") or "").lower()
            target = str(line.get(field) or "").lower()
            match_type = rule.get("match_type", "contains")
            if match_type == "exact":
                matched = target == pattern
            elif match_type == "starts_with":
                matched = target.startswith(pattern)
            else:
                matched = bool(pattern) and pattern in target
            if matched:
                results.append((line["id"], rule["id"]))
                break
    return results

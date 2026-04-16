# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
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
    import re
    # Extract identifier-like tokens: INV-123, PO-123, REF-123, 8+ digit numbers
    tokens = re.findall(r"[A-Z]{2,}-\d+|\d{8,}", memo.upper())
    if not tokens:
        return False
    desc_upper = desc.upper()
    return any(tok in desc_upper for tok in tokens)


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
    1. Exact amount + exact date match → high
    2. Exact amount + date within 3-day window → high
    3. Exact amount + date within date_window_days → medium
    4. Description contains reference/invoice number from memo → high
    5. Amount within tolerance + name similarity > 0.6 → medium
    6. Recurring pattern (same amount + description similarity > 0.8) → low
    """
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
            ts=e.get("ts", ""),
            memo=e.get("memo", ""),
            amount=Decimal(str(e.get("amount", 0))),
        )
        for e in book_entries
    ]

    used_entry_ids: set[str] = set()
    results: list[tuple[str, str, str]] = []

    for line in lines:
        line_dt = _to_date(line.line_date)
        best: tuple[str, str] | None = None
        best_rule = 99

        for entry in entries:
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

            # Rule 4: reference match (e.g. INV-0042 in bank description)
            if _looks_like_reference(line.description, entry.memo):
                if best_rule > 4:
                    best = (entry.id, "high")
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
) -> list[tuple[str, str]]:
    """Apply reconciliation rules to unmatched statement lines.

    Returns list of (line_id, rule_id) for each rule that matches.
    """
    import re
    results: list[tuple[str, str]] = []

    for line in statement_lines:
        if line.get("status", "unmatched") != "unmatched":
            continue
        desc = line.get("description", "")
        for rule in rules:
            if not rule.get("is_active", True):
                continue
            field = rule.get("match_field", "description")
            pattern = rule.get("match_pattern", "")
            match_type = rule.get("match_type", "contains")
            target = desc if field == "description" else line.get(field, "")

            matched = False
            if match_type == "exact":
                matched = target.lower() == pattern.lower()
            elif match_type == "starts_with":
                matched = target.lower().startswith(pattern.lower())
            else:  # contains
                matched = pattern.lower() in target.lower()

            if matched:
                results.append((line["id"], rule["id"]))
                break  # First matching rule wins

    return results

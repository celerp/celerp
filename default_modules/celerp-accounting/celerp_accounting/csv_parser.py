# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Pure CSV parser for bank statements. No DB access."""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Any

# Known header aliases → canonical field names
_FIELD_ALIASES: dict[str, str] = {
    # Date
    "date": "date", "transaction date": "date", "txn date": "date",
    "posting date": "date", "value date": "date",
    # Description
    "description": "description", "narrative": "description",
    "details": "description", "particulars": "description",
    "memo": "description", "remarks": "description",
    "transaction": "description", "transaction description": "description",
    # Amount (combined debit/credit sign)
    "amount": "amount", "net amount": "amount", "transaction amount": "amount",
    # Debit
    "debit": "debit", "dr": "debit", "withdrawals": "debit", "withdrawal": "debit",
    "debit amount": "debit", "paid out": "debit",
    # Credit
    "credit": "credit", "cr": "credit", "deposits": "credit", "deposit": "credit",
    "credit amount": "credit", "paid in": "credit",
    # Balance
    "balance": "balance", "running balance": "balance", "closing balance": "balance",
    "available balance": "balance",
    # Reference
    "reference": "reference", "ref": "reference", "cheque no": "reference",
    "check no": "reference", "transaction id": "reference", "txn id": "reference",
    "reference no": "reference",
}

_REQUIRED_FIELDS = {"date", "description"}
_AMOUNT_FIELDS = {"amount", "debit", "credit"}


def _normalise_header(h: str) -> str:
    return h.strip().lower().replace("-", " ").replace("_", " ")


def _detect_column_map(headers: list[str]) -> dict[str, str] | None:
    """Return {canonical_field: header_index_str} or None if ambiguous."""
    mapping: dict[str, str] = {}
    for h in headers:
        canonical = _FIELD_ALIASES.get(_normalise_header(h))
        if canonical and canonical not in mapping:
            mapping[canonical] = h

    has_required = _REQUIRED_FIELDS.issubset(mapping)
    has_amount = bool(_AMOUNT_FIELDS & set(mapping))
    if has_required and has_amount:
        return mapping
    return None


def _parse_decimal(raw: str) -> Decimal | None:
    if not raw:
        return None
    cleaned = raw.replace(",", "").replace(" ", "").strip()
    # Remove currency symbols
    for sym in ("$", "€", "£", "¥", "฿", "₹"):
        cleaned = cleaned.replace(sym, "")
    # Handle parentheses as negative
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _row_to_line(row: dict[str, str], col_map: dict[str, str], idx: int) -> dict[str, Any]:
    """Convert a CSV row dict → normalised line dict using column_map."""
    def get(field: str) -> str:
        return row.get(col_map.get(field, ""), "").strip()

    line_date = get("date")
    description = get("description")

    # Amount resolution: prefer combined amount, fall back to debit/credit
    amount: Decimal | None = None
    if "amount" in col_map:
        amount = _parse_decimal(get("amount"))
    if amount is None:
        debit = _parse_decimal(get("debit")) or Decimal("0")
        credit = _parse_decimal(get("credit")) or Decimal("0")
        if debit or credit:
            # Debit = outflow (negative), credit = inflow (positive)
            amount = credit - debit

    raw_balance = _parse_decimal(get("balance")) if "balance" in col_map else None
    reference = get("reference") or None

    return {
        "line_date": line_date,
        "description": description,
        "amount": str(amount) if amount is not None else "0",
        "raw_balance": str(raw_balance) if raw_balance is not None else None,
        "reference": reference,
        "raw_csv_row": dict(row),
        "_row_idx": idx,
    }


def parse_bank_csv(content: bytes, column_map: dict[str, str] | None = None) -> dict:
    """Parse bank statement CSV.

    Args:
        content: raw CSV bytes (UTF-8 or latin-1)
        column_map: optional {canonical_field: csv_header} mapping.
                    If None, auto-detection is attempted.

    Returns:
        {
            "needs_mapping": bool,
            "headers": list[str],
            "lines": list[dict],   # populated when mapping is known
            "preview": list[dict], # first 5 raw rows (always)
        }
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = list(reader)

    preview = [dict(r) for r in rows[:5]]

    if column_map is None:
        column_map = _detect_column_map(headers)

    if column_map is None:
        return {"needs_mapping": True, "headers": headers, "lines": [], "preview": preview}

    lines = [_row_to_line(dict(r), column_map, i) for i, r in enumerate(rows)]
    return {"needs_mapping": False, "headers": headers, "lines": lines, "preview": preview}

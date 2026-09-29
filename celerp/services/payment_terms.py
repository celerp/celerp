# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Payment terms: the company's configured list and the due date a term implies."""
from __future__ import annotations

from datetime import date, timedelta


DEFAULT_PAYMENT_TERMS: list[dict] = [
    {"name": "Pay in Advance", "days": 0, "description": "Full payment before delivery"},
    {"name": "Cash on Delivery", "days": 0, "description": "Payment on receipt of goods"},
    {"name": "Deposit (50%)", "days": 0, "description": "50% deposit upfront, balance on delivery"},
    {"name": "Net 7", "days": 7, "description": "Due within 7 days"},
    {"name": "Net 15", "days": 15, "description": "Due within 15 days"},
    {"name": "Net 30", "days": 30, "description": "Due within 30 days"},
    {"name": "Net 60", "days": 60, "description": "Due within 60 days"},
    {"name": "Net 90", "days": 90, "description": "Due within 90 days"},
]


def company_payment_terms(settings: dict | None) -> list[dict]:
    """The company's payment terms, else the defaults."""
    return (settings or {}).get("payment_terms") or DEFAULT_PAYMENT_TERMS


def due_date_for_terms(issue_date: str | None, payment_terms_name: str | None, terms_list: list[dict]) -> str | None:
    """Return the ISO due date a payment term sets from issue_date.

    Returns None when any input is missing or invalid, or the term has no day count,
    so callers leave the due date as it is.
    """
    if not issue_date or not payment_terms_name:
        return None
    term = next((item for item in terms_list if item.get("name") == payment_terms_name), None)
    if term is None:
        return None
    days = term.get("days")
    if days is None:
        return None
    try:
        base = date.fromisoformat(str(issue_date)[:10])
    except (ValueError, TypeError):
        return None
    return (base + timedelta(days=int(days))).isoformat()

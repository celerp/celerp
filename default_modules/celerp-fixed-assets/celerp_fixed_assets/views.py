# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What a member reads of the register's money.

Amounts are financial. A role that views fixed assets without view_financial_reports reads
every response with its amounts taken out, wherever they sit (an asset, its sale, a
movement, a correction, a reminder, a review, the history kept on any of them), and only
the files that show no money. Every read applies this one projection, so no surface shows
what another hides.
"""
from __future__ import annotations

from fastapi import Depends

from celerp.services.auth import get_current_role
from celerp.services.permissions import get_current_company_settings, role_has_permission

# The files that show what was paid, insured, valued or received.
FINANCIAL_TAGS = ("bills", "valuations", "insurance", "disposal")
# The keys that hold an amount, at any depth of a response.
FINANCIAL_KEYS = frozenset((
    "accepted_amount", "accumulated", "accumulated_depreciation", "accumulated_impairment", "amount", "amounts",
    "balance", "book_carrying", "carrying_amount", "contributions", "cost", "credit", "credited", "debit",
    "deferred_tax", "depreciation", "difference", "fair_value", "forecast", "gain_or_loss", "importing",
    "insured_value", "ledger", "measured", "nbv", "new_carrying_amount", "periods", "pnl", "posted", "price",
    "proceeds", "profit_or_loss", "recoverable_amount", "registered", "remainder", "remaining", "reported",
    "restated", "revaluation_surplus", "reversal", "surplus", "tax_values", "tax_written_down_value",
    "their_cost", "total"))


def shows_money(settings: dict, role: str) -> bool:
    return role_has_permission(settings, role, "view_financial_reports")


async def money_shown(role: str = Depends(get_current_role),
                      settings: dict = Depends(get_current_company_settings)) -> bool:
    """Whether the member's role reads amounts, checked on every request."""
    return shows_money(settings, role)


def financial_file(f: dict) -> bool:
    return f.get("document_tag") in FINANCIAL_TAGS


def without_money(value):
    """The value with every amount set to None and every financial file left out."""
    if isinstance(value, list):
        return [without_money(v) for v in value
                if not (isinstance(v, dict) and "document_tag" in v and financial_file(v))]
    if isinstance(value, dict):
        return {k: None if k in FINANCIAL_KEYS and not isinstance(v, bool) else without_money(v)
                for k, v in value.items()}
    return value


def readable(value, money: bool):
    """The value as the member reads it."""
    return value if money else without_money(value)

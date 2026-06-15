# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Canonical monetary arithmetic for Celerp.

All monetary computations must go through this module.
Never use raw float arithmetic for money. Never call round(x, 2) on money.

Usage pattern:
    from celerp.services.money import round_money, to_decimal, to_stored_float

    line_total = round_money(to_decimal(qty) * to_decimal(price), currency)
    data["line_total"] = to_stored_float(line_total)
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

# ISO 4217 decimal place overrides. All other currencies default to DEFAULT_DP.
CURRENCY_DP: dict[str, int] = {
    # 0 decimal places
    "JPY": 0, "KRW": 0, "VND": 0, "IDR": 0, "UGX": 0,
    "RWF": 0, "GNF": 0, "BIF": 0, "XOF": 0, "XAF": 0,
    # 3 decimal places
    "KWD": 3, "BHD": 3, "OMR": 3, "JOD": 3, "TND": 3,
    # 4 decimal places
    "CLF": 4,
}
DEFAULT_DP: int = 2

_MoneyInput = Union[float, str, int, Decimal, None]


def currency_dp(currency: str) -> int:
    """Return ISO 4217 decimal places for a currency code. Unknown codes default to 2."""
    return CURRENCY_DP.get((currency or "").upper(), DEFAULT_DP)


def to_decimal(v: _MoneyInput) -> Decimal:
    """Safely coerce any numeric input to Decimal.

    Always use str() conversion for float inputs to avoid IEEE 754 representation
    errors (e.g. Decimal(38.6) produces 38.5999999... but Decimal('38.6') is exact).
    """
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def round_money(v: _MoneyInput, currency: str) -> Decimal:
    """Round to currency decimal places using HALF_UP.

    This is the only function allowed to round monetary values.
    Call at every computation boundary before storing.
    """
    dp = currency_dp(currency)
    quant = Decimal(10) ** -dp
    return to_decimal(v).quantize(quant, rounding=ROUND_HALF_UP)


def to_stored_float(v: Decimal) -> float:
    """Convert a rounded Decimal to float for JSON storage.

    Only call this AFTER round_money() - never on raw computed values.
    The explicit wrapper makes every storage boundary searchable in code.
    """
    return float(v)

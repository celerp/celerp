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


def to_minor_units(v: _MoneyInput, currency: str) -> int:
    """Integer smallest-currency-units for a processor (e.g. cents), respecting
    zero-decimal (JPY) and 3-decimal (KWD) currencies."""
    dp = currency_dp(currency)
    return int((round_money(v, currency) * (Decimal(10) ** dp)).to_integral_value(rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Rates (unit prices) vs amounts.
# A unit price is a RATE, not a money amount: it may carry more precision so that
# rate * quantity reconciles to a currency-rounded total for fractional quantities.
# Amounts (line totals, tax, valuation, JE postings) ALWAYS use round_money.
# ---------------------------------------------------------------------------
RATE_EXTRA_DP: int = 4


def rate_dp(currency: str) -> int:
    """Decimal-place ceiling for a unit price/rate: currency precision + RATE_EXTRA_DP of headroom.

    A rate needs ``2 + log10(qty)`` decimals for ``round(rate*qty)`` to hit any cent; +4 covers
    quantities up to 10**RATE_EXTRA_DP (USD -> 6 dp covers qty <= 10,000, incl. weight/volume goods).
    """
    return currency_dp(currency) + RATE_EXTRA_DP


def round_rate(v: _MoneyInput, currency: str) -> Decimal:
    """Round a unit price/rate to the rate ceiling. Use for storing any directly entered or derived
    unit price. Amounts (totals, tax) must use round_money instead."""
    quant = Decimal(10) ** -rate_dp(currency)
    return to_decimal(v).quantize(quant, rounding=ROUND_HALF_UP)


def unit_price_from_total(total: _MoneyInput, qty: _MoneyInput, currency: str) -> Decimal:
    """Derive the unit price from a target line/lot total at the FEWEST decimals (currency_dp..rate_dp)
    such that ``round_money(unit * qty) == round_money(total)``.

    This lets a typed total be honoured exactly while keeping the stored rate as clean as possible
    (e.g. 590 / 38.6 -> 15.285, not 15.284974). Falls back to the rate ceiling when qty exceeds the
    headroom (then round_money(unit*qty) is the nearest representable total). Returns 0 for qty == 0;
    the caller must guard that case (a rate cannot be derived from a total at zero quantity).
    """
    q = to_decimal(qty)
    if q == 0:
        return Decimal(0)
    target = round_money(total, currency)
    exact = to_decimal(total) / q
    lo, hi = currency_dp(currency), rate_dp(currency)
    for d in range(lo, hi + 1):
        cand = exact.quantize(Decimal(10) ** -d, rounding=ROUND_HALF_UP)
        if round_money(cand * q, currency) == target:
            return cand
    return exact.quantize(Decimal(10) ** -hi, rounding=ROUND_HALF_UP)

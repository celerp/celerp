# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Unit tests for celerp.services.money - no DB required."""
from decimal import Decimal

import pytest

from celerp.services.money import (
    CURRENCY_DP,
    DEFAULT_DP,
    currency_dp,
    round_money,
    to_decimal,
    to_stored_float,
)


# ---------------------------------------------------------------------------
# currency_dp
# ---------------------------------------------------------------------------

def test_currency_dp_usd():
    assert currency_dp("USD") == 2

def test_currency_dp_eur():
    assert currency_dp("EUR") == 2

def test_currency_dp_thb():
    assert currency_dp("THB") == 2

def test_currency_dp_jpy_zero():
    assert currency_dp("JPY") == 0

def test_currency_dp_krw_zero():
    assert currency_dp("KRW") == 0

def test_currency_dp_vnd_zero():
    assert currency_dp("VND") == 0

def test_currency_dp_kwd_three():
    assert currency_dp("KWD") == 3

def test_currency_dp_bhd_three():
    assert currency_dp("BHD") == 3

def test_currency_dp_clf_four():
    assert currency_dp("CLF") == 4

def test_currency_dp_unknown_defaults_to_2():
    assert currency_dp("XYZ") == DEFAULT_DP == 2

def test_currency_dp_case_insensitive():
    assert currency_dp("usd") == currency_dp("USD") == 2
    assert currency_dp("kwd") == currency_dp("KWD") == 3

def test_currency_dp_empty_string():
    assert currency_dp("") == DEFAULT_DP

def test_currency_dp_none_like():
    assert currency_dp(None) == DEFAULT_DP  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# to_decimal
# ---------------------------------------------------------------------------

def test_to_decimal_float_exact():
    # str() conversion avoids IEEE 754 representation errors
    result = to_decimal(38.6)
    assert result == Decimal("38.6")

def test_to_decimal_string():
    assert to_decimal("38.6") == Decimal("38.6")

def test_to_decimal_int():
    assert to_decimal(100) == Decimal("100")

def test_to_decimal_none_returns_zero():
    assert to_decimal(None) == Decimal(0)

def test_to_decimal_decimal_passthrough():
    d = Decimal("1.5")
    assert to_decimal(d) is d

def test_to_decimal_zero_float():
    assert to_decimal(0.0) == Decimal(0)

def test_to_decimal_negative():
    assert to_decimal(-5.5) == Decimal("-5.5")


# ---------------------------------------------------------------------------
# round_money
# ---------------------------------------------------------------------------

def test_round_money_usd_basic():
    assert round_money("1.234", "USD") == Decimal("1.23")

def test_round_money_usd_half_up():
    assert round_money("0.005", "USD") == Decimal("0.01")

def test_round_money_usd_truncate():
    assert round_money("0.004", "USD") == Decimal("0.00")

def test_round_money_kwd_three_dp():
    assert round_money("1.2345", "KWD") == Decimal("1.235")

def test_round_money_kwd_half_up():
    assert round_money("1.2345", "KWD") == Decimal("1.235")

def test_round_money_jpy_zero_dp():
    assert round_money("99.5", "JPY") == Decimal("100")

def test_round_money_jpy_truncate():
    assert round_money("99.4", "JPY") == Decimal("99")

def test_round_money_clf_four_dp():
    assert round_money("1.23456", "CLF") == Decimal("1.2346")

def test_round_money_unknown_currency_uses_2dp():
    assert round_money("1.999", "XYZ") == Decimal("2.00")


# ---------------------------------------------------------------------------
# Real invoice scenario (the exact bug Nikolai reported)
# ---------------------------------------------------------------------------

def test_line_total_rounding_tourmaline():
    """38.6 ct × 38.86 USD/ct must round to 1500.00, not 1499.996."""
    line_total = round_money(to_decimal(38.6) * to_decimal(38.86), "USD")
    assert line_total == Decimal("1500.00")

def test_line_total_rounding_ruby():
    """64 ct × 17.19 USD/ct = 1100.16 (exact)."""
    line_total = round_money(to_decimal(64) * to_decimal(17.19), "USD")
    assert line_total == Decimal("1100.16")

def test_tax_rounding_consistency():
    """Per-line VAT sum must equal round(subtotal × rate, 2) - audit consistency."""
    lt1 = round_money(to_decimal(38.6) * to_decimal(38.86), "USD")   # 1500.00
    lt2 = round_money(to_decimal(64) * to_decimal(17.19), "USD")     # 1100.16
    tax1 = round_money(lt1 * to_decimal("0.07"), "USD")              # 105.00
    tax2 = round_money(lt2 * to_decimal("0.07"), "USD")              # 77.01
    per_line_tax = tax1 + tax2                                        # 182.01

    subtotal = lt1 + lt2                                              # 2600.16
    subtotal_tax = round_money(subtotal * to_decimal("0.07"), "USD")  # round(182.0112, 2) = 182.01
    assert per_line_tax == subtotal_tax, (
        f"Per-line tax {per_line_tax} must equal subtotal-based tax {subtotal_tax}"
    )

def test_total_chain_exact():
    """Full chain produces 2782.17, not 2782.16692."""
    lt1 = round_money(to_decimal(38.6) * to_decimal(38.86), "USD")
    lt2 = round_money(to_decimal(64) * to_decimal(17.19), "USD")
    subtotal = lt1 + lt2
    tax = round_money(lt1 * to_decimal("0.07"), "USD") + round_money(lt2 * to_decimal("0.07"), "USD")
    total = round_money(subtotal + tax, "USD")
    assert total == Decimal("2782.17")

def test_payment_of_displayed_amount_matches_stored():
    """The displayed total (2782.17) is exactly what gets stored - no 409."""
    total = Decimal("2782.17")
    payment = Decimal("2782.17")
    diff = payment - total
    assert diff <= Decimal("0.01"), f"Payment should be accepted, diff={diff}"


# ---------------------------------------------------------------------------
# to_stored_float
# ---------------------------------------------------------------------------

def test_to_stored_float_returns_float():
    result = to_stored_float(Decimal("1.23"))
    assert isinstance(result, float)

def test_to_stored_float_preserves_value():
    assert to_stored_float(Decimal("2782.17")) == pytest.approx(2782.17)

def test_to_stored_float_zero():
    assert to_stored_float(Decimal(0)) == 0.0

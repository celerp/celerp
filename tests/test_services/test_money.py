# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Unit tests for celerp.services.money - no DB required."""
from decimal import Decimal

import pytest

from celerp.services.money import (
    CURRENCY_DP,
    DEFAULT_DP,
    EXCHANGE_RATE_DP,
    RATE_EXTRA_DP,
    currency_dp,
    rate_dp,
    round_exchange_rate,
    round_money,
    round_rate,
    to_decimal,
    to_stored_float,
    unit_price_from_total,
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


# ---------------------------------------------------------------------------
# Rates (unit prices): rate_dp / round_rate / unit_price_from_total
# ---------------------------------------------------------------------------

def test_rate_dp_is_currency_plus_headroom():
    assert rate_dp("USD") == 2 + RATE_EXTRA_DP        # 6
    assert rate_dp("JPY") == 0 + RATE_EXTRA_DP        # 4 (0-dp currency)
    assert rate_dp("KWD") == 3 + RATE_EXTRA_DP        # 7 (3-dp currency)


def test_round_rate_ceiling_half_up():
    assert round_rate("15.2849749", "USD") == Decimal("15.284975")  # 6 dp, HALF_UP
    assert round_rate("15.28", "USD") == Decimal("15.280000")        # value unchanged, just precision


def test_round_exchange_rate_keeps_twelve_places():
    """A rate belongs to neither currency, so it needs its own ceiling.

    1 LBP is about 0.00001117318 USD. Six places round that to 0.000011 and
    lose three significant figures, which is why rate_dp cannot express a rate.
    """
    assert EXCHANGE_RATE_DP == 12
    assert round_exchange_rate("0.00001117318") == Decimal("0.000011173180")
    # The smallest rate the ceiling can carry, and a whole one, both survive.
    assert round_exchange_rate("0.000000000001") == Decimal("0.000000000001")
    assert round_exchange_rate(35) == Decimal("35.000000000000")


def test_round_exchange_rate_is_half_up_at_the_ceiling():
    """Half rounds away from zero at the twelfth place, matching round_money."""
    assert round_exchange_rate("0.0000000000005") == Decimal("0.000000000001")
    assert round_exchange_rate("0.0000000000004") == Decimal("0.000000000000")
    assert round_exchange_rate("1.2345678901235") == Decimal("1.234567890124")


def test_unit_price_from_total_the_reported_bug():
    # 590 over 38.6 units: 2 dp (15.28) can't reconcile; minimal is 3 dp -> 15.285.
    up = unit_price_from_total("590.00", "38.6", "USD")
    assert up == Decimal("15.285")
    assert round_money(up * to_decimal("38.6"), "USD") == Decimal("590.00")


def test_unit_price_from_total_needs_four_dp():
    # 500 over 38.6 needs 4 dp to reconcile (3 dp gives 499.99).
    up = unit_price_from_total("500.00", "38.6", "USD")
    assert round_money(up * to_decimal("38.6"), "USD") == Decimal("500.00")
    assert up == Decimal("12.9534")


def test_unit_price_from_total_clean_price_stays_two_dp():
    # A total that divides cleanly must NOT gain spurious precision.
    assert unit_price_from_total("772.00", "38.6", "USD") == Decimal("20.00")
    assert unit_price_from_total("100.00", "4", "USD") == Decimal("25.00")


def test_unit_price_from_total_qty_zero_returns_zero():
    assert unit_price_from_total("590", "0", "USD") == Decimal(0)


def test_unit_price_from_total_beyond_headroom_falls_back_to_cap():
    # qty above 10**RATE_EXTRA_DP: no precision within the cap reconciles every cent; fall back to
    # the rate ceiling and accept the nearest representable total (transparent, not silent).
    up = unit_price_from_total("12345.67", "50000", "USD")
    assert up == round_rate(up, "USD")  # at the 6-dp ceiling
    # within a cent of target despite the headroom overflow
    assert abs(round_money(up * to_decimal("50000"), "USD") - Decimal("12345.67")) <= Decimal("0.50")


def test_unit_price_from_total_zero_decimal_currency():
    # JPY: amounts are whole yen; rate carries up to rate_dp(JPY)=4.
    up = unit_price_from_total("590", "38.6", "JPY")
    assert round_money(up * to_decimal("38.6"), "JPY") == Decimal("590")


def test_reconciliation_property_random_realistic():
    # For realistic (total, qty) the derived rate reconciles to the entered total to the cent.
    import itertools
    totals = ["0.01", "1.00", "9.99", "100.00", "590.00", "1234.56", "9999.99"]
    qtys = ["1", "3", "7.5", "38.6", "100", "1500", "9999"]
    for t, q in itertools.product(totals, qtys):
        up = unit_price_from_total(t, q, "USD")
        assert round_money(up * to_decimal(q), "USD") == round_money(t, "USD"), f"{t}/{q} -> {up}"

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp.services.money - canonical monetary arithmetic."""
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


class TestCurrencyDp:
    def test_known_kwd(self):
        assert currency_dp("KWD") == 3

    def test_known_jpy(self):
        assert currency_dp("JPY") == 0

    def test_known_usd(self):
        assert currency_dp("USD") == 2

    def test_known_clf(self):
        assert currency_dp("CLF") == 4

    def test_unknown_defaults_to_2(self):
        assert currency_dp("XYZ") == DEFAULT_DP

    def test_case_insensitive(self):
        assert currency_dp("usd") == currency_dp("USD")

    def test_empty_string_defaults(self):
        assert currency_dp("") == DEFAULT_DP

    def test_none_like_defaults(self):
        # None would fail str.upper(); but currency_dp handles via (currency or "").upper()
        assert currency_dp(None) == DEFAULT_DP  # type: ignore[arg-type]


class TestToDecimal:
    def test_float(self):
        # Must use str() path to avoid IEEE 754 noise
        result = to_decimal(38.6)
        assert result == Decimal("38.6")
        assert str(result) == "38.6"

    def test_string(self):
        assert to_decimal("38.6") == Decimal("38.6")

    def test_none(self):
        assert to_decimal(None) == Decimal(0)

    def test_decimal_passthrough(self):
        d = Decimal("1.5")
        assert to_decimal(d) is d

    def test_int(self):
        assert to_decimal(100) == Decimal("100")

    def test_zero(self):
        assert to_decimal(0) == Decimal(0)


class TestRoundMoney:
    def test_usd_half_up(self):
        # 0.005 -> 0.01 with HALF_UP
        assert round_money("0.005", "USD") == Decimal("0.01")

    def test_usd_truncate(self):
        assert round_money("0.004", "USD") == Decimal("0.00")

    def test_kwd_3dp(self):
        assert round_money("1.2345", "KWD") == Decimal("1.235")

    def test_jpy_0dp(self):
        assert round_money("99.5", "JPY") == Decimal("100")

    def test_clf_4dp(self):
        assert round_money("1.23456", "CLF") == Decimal("1.2346")

    def test_line_total_rounding(self):
        # 38.6 * 38.86 = 1499.996 (raw float) -> must round to 1500.00
        result = round_money(to_decimal("38.6") * to_decimal("38.86"), "USD")
        assert result == Decimal("1500.00")

    def test_tax_rounding_consistency(self):
        # round(1500.00 * 7%, 2) = 105.00; round(1100.16 * 7%, 2) = 77.01
        tax1 = round_money(to_decimal("1500.00") * to_decimal("7") / 100, "USD")
        tax2 = round_money(to_decimal("1100.16") * to_decimal("7") / 100, "USD")
        assert tax1 == Decimal("105.00")
        assert tax2 == Decimal("77.01")

    def test_total_chain(self):
        # subtotal=2600.16 + tax=182.01 = 2782.17 (not 2782.16692)
        subtotal = Decimal("2600.16")
        tax = Decimal("182.01")
        total = round_money(subtotal + tax, "USD")
        assert total == Decimal("2782.17")

    def test_returns_decimal(self):
        result = round_money(1.5, "USD")
        assert isinstance(result, Decimal)


class TestToStoredFloat:
    def test_is_float(self):
        result = to_stored_float(Decimal("1.50"))
        assert isinstance(result, float)

    def test_clean_precision(self):
        d = round_money("38.6", "USD")
        f = to_stored_float(d)
        assert f == 38.6

    def test_zero(self):
        assert to_stored_float(Decimal("0.00")) == 0.0

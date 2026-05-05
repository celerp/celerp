# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for auto_je conversion to base currency."""
from decimal import Decimal as D


def test_to_base_clean_conversion():
    from celerp.services.auto_je import _to_base
    # 100 USD * 35 THB/USD = 3500 THB
    result = _to_base(100.0, D("35"), "THB")
    assert abs(result - 3500.0) < 0.001


def test_to_base_fractional_rounds_correctly():
    from celerp.services.auto_je import _to_base
    # 100 USD * 35.333 = 3533.3 -> rounds to 3533.30 (2dp for THB)
    result = _to_base(100.0, D("35.333"), "THB")
    assert abs(result - 3533.30) < 0.01


def test_to_base_rate_one_unchanged():
    from celerp.services.auto_je import _to_base
    result = _to_base(100.0, D("1"), "USD")
    assert result == 100.0


def test_to_base_zero_amount():
    from celerp.services.auto_je import _to_base
    result = _to_base(0.0, D("35"), "THB")
    assert result == 0.0

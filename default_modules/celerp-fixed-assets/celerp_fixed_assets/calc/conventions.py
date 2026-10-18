# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Timing conventions: how much of a month a layer consumes when it starts and when
the asset is disposed of. Every other month consumes one whole month."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from celerp_fixed_assets.calc.periods import days_in_month, month_index, months_to_fiscal_year_end

_ONE = Decimal(1)
_HALF = Decimal("0.5")


class FullMonth:
    key = "full_month"
    label_key = "fixed_assets.convention.full_month"

    def start_weights(self, day: date, fiscal_year_start: int) -> list[Decimal]:
        return [_ONE]

    def disposal(self, normal: Decimal, day: date, fy_consumed: Decimal, remaining: Decimal) -> Decimal:
        return Decimal(0)


class MidMonth:
    key = "mid_month"
    label_key = "fixed_assets.convention.mid_month"

    def start_weights(self, day: date, fiscal_year_start: int) -> list[Decimal]:
        return [_HALF]

    def disposal(self, normal: Decimal, day: date, fy_consumed: Decimal, remaining: Decimal) -> Decimal:
        return min(max(normal - _HALF, Decimal(0)), remaining)


class Daily:
    key = "daily"
    label_key = "fixed_assets.convention.daily"

    def start_weights(self, day: date, fiscal_year_start: int) -> list[Decimal]:
        dim = days_in_month(day)
        return [Decimal(dim - day.day + 1) / Decimal(dim)]

    def disposal(self, normal: Decimal, day: date, fy_consumed: Decimal, remaining: Decimal) -> Decimal:
        dim = days_in_month(day)
        unused = Decimal(dim - day.day) / Decimal(dim)
        return min(max(normal - unused, Decimal(0)), remaining)


class HalfYear:
    """Six months in the fiscal year a layer starts, spread over its months in service
    that year, and six months in the fiscal year of disposal, which may take back part
    of what that year already charged."""
    key = "half_year"
    label_key = "fixed_assets.convention.half_year"

    def start_weights(self, day: date, fiscal_year_start: int) -> list[Decimal]:
        months = months_to_fiscal_year_end(month_index(day), fiscal_year_start)
        return [Decimal(6) / Decimal(months)] * months

    def disposal(self, normal: Decimal, day: date, fy_consumed: Decimal, remaining: Decimal) -> Decimal:
        return min(Decimal(6), fy_consumed + remaining) - fy_consumed

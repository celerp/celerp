# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Months as integers, so the engine can step through them, and the fiscal year a day falls in."""
from __future__ import annotations

import calendar
from datetime import date


def month_index(day: date) -> int:
    return day.year * 12 + day.month - 1


def parse_period(period: str) -> int:
    """``YYYY-MM`` to a month index."""
    year, month = period.split("-")
    return int(year) * 12 + int(month) - 1


def period_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def days_in_month(day: date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def months_to_fiscal_year_end(index: int, fiscal_year_start: int) -> int:
    """How many months, this one included, are left in the fiscal year of ``index``."""
    return (fiscal_year_start - (index % 12 + 1) - 1) % 12 + 1


def fiscal_year_start_index(index: int, fiscal_year_start: int) -> int:
    return index - (index % 12 + 1 - fiscal_year_start) % 12


# The valid ``fiscal_year_start`` settings, January first: the first day of each month.
FISCAL_YEAR_STARTS = tuple(f"{m:02d}-01" for m in range(1, 13))


def fiscal_year_start_month(value) -> int:
    """The start month a ``fiscal_year_start`` setting names. Missing or empty reads as
    January, the provisioning default; any other value outside FISCAL_YEAR_STARTS raises
    ValueError rather than being guessed."""
    if value is None or value == "":
        return 1
    if value not in FISCAL_YEAR_STARTS:
        raise ValueError(f"{value!r} is not a fiscal year start")
    return FISCAL_YEAR_STARTS.index(value) + 1


def fiscal_year_bounds(day: date, start_month: int) -> tuple[date, date]:
    """The first and last day of the fiscal year starting in ``start_month`` that holds ``day``."""
    end_index = month_index(day) + months_to_fiscal_year_end(month_index(day), start_month) - 1
    end_month = date(end_index // 12, end_index % 12 + 1, 1)
    start_index = end_index - 11
    return date(start_index // 12, start_index % 12 + 1, 1), end_month.replace(day=days_in_month(end_month))

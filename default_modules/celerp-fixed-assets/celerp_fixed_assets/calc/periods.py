# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Months as integers, so the engine can step through them."""
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

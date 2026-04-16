# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for fiscal-year-aware date presets in ui/routes/reports.py."""

from __future__ import annotations

from datetime import date

import pytest

from ui.routes.reports import _fy_start, _resolve_preset


class TestFyStart:
    def test_jan_fiscal_year_in_middle_of_year(self):
        # Jan FY: start is always Jan 1 of current year
        ref = date(2026, 6, 15)
        assert _fy_start("01-01", ref) == date(2026, 1, 1)

    def test_jan_fiscal_year_on_start_date(self):
        ref = date(2026, 1, 1)
        assert _fy_start("01-01", ref) == date(2026, 1, 1)

    def test_apr_fiscal_year_before_start(self):
        # Apr FY: if today is Feb 2026, FY started Apr 2025
        ref = date(2026, 2, 15)
        assert _fy_start("04-01", ref) == date(2025, 4, 1)

    def test_apr_fiscal_year_after_start(self):
        # Apr FY: if today is May 2026, FY started Apr 2026
        ref = date(2026, 5, 15)
        assert _fy_start("04-01", ref) == date(2026, 4, 1)

    def test_apr_fiscal_year_on_start_date(self):
        ref = date(2026, 4, 1)
        assert _fy_start("04-01", ref) == date(2026, 4, 1)

    def test_jul_fiscal_year_before_start(self):
        ref = date(2026, 3, 31)
        assert _fy_start("07-01", ref) == date(2025, 7, 1)

    def test_jul_fiscal_year_after_start(self):
        ref = date(2026, 9, 1)
        assert _fy_start("07-01", ref) == date(2026, 7, 1)

    def test_invalid_fiscal_year_start_falls_back_to_jan(self):
        ref = date(2026, 6, 15)
        assert _fy_start("bad", ref) == date(2026, 1, 1)

    def test_oct_fiscal_year(self):
        # US Federal FY: starts Oct 1
        ref = date(2026, 11, 15)
        assert _fy_start("10-01", ref) == date(2026, 10, 1)
        ref2 = date(2026, 9, 30)
        assert _fy_start("10-01", ref2) == date(2025, 10, 1)


class TestResolvePreset:
    def test_this_month(self):
        today = date.today()
        dfrom, dto = _resolve_preset("this_month")
        assert dfrom == today.replace(day=1).isoformat()
        assert dto == today.isoformat()

    def test_last_3m(self):
        today = date.today()
        dfrom, dto = _resolve_preset("last_3m")
        assert dto == today.isoformat()
        assert dfrom < dto

    def test_last_12m(self):
        today = date.today()
        dfrom, dto = _resolve_preset("last_12m")
        assert dto == today.isoformat()

    def test_this_fy_jan_fiscal(self):
        today = date.today()
        dfrom, dto = _resolve_preset("this_fy", "01-01")
        assert dfrom == date(today.year, 1, 1).isoformat()
        assert dto == today.isoformat()

    def test_this_fy_apr_fiscal(self):
        today = date.today()
        dfrom, dto = _resolve_preset("this_fy", "04-01")
        expected_start = _fy_start("04-01", today)
        assert dfrom == expected_start.isoformat()
        assert dto == today.isoformat()

    def test_last_fy_jan_fiscal(self):
        today = date.today()
        dfrom, dto = _resolve_preset("last_fy", "01-01")
        this_year = today.year
        assert dfrom == date(this_year - 1, 1, 1).isoformat()
        assert dto == date(this_year - 1, 12, 31).isoformat()

    def test_last_fy_apr_fiscal(self):
        # Test directly: given Apr FY, last_fy is Apr 2025 - Mar 2026 (when today is mid-2026)
        # Use the pure functions directly with a fixed reference date
        from datetime import timedelta as _td
        ref = date(2026, 6, 15)
        this_start = _fy_start("04-01", ref)
        last_start = _fy_start("04-01", this_start - _td(days=1))
        last_end = this_start - _td(days=1)
        assert last_start == date(2025, 4, 1)
        assert last_end == date(2026, 3, 31)

    def test_all_preset_returns_empty(self):
        dfrom, dto = _resolve_preset("all")
        assert dfrom == ""
        assert dto == ""

    def test_unknown_preset_returns_empty(self):
        dfrom, dto = _resolve_preset("unknown_preset")
        assert dfrom == ""
        assert dto == ""

    def test_default_fiscal_is_jan_when_omitted(self):
        today = date.today()
        dfrom_fy, _ = _resolve_preset("this_fy")
        dfrom_jan, _ = _resolve_preset("this_fy", "01-01")
        assert dfrom_fy == dfrom_jan

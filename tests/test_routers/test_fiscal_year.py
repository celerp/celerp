# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for fiscal-year-aware date presets in ui/routes/reports.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

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


TODAY = date(2026, 6, 15)


@pytest.fixture
def frozen(monkeypatch):
    from company_today_support import freeze

    freeze(monkeypatch, datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))


@pytest.mark.usefixtures("frozen")
class TestResolvePreset:
    def test_this_month(self):
        assert _resolve_preset("this_month", {}) == ("2026-06-01", "2026-06-15")

    def test_last_3m(self):
        assert _resolve_preset("last_3m", {}) == ((TODAY - timedelta(days=90)).isoformat(), "2026-06-15")

    def test_last_12m(self):
        assert _resolve_preset("last_12m", {}) == ((TODAY - timedelta(days=365)).isoformat(), "2026-06-15")

    def test_this_fy_jan_fiscal(self):
        assert _resolve_preset("this_fy", {"fiscal_year_start": "01-01"}) == ("2026-01-01", "2026-06-15")

    def test_this_fy_apr_fiscal(self):
        assert _resolve_preset("this_fy", {"fiscal_year_start": "04-01"}) == ("2026-04-01", "2026-06-15")

    def test_last_fy_jan_fiscal(self):
        assert _resolve_preset("last_fy", {"fiscal_year_start": "01-01"}) == ("2025-01-01", "2025-12-31")

    def test_last_fy_apr_fiscal(self):
        assert _resolve_preset("last_fy", {"fiscal_year_start": "04-01"}) == ("2025-04-01", "2026-03-31")

    def test_all_preset_returns_empty(self):
        assert _resolve_preset("all", {}) == ("", "")

    def test_unknown_preset_returns_empty(self):
        assert _resolve_preset("unknown_preset", {}) == ("", "")

    def test_default_fiscal_is_jan_when_unset(self):
        assert _resolve_preset("this_fy", {}) == _resolve_preset("this_fy", {"fiscal_year_start": "01-01"})


class TestFinancialSummaryRobustness:
    """The contact money cards count the fiscal year to date through the this_fy preset; a
    malformed fiscal_year_start (e.g. "01" with no day, leaked into the DB by other tests)
    must not 500 the contact page: it falls back to Jan 1 like _fy_start."""

    @pytest.mark.parametrize("fy", ["01", "bad", "", "1-1", "04-01", "01-01"])
    def test_contact_year_to_date_survives_malformed_fiscal_year_start(self, fy):
        date_from, date_to = _resolve_preset("this_fy", {"fiscal_year_start": fy})
        assert date.fromisoformat(date_from) <= date.fromisoformat(date_to)

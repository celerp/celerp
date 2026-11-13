# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for fiscal-year-aware date presets in ui/routes/reports.py."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ui.routes.reports import _resolve_preset


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

    def test_this_fy_reads_a_malformed_start_as_january(self):
        assert _resolve_preset("this_fy", {"fiscal_year_start": "bad"}) == ("2026-01-01", "2026-06-15")

    def test_all_preset_returns_empty(self):
        assert _resolve_preset("all", {}) == ("", "")

    def test_unknown_preset_returns_empty(self):
        assert _resolve_preset("unknown_preset", {}) == ("", "")

    def test_default_fiscal_is_jan_when_unset(self):
        assert _resolve_preset("this_fy", {}) == _resolve_preset("this_fy", {"fiscal_year_start": "01-01"})


class TestFinancialSummaryRobustness:
    """The contact money cards count the fiscal year to date through the this_fy preset; a
    malformed fiscal_year_start (e.g. "01" with no day, leaked into the DB by other tests)
    must not 500 the contact page: it falls back to Jan 1."""

    @pytest.mark.parametrize("fy", ["01", "bad", "", "1-1", "04-01", "01-01"])
    def test_contact_year_to_date_survives_malformed_fiscal_year_start(self, fy):
        date_from, date_to = _resolve_preset("this_fy", {"fiscal_year_start": fy})
        assert date.fromisoformat(date_from) <= date.fromisoformat(date_to)

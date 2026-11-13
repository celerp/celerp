# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fiscal year arithmetic in core: the bounds of the fiscal year a day falls in, and
the start month read from a company's ``fiscal_year_start`` setting.

The bounds are checked against a plain calendar construction (the year runs from the
first of the start month to the day before the next one), for every start month and
for both edges of every month, a leap February included.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

import pytest


def _expected(day: date, start: int) -> tuple[date, date]:
    for year in (day.year - 1, day.year):
        first = date(year, start, 1)
        nxt = date(year + 1, start, 1)
        if first <= day < nxt:
            return first, nxt - timedelta(days=1)
    raise AssertionError(day)


def _edges(year: int):
    for month in range(1, 13):
        yield date(year, month, 1)
        yield date(year, month, calendar.monthrange(year, month)[1])


@pytest.mark.parametrize("start", range(1, 13))
def test_fiscal_year_bounds_for_every_start_month(start):
    from celerp.services.periods import fiscal_year_bounds

    for year in (2023, 2024):  # 2024 is a leap year
        for day in _edges(year):
            assert fiscal_year_bounds(day, start) == _expected(day, start), (day, start)
    assert fiscal_year_bounds(date(2024, 2, 29), 3) == (date(2023, 3, 1), date(2024, 2, 29))


def _last_end(day: date, start: int) -> date | None:
    """The plain construction: a fiscal year ends on the last day of the month before its start
    month, so the latest such day on or before ``day``; None when the calendar holds none."""
    month = (start - 2) % 12 + 1
    for year in range(day.year, 0, -1):
        end = date(year, month, calendar.monthrange(year, month)[1])
        if end <= day:
            return end
    return None


@pytest.mark.parametrize("start", range(1, 13))
def test_last_fiscal_year_end_for_every_start_month_and_calendar_edge(start):
    """The latest fiscal year end on or before a day, the day itself when it is one, for both
    edges of every month and at both ends of the calendar, where it is None (no year end yet)
    or a date, never an overflow."""
    from celerp.services.periods import last_fiscal_year_end

    days = [*_edges(2023), *_edges(2024), *_edges(1), *_edges(9999), date(2024, 2, 29)]
    for day in days:
        assert last_fiscal_year_end(day, start) == _last_end(day, start), (day, start)


def test_fiscal_year_start_month_refuses_a_malformed_start():
    """Missing or empty reads as January, the provisioning default; a value that is
    not the first of a month is refused, never guessed."""
    from celerp.services.periods import fiscal_year_start_month

    assert fiscal_year_start_month(None) == 1
    assert fiscal_year_start_month("") == 1
    assert [fiscal_year_start_month(f"{m:02d}-01") for m in range(1, 13)] == list(range(1, 13))
    for bad in ("bad", "1-1", "13-01", "04-15"):
        with pytest.raises(ValueError):
            fiscal_year_start_month(bad)


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


@pytest.mark.asyncio
async def test_fiscal_year_start_choices_come_from_one_list():
    """The settings writer, the Company Details choices and the page's check read one
    ordered list of valid starts."""
    from unittest.mock import AsyncMock, patch

    from httpx import ASGITransport, AsyncClient

    import ui.routes.settings as settings_page
    from celerp.services.periods import FISCAL_YEAR_STARTS
    from test_helpers import make_test_token
    from ui.app import app as ui_app

    assert FISCAL_YEAR_STARTS == tuple(f"{m:02d}-01" for m in range(1, 13))
    assert settings_page._FISCAL_MONTHS == [
        (v, f"settings.fiscal_month_{m}") for v, m in zip(FISCAL_YEAR_STARTS, _MONTHS)]
    assert not hasattr(settings_page, "_FISCAL_VALUES")

    sent = AsyncMock(return_value=None)
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://test") as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        with patch("ui.api_client.patch_company", new=sent):
            r = await c.patch("/settings/company/fiscal_year_start", data={"value": "13-01"})
    assert r.status_code == 200
    assert "cell-error" in r.text
    sent.assert_not_called()


def test_close_year_keys_are_in_every_locale():
    import json
    import pathlib

    locales = sorted((pathlib.Path(__file__).resolve().parents[1] / "ui" / "locales").glob("*.json"))
    assert len(locales) == 12
    for path in locales:
        catalog = json.loads(path.read_text(encoding="utf-8"))
        for key in ("accounting.year_end_not_fiscal", "accounting.fiscal_year_start_invalid",
                    "accounting.year_end_before_first"):
            assert catalog.get(key), (path.name, key)

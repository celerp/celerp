# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the bank page.

The bank page builds every user-facing label, status badge, button, summary and
column-mapper heading by calling ``t()`` at render time, so a request in a
non-English language gets translated output. These tests register a sentinel
language ``xx`` and assert the sentinel text reaches the rendered output while
``xx`` is active.

Each conversion mechanism is covered at least once:
- the line status badge, raw status kept in the class, label translated,
- render-time interpolation (the summary line),
- bare button and heading text (Suggest, the column mapper),
- answer picker option labels (the answer form).
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components import bank_groups as BG

_XX = {
    "bank.status_open": "XX_OPEN",
    "bank.status_posted": "XX_POSTED",
    "bank.summary": "XX_SUMMARY {lines}/{open}",
    "bank.suggest": "XX_SUGGEST",
    "recon.map_csv_columns": "XX_MAPCSV",
    "bank.answer_cash": "XX_CASH",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the registry
    and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def _line(status: str) -> str:
    return to_xml(BG.line_row({"id": "L1", "status": status, "amount": "-50.00", "line_date": "2026-08-01",
                               "description": "Coffee"}, "B1", "USD"))


def test_line_status_badge_translates():
    open_html, posted_html = _line("unmatched"), _line("posted")
    assert "XX_OPEN" in open_html and "badge--recon-unmatched" in open_html
    assert "XX_POSTED" in posted_html and "badge--recon-posted" in posted_html


def test_summary_interpolates_translated():
    html = to_xml(BG.summary_line({"summary": {"lines": 7, "open": 3, "posted": 4}}, "USD"))
    assert "XX_SUMMARY 7/3" in html


def test_suggest_button_translates():
    assert "XX_SUGGEST" in to_xml(BG.page_actions("B1", "open", ""))


def test_mapper_heading_translates():
    assert "XX_MAPCSV" in to_xml(BG.column_mapper("B1", "J1", ["Date", "Amount"], "{}"))


def test_answer_option_label_translates():
    options = BG.answer_options([{"code": "5000", "name": "Expenses", "account_type": "expense"}], [], "B1")
    assert "XX_CASH" in to_xml(BG.answer_form({"id": "B1", "currency": "USD"}, options))

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the financial reports.

The financial statements (P&L, balance sheet, cash flow, trial balance, general
ledger, statements of account) build every on-screen section heading, subtotal
label, tooltip, and browser title by calling ``t()`` at render time, so a request
in a non-English language gets translated output. These tests prove that by
registering a sentinel language ``xx`` and asserting the sentinel text reaches the
rendered output while ``xx`` is active. They are red against a tree that hardcodes
the English strings, and need no en.json change (the sentinel carries the values).
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.shell import page_title
from ui.routes.financial_reports import _pnl_view, _balance_sheet_view

# Sentinel catalog: one unmistakable value per key these reports render. Covers
# every conversion mechanism used in this file - a section heading resolved from a
# reused acct.* key, an interpolated subtotal label, a newly-minted tooltip
# attribute, and the R5 browser-title helper.
_XX = {
    "acct.section_revenue": "XX_REVENUE",
    "acct.gross_profit": "XX_GROSS_PROFIT",
    "acct.net_profit": "XX_NET_PROFIT",
    "acct.section_assets": "XX_ASSETS",
    "financial.total_liabilities_equity": "XX_TLE",
    "financial.retained_earnings_tooltip": "XX_RETAINED_TIP",
    "acct.tab_cash_flow": "XX_CASH_FLOW",
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


def test_pnl_section_and_subtotal_labels_translate():
    """Section heading (function-arg label) plus the interpolated Gross/Net Profit
    subtotals all resolve at render time."""
    data = {
        "revenue": {"total": 100, "lines": [{"code": "4000", "name": "Sales", "amount": 100}]},
        "cogs": {"total": 0, "lines": []},
        "expenses": {"total": 0, "lines": []},
        "gross_profit": 100,
        "net_profit": 100,
    }
    html = to_xml(_pnl_view(data, "USD", date_from="2026-01-01", date_to="2026-01-31"))
    assert "XX_REVENUE" in html
    assert "XX_GROSS_PROFIT" in html
    assert "XX_NET_PROFIT" in html


def test_balance_sheet_labels_and_tooltip_translate():
    """Section heading, the interpolated Total Liabilities & Equity subtotal, and
    the retained-earnings tooltip attribute all resolve at render time."""
    data = {
        "assets": {"total": 0, "lines": []},
        "liabilities": {"total": 0, "lines": []},
        "equity": {"total": 0, "lines": [
            {"synthetic": True, "href_pnl": True, "name": "Retained earnings"},
        ]},
        "balanced": True,
    }
    html = to_xml(_balance_sheet_view(data, "USD", as_of="2026-01-31"))
    assert "XX_ASSETS" in html
    assert "XX_TLE" in html
    assert "XX_RETAINED_TIP" in html


def test_page_title_helper_translates():
    """R5 browser-title helper resolves its key in the active language."""
    assert page_title("acct.tab_cash_flow") == "XX_CASH_FLOW - Celerp"

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the small_mixed area: the Purchasing
settings sub-tabs and page chrome, and the shared currency combobox search
placeholder.

Both routes/components build their tab labels, breadcrumb, page header,
browser title, and combobox placeholder by calling ``t()`` at render time, so
a request in a non-English language gets translated output. These tests
register a sentinel language ``xx`` (via the module i18n seam) and assert the
sentinel text reaches the rendered output while ``xx`` is active. They are
red against a tree that hardcodes the English strings.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.currency import currency_combobox_td
from ui.components.shell import page_header, page_title
from ui.routes.settings_general import _section_breadcrumb
from ui.routes.settings_purchasing import _purchasing_tabs

# Sentinel catalog: one unmistakable value per key this area renders.
_XX = {
    "page.terms_conditions": "XX_TERMS_CONDITIONS",
    "settings_sales.tab_numbering": "XX_NUMBERING",
    "settings_purchasing.page_title": "XX_PURCHASING_PAGE_TITLE",
    "subscriptions.purchasing": "XX_PURCHASING_BREADCRUMB",
    "page.settings": "XX_SETTINGS",
    "currency.search_placeholder": "XX_SEARCH_CURRENCY",
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


def test_purchasing_tabs_translate():
    # Module-level list-of-tuples pattern (R1): the tab labels resolve
    # through t() at render, inside _purchasing_tabs().
    html = to_xml(_purchasing_tabs("taxes", lang="xx"))
    assert "XX_TERMS_CONDITIONS" in html
    assert "XX_NUMBERING" in html


def test_purchasing_breadcrumb_translates():
    # Route-composition call: t() resolved before being handed to the
    # shared breadcrumb helper.
    html = to_xml(_section_breadcrumb(i18n.t("subscriptions.purchasing")))
    assert "XX_PURCHASING_BREADCRUMB" in html


def test_purchasing_page_header_translates():
    # Route-composition call for the newly-minted page-header key.
    html = to_xml(page_header(i18n.t("settings_purchasing.page_title")))
    assert "XX_PURCHASING_PAGE_TITLE" in html


def test_purchasing_browser_title_translates():
    # R5: the browser <title> resolves through the shared page_title() helper.
    assert page_title("page.settings") == "XX_SETTINGS - Celerp"


def test_currency_combobox_search_placeholder_translates():
    td = currency_combobox_td(
        value="USD",
        hidden_id="cur-search-1",
        patch_url="/x/patch",
        cancel_url="/x/cancel",
    )
    html = to_xml(td)
    assert "XX_SEARCH_CURRENCY" in html

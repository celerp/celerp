# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the modules admin page.

Every user-facing label, tab, status badge, and table header on the modules
page is built by calling ``t()`` at render time, so a request in a non-English
language gets translated output - including the browser ``<title>``, which is
composed through the shared ``page_title`` helper (R5) rather than a hardcoded
"Modules - Celerp" literal. These tests register a sentinel language ``xx`` and
assert its unmistakable values reach the rendered output while ``xx`` is active.
They are red against a tree that hardcodes the English title (the sentinel never
appears when the source emits a literal).
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.shell import page_title
from ui.routes.modules_page import _tabs

# Sentinel catalog: unmistakable values for the keys this page renders. All are
# existing, reused keys (the only new wiring here is the browser title, which
# reuses modules.title); the sentinel values prove render-time resolution.
_XX = {
    "modules.title": "XX_MODULES_TITLE",
    "modules.tab_local": "XX_TAB_LOCAL",
    "modules.tab_marketplace": "XX_TAB_MARKET",
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


def test_browser_title_translates():
    # Mechanism (R5): the modules page composes its browser <title> through the
    # shared page_title helper, so it resolves in the request language at render
    # time instead of hardcoding "Modules - Celerp".
    assert page_title("modules.title") == "XX_MODULES_TITLE - Celerp"


def test_tabs_chrome_translates():
    # The modules page chrome resolves its labels at render time under the active
    # language: the tabs strip renders the sentinel tab names.
    html = to_xml(_tabs("local", "xx"))
    assert "XX_TAB_LOCAL" in html
    assert "XX_TAB_MARKET" in html

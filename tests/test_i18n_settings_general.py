# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the General settings breadcrumb chrome.

``_section_breadcrumb`` builds its breadcrumb link and current-section label by
calling ``t()`` at render time, so a request in a non-English language gets
translated output for both the reused ``nav.settings`` link and the section
label key it is passed. This test proves that by registering a sentinel
language ``xx`` (via the module i18n seam) and asserting the sentinel text
reaches the rendered output while ``xx`` is the active language. It is red
against a tree that hardcodes the "General" section string or resolves the
breadcrumb chrome at import time.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings_general import _section_breadcrumb

# Sentinel catalog: one unmistakable value per key the breadcrumb renders.
_XX = {
    "nav.settings": "XX_NAV_SETTINGS",
    "settings_general.breadcrumb_general": "XX_GENERAL",
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


def test_breadcrumb_section_label_translates():
    html = to_xml(_section_breadcrumb("settings_general.breadcrumb_general"))
    assert "XX_GENERAL" in html


def test_breadcrumb_link_reuses_nav_settings_key():
    html = to_xml(_section_breadcrumb("settings_general.breadcrumb_general"))
    assert "XX_NAV_SETTINGS" in html

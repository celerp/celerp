# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the shared app shell (nav, page chrome,
global search help, backup banner, star-supporter card, and the JS bundle's
translated config).

These tests prove the shell builds every user-facing label, banner string, and
JS-bound string by calling ``t()`` at render time, so a request in a
non-English language gets translated output. They register a sentinel
language ``xx`` (via the module i18n seam) and assert the sentinel text
reaches the rendered output while ``xx`` is the active language. They are red
against a tree that resolves any of these strings at import time or hardcodes
English.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.shell import (
    page_title,
    _backup_banner_html,
    _shell_js_i18n,
    _sidebar,
    search_help,
    star_supporter_card,
)

# Sentinel catalog: one unmistakable value per key these tests exercise.
_XX = {
    "nav.settings": "XX_SETTINGS",
    "shell.backup_in_progress": "XX_BACKUP_IN_PROGRESS",
    "shell.search_help_plain_text": "XX_PLAIN_TEXT",
    "shell.copied": "XX_COPIED",
    "shell.star_on_github": "XX_STAR_ON_GITHUB",
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


def test_page_title_composes_translated_title():
    assert page_title("nav.settings") == "XX_SETTINGS - Celerp"


def test_sidebar_nav_label_translates():
    xml = to_xml(_sidebar("settings", lang="xx", role="owner", request=None, settings={}))
    assert "XX_SETTINGS" in xml


def test_backup_banner_translates():
    xml = to_xml(_backup_banner_html("xx"))
    assert "XX_BACKUP_IN_PROGRESS" in xml


def test_search_help_panel_translates():
    xml = to_xml(search_help(lang="xx"))
    assert "XX_PLAIN_TEXT" in xml


def test_shell_js_config_translates():
    # R2: strings baked into the static JS bundle are resolved here in Python and
    # handed to the client as a config object (window.__shellI18n), never spliced
    # into JS source. This is the config-builder base_shell() injects.
    config = _shell_js_i18n("xx")
    assert config["copied"] == "XX_COPIED"


def test_star_supporter_card_js_fallback_data_attr_translates():
    # R2, second mechanism: star_supporter_card() also renders inside auth_shell()
    # pages that carry no window.__shellI18n config, so its JS fallback strings are
    # translated in Python and passed via the card's own data-* attributes instead.
    xml = to_xml(star_supporter_card("dashboard"))
    assert "XX_STAR_ON_GITHUB" in xml

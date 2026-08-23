# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the Contacts settings page.

The contacts-settings route builds its tab labels, tag-table chrome, the
delete-tag confirmation, the category placeholder, and the browser title by
calling ``t()`` at render time, so a request in a non-English language gets
translated output. These tests register a sentinel language ``xx`` (via the
module i18n seam) and assert the sentinel text reaches the rendered output
while ``xx`` is active. They are red against a tree that hardcodes the English
strings.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.shell import page_title
from ui.routes.settings_contacts import _contacts_tabs, _tags_tab

# Sentinel catalog: one unmistakable value per newly-minted key this page renders.
_XX = {
    "settings_contacts.tab_defaults": "XX_DEFAULTS_TAB",
    "settings_contacts.page_header": "XX_CONTACTS_SETTINGS",
    "settings_contacts.confirm_delete_tag": "XX_DELETE_TAG {name}",
    "settings_contacts.tag_category_placeholder": "XX_CATEGORY_PLACEHOLDER",
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


def test_tab_labels_translate():
    # Render-time label list: the "Defaults" tab resolves through t() at render.
    html = to_xml(_contacts_tabs("tags"))
    assert "XX_DEFAULTS_TAB" in html


def test_delete_tag_confirm_translates_with_name():
    # R2: the hx_confirm value is JS-consumed chrome; it is translated in Python
    # with the tag name interpolated, never spliced into JS source.
    html = to_xml(_tags_tab([{"name": "Acme", "color": "", "category": ""}]))
    assert "XX_DELETE_TAG Acme" in html


def test_tag_category_placeholder_translates():
    html = to_xml(_tags_tab([]))
    assert "XX_CATEGORY_PLACEHOLDER" in html


def test_page_title_translates():
    # R5: the browser <title> resolves the page-header key through the shared helper.
    assert page_title("settings_contacts.page_header") == "XX_CONTACTS_SETTINGS - Celerp"

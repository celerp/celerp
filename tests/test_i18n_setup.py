# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the setup / onboarding wizard.

The setup wizard builds its placeholders, cloud-upsell subtitle, feature list,
and skip link by calling ``t()`` at render time, so a request in a non-English
language gets translated output. These tests prove that by registering a
sentinel language ``xx`` (via the module i18n seam) and asserting the sentinel
text reaches the rendered output while ``xx`` is the active language. They are
red against a tree that hardcodes the English strings.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.setup import _company_details_form, _cloud_form

# Sentinel catalog: one unmistakable value per key the wizard renders at render time.
_XX = {
    "setup.address_placeholder": "XX_ADDR_PH",
    "setup.currency_search_placeholder": "XX_CUR_SEARCH",
    "setup.cloud_subtitle": "XX_CLOUD_SUB",
    "setup.feature_connectors_title": "XX_FEAT_CONN",
    "setup.feature_connectors_desc": "XX_FEAT_CONN_DESC",
    "setup.feature_ai_title": "XX_FEAT_AI",
    "setup.skip_for_now": "XX_SKIP",
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


def test_company_details_placeholders_translate():
    """Textarea / currency-search placeholders resolve through t() at render."""
    html = to_xml(_company_details_form({}))
    assert "XX_ADDR_PH" in html
    assert "XX_CUR_SEARCH" in html


def test_cloud_form_subtitle_translates():
    """The cloud-upsell subtitle resolves through t() at render."""
    html = to_xml(_cloud_form())
    assert "XX_CLOUD_SUB" in html


def test_cloud_form_features_translate():
    """Feature-list title/description labels resolve through t() at render."""
    html = to_xml(_cloud_form())
    assert "XX_FEAT_CONN" in html
    assert "XX_FEAT_CONN_DESC" in html
    assert "XX_FEAT_AI" in html


def test_cloud_form_skip_link_translates():
    """The 'skip for now' link text resolves through t() at render."""
    html = to_xml(_cloud_form())
    assert "XX_SKIP" in html

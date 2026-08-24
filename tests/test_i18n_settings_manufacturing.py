# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for Settings -> Manufacturing.

The manufacturing-settings route builds its headings, toggle labels, help and
info-tooltip text, and the work-centers card chrome by calling ``t()`` at render
time, so a request in a non-English language gets translated output. These tests
prove that by registering a sentinel language ``xx`` (via the module i18n seam)
and asserting the sentinel text reaches the rendered output while ``xx`` is the
active language. They are red against a tree that hardcodes the English.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings_manufacturing import (
    _production_rules_form,
    _work_centers_card,
)

# Sentinel catalog: one unmistakable value per conversion MECHANISM used in the
# route - a section heading, a toggle-label plain string, an info-tooltip whose
# text is handed to aria-label/data-tip attributes, and the work-centers card
# heading plus its action button.
_XX = {
    "settings_manufacturing.production_rules": "XX_PROD_RULES",
    "settings_manufacturing.require_issued_label": "XX_REQUIRE_ISSUED",
    "settings_manufacturing.require_issued_info": "XX_REQUIRE_INFO",
    "settings_manufacturing.per_workcenter_hours_note": "XX_HOURS_NOTE",
    "settings_manufacturing.work_centers": "XX_WORK_CENTERS",
    "settings_manufacturing.work_centers_info": "XX_WC_INFO",
    "settings_manufacturing.add_work_center": "XX_ADD_WC",
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


def test_production_rules_form_headings_and_labels_translate():
    """Section heading and a toggle label render in the request language."""
    html = to_xml(_production_rules_form(False, False, False))
    assert "XX_PROD_RULES" in html
    assert "XX_REQUIRE_ISSUED" in html
    assert "XX_HOURS_NOTE" in html


def test_production_rules_form_info_tooltip_translates():
    """Info-tooltip mechanism: the explanation reaches the aria-label/data-tip
    attributes translated (text passed from Python into attributes, R2)."""
    html = to_xml(_production_rules_form(False, False, False))
    assert "XX_REQUIRE_INFO" in html


def test_work_centers_card_chrome_translates():
    """Work-centers card heading, its info tooltip, and the action button all
    render in the request language."""
    html = to_xml(_work_centers_card([], {}))
    assert "XX_WORK_CENTERS" in html
    assert "XX_WC_INFO" in html
    assert "XX_ADD_WC" in html

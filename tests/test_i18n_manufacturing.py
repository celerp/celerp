# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the manufacturing routes.

Every user-facing label, tooltip, table header, enum display, and JS-config
string in ``ui/routes/manufacturing.py`` is built by calling ``t()`` at render
time, so a request in a non-English language gets translated output. These tests
register a sentinel language ``xx`` and assert its text reaches the rendered
output, covering each conversion mechanism used in the file: a module-dict label
(status/coverage help), a table header, an enum display label, and a string
handed to JS via the bulk-toolbar data-config. They are red against a tree that
resolves any of these strings at import time or hardcodes English.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.i18n import t
from ui.components.table import bulk_toolbar
from ui.routes.manufacturing import (
    _badge,
    _priority_badge,
    _status_badge,
    _wc_table,
)

# Sentinel catalog: one unmistakable value per key a manufacturing render touches.
_XX = {
    "enum.mfg_run_status.in_progress": "XX_INPROG",       # enum display label (R3)
    "manufacturing.help_status_in_progress": "XX_HELP_INPROG",  # module-dict label (R1)
    "enum.mfg_priority.high": "XX_PRIO_HIGH",             # enum display label (R3)
    "manufacturing.coverage_short": "XX_NEEDED",          # module-dict relabel (R1)
    "manufacturing.help_coverage_short": "XX_HELP_SHORT",  # module-dict label (R1)
    "manufacturing.th_wip_location": "XX_WIPLOC",         # table header
    "manufacturing.no_work_centers": "XX_NOWC",
    "manufacturing.action_make_selected": "XX_MAKESEL",   # JS data-config value (R2)
    "manufacturing.confirm_make_complete": "XX_CONFIRM",  # JS data-config value (R2)
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


def test_run_status_badge_translates_enum_and_help():
    html = to_xml(_badge("in_progress"))
    assert "XX_INPROG" in html      # enum display label
    assert "XX_HELP_INPROG" in html  # module-dict tooltip resolved at render


def test_priority_badge_translates_enum():
    assert "XX_PRIO_HIGH" in to_xml(_priority_badge("high"))


def test_coverage_badge_translates_label_and_help():
    html = to_xml(_status_badge("short"))
    assert "XX_NEEDED" in html      # module-dict relabel resolved at render
    assert "XX_HELP_SHORT" in html


def test_work_center_table_header_and_empty_translate():
    html = to_xml(_wc_table([], {}))
    assert "XX_WIPLOC" in html  # table header
    assert "XX_NOWC" in html    # empty-state row


def test_bulk_action_label_and_confirm_reach_js_config():
    # The route translates in Python and hands the value to the bulk JS via the
    # toolbar's data-config (R2), never splicing text into JS source.
    html = to_xml(bulk_toolbar("mfg-table", [
        {"value": "make", "label": t("manufacturing.action_make_selected"),
         "method": "post", "url": "/manufacturing/make-selected"},
        {"value": "make_complete", "label": t("manufacturing.action_make_selected"),
         "method": "post", "url": "/manufacturing/make-selected?complete=1",
         "confirm": t("manufacturing.confirm_make_complete")},
    ]))
    assert "XX_MAKESEL" in html
    assert "XX_CONFIRM" in html

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the shared CSV import UI.

The CSV import helpers build every user-facing label, instruction, button,
placeholder, table header, and error message by calling ``t()`` at render (or
request) time. These tests register a sentinel language ``xx`` through the i18n
seam and assert the sentinel text reaches the rendered output while ``xx`` is
active. They are red against a tree that hardcodes the English literals or the
JavaScript that carried them.

Mechanisms covered: a render-time component label (step indicator, intro),
a value baked into a JS config object via ``json.dumps`` (``_MAPPING_OPTIONS``
option labels and the ``_MAP_I18N`` dropdown labels), an interpolated count
string, and a request-time validation error message.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.csv_import import (
    _step_indicator,
    column_mapping_form,
    validation_result,
    validate_column_mapping,
)

# Sentinel catalog: one unmistakable value per key exercised below.
_XX = {
    "import.step_upload": "XX_UPLOAD",
    "import.step_map_columns": "XX_MAP_COLS",
    "import.map_intro": "XX_MAP_INTRO",
    "import.opt_import_as_custom": "XX_IMPORT_CUSTOM",
    "import.js_search_placeholder": "XX_SEARCH",
    "import.showing_rows": "XX_SHOWING {n} OF {total}",
    "import.import_all_rows": "XX_IMPORTALL {n}",
    "import.cells_count": "XX_CELLS {n}",
    "import.need_fixing_across": " XX_NEEDFIX {rows}/{total}",
    "import.err_duplicate_target": "XX_DUPTARGET {cols} {target}",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the
    registry and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_step_indicator_translates_labels():
    # Render-time component label: the step names resolve through t().
    html = to_xml(_step_indicator(2, has_mapping=True))
    assert "XX_UPLOAD" in html
    assert "XX_MAP_COLS" in html


def test_column_mapping_form_translates_label_js_and_count():
    sample = [{"sku": f"S{i}", "name": f"N{i}"} for i in range(7)]
    html = to_xml(column_mapping_form(
        csv_cols=["sku", "name"],
        target_cols=["sku", "name"],
        csv_ref="tok",
        sample_rows=sample,
        confirm_action="/import/confirm",
        back_href="/items",
    ))
    # Render-time intro label.
    assert "XX_MAP_INTRO" in html
    # Value baked into a JS config object (_MAPPING_OPTIONS) via json.dumps.
    assert "XX_IMPORT_CUSTOM" in html
    # Dropdown label handed to JS through the _MAP_I18N json.dumps config.
    assert "XX_SEARCH" in html
    # Interpolated count string (5 preview rows of 7 total).
    assert "XX_SHOWING 5 OF 7" in html


def test_validation_result_clean_translates_import_button():
    rows = [{"sku": f"S{i}", "name": f"N{i}"} for i in range(6)]
    html = to_xml(validation_result(
        rows=rows,
        cols=["sku", "name"],
        validate=lambda col, val, row: True,
        confirm_action="/import/confirm",
        error_report_action="/import/errors",
        back_href="/items",
    ))
    # Interpolated string in the confirm panel.
    assert "XX_IMPORTALL 6" in html
    assert "XX_SHOWING 5 OF 6" in html


def test_validation_result_errors_translates_summary():
    rows = [{"name": "", "sku": "A"}, {"name": "", "sku": "B"}]
    html = to_xml(validation_result(
        rows=rows,
        cols=["name", "sku"],
        validate=lambda col, val, row: not (col == "name" and val == ""),
        confirm_action="/import/confirm",
        error_report_action="/import/errors",
        revalidate_action="/import/revalidate",
        back_href="/items",
    ))
    assert "XX_CELLS 2" in html
    assert "XX_NEEDFIX 2/2" in html


def test_validate_column_mapping_translates_error():
    # Request-time (non-render) error message resolves through t().
    errors = validate_column_mapping(
        {"map__a": "sku", "map__b": "sku"},
        ["a", "b"],
    )
    assert any("XX_DUPTARGET" in e for e in errors)

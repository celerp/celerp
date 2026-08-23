# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time translation of built-in item-schema field labels.

The item field schema's built-in fields carry a ``label_key`` (mirroring the
existing ``tooltip_key``), and the UI resolves it through ``t()`` at render time
via ``ui.i18n.field_label``. A request in a non-English language gets a
translated column header, detail label, and column-manager label. Stored custom
fields and dynamic price columns carry no ``label_key`` and keep their raw
``label`` (a user-defined label is data, never translated).

These tests register a sentinel language ``xx`` and assert the sentinel text
reaches the rendered output while ``xx`` is active. They are red against a tree
that hardcodes ``f["label"]`` at the render sites or lacks the ``label_key``
mechanism entirely.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.table import column_manager
from ui.routes.inventory import _detail_table
from celerp.services.field_schema import _BASE_FIELDS

# One unmistakable sentinel per label_key exercised by the render tests.
_XX = {
    "field.label.sku": "XX_SKU",
    "field.label.quantity": "XX_QTY",
    "field.label.status": "XX_STATUS",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the
    registry and the context language afterwards so nothing leaks between
    tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def _schema():
    return [
        {"key": "sku", "label": "SKU", "type": "text", "show_in_table": True,
         "label_key": "field.label.sku"},
        {"key": "quantity", "label": "Qty", "type": "number", "show_in_table": True,
         "label_key": "field.label.quantity", "tooltip_key": "field.tooltip.quantity"},
    ]


def test_every_builtin_field_carries_its_label_key():
    """Every built-in field opts into translation with label_key == field.label.<key>."""
    missing = [f["key"] for f in _BASE_FIELDS
               if f.get("label_key") != f"field.label.{f['key']}"]
    assert missing == [], f"built-in fields missing label_key: {missing}"


def test_field_label_resolves_label_key_through_t():
    """field_label routes a field's label_key through t() under the active language."""
    from ui.i18n import field_label
    assert field_label({"key": "sku", "label": "SKU",
                        "label_key": "field.label.sku"}) == "XX_SKU"


def test_field_label_falls_back_to_raw_label_without_label_key():
    """A stored custom field (no label_key) keeps its raw, user-defined label."""
    from ui.i18n import field_label
    assert field_label({"key": "custom", "label": "My Custom Field"}) == "My Custom Field"


def test_field_label_falls_back_to_english_for_unregistered_key():
    """A built-in whose key the sentinel catalog omits falls back to English, not blank."""
    from ui.i18n import field_label
    # A built-in absent from _XX forces the en fallback (locale -> en -> key).
    notes = next(f for f in _BASE_FIELDS if f["key"] == "notes")
    assert field_label(notes) == "Notes"


def test_column_manager_translates_builtin_labels():
    """The column-manager picker shows translated built-in field labels."""
    html = to_xml(column_manager(_schema(), "item"))
    assert "XX_SKU" in html
    assert "XX_QTY" in html


def test_detail_table_translates_builtin_field_label():
    """The item detail card renders translated built-in field labels."""
    html = to_xml(_detail_table("eid", {"sku": "A1", "quantity": 5}, _schema()))
    assert "XX_SKU" in html
    assert "XX_QTY" in html

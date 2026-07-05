# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Proves the optional `placeholder` on editable_cell/display_cell is a hint-only,
strictly-additive change: existing (no-placeholder) callers are unchanged, the cell
stays editable, and the suggestion can never become the stored value.
"""
from __future__ import annotations

from fasthtml.common import to_xml

from ui.components.table import EMPTY, display_cell, editable_cell


def _html(ft) -> str:
    return to_xml(ft)


# ── editable_cell: additive, editable, hint-only ──────────────────────────────

def test_editable_cell_without_placeholder_is_unchanged():
    """Backward-compat: no placeholder arg -> no placeholder attribute at all."""
    html = _html(editable_cell("item:1", "reorder_qty", 5, cell_type="number"))
    assert "placeholder" not in html
    assert 'value="5"' in html


def test_editable_cell_placeholder_is_a_hint_never_the_value():
    """Empty field + suggestion: the input shows placeholder="24" but its submitted
    value is empty - the suggestion is not prefilled, so it can never be saved."""
    html = _html(editable_cell("item:1", "reorder_qty", "", cell_type="number", placeholder="24"))
    assert 'placeholder="24"' in html
    assert 'name="value"' in html
    assert 'value="24"' not in html  # suggestion is NOT the value


def test_editable_cell_keeps_existing_value_editable_alongside_hint():
    """A stored value stays the editable value; the placeholder is just a hint."""
    html = _html(editable_cell("item:1", "reorder_qty", 30, cell_type="number", placeholder="24"))
    assert 'value="30"' in html
    assert 'placeholder="24"' in html


# ── display_cell: additive, grey only when empty, still click-to-edit ──────────

def test_display_cell_without_placeholder_empty_is_unchanged():
    html = _html(display_cell("item:1", "reorder_qty", "", cell_type="number"))
    assert "cell-suggestion" not in html
    assert "hx-get" in html  # still double-click-to-edit


def test_display_cell_empty_shows_grey_suggestion_and_stays_editable():
    html = _html(display_cell("item:1", "reorder_qty", "", cell_type="number", placeholder="24"))
    assert "cell-suggestion" in html and "24" in html
    assert "hx-get" in html  # edit trigger intact -> the cell is still editable


def test_display_cell_with_value_ignores_placeholder():
    """When the field has a real value, the suggestion is not shown."""
    html = _html(display_cell("item:1", "reorder_qty", 30, cell_type="number", placeholder="24"))
    assert "cell-suggestion" not in html
    assert "30" in html


def test_display_cell_empty_constant_treated_as_empty():
    """A value equal to the EMPTY sentinel also surfaces the suggestion."""
    html = _html(display_cell("item:1", "reorder_qty", EMPTY, cell_type="number", placeholder="24"))
    assert "cell-suggestion" in html

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""An empty value reads as one "--" mark wherever the inventory table shows it, and the
quantity/weight totals chips name their unit without needing a plural form."""

from __future__ import annotations

import html
import re
from pathlib import Path

from fasthtml.common import to_xml

_UMAP = {
    "carat": {"name": "carat", "unit_type": "weight", "decimals": 2},
    "piece": {"name": "piece", "unit_type": "pieces", "decimals": 0},
}


def _text(ft) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", to_xml(ft))).strip()


def _renderers():
    from celerp.services.field_schema import _BASE_FIELDS
    from ui.routes.inventory import _inventory_cell_renderers
    return _inventory_cell_renderers(_BASE_FIELDS, units_map=_UMAP)


def test_paired_cell_with_both_values_empty_shows_a_single_mark():
    from ui.components.table import paired_display_cell
    td = paired_display_cell(
        entity_id="item:1", primary_field="weight", primary_value="",
        secondary_field="weight_unit", secondary_value="",
    )
    assert _text(td) == "--"
    s = to_xml(td)
    assert "paired-sep" not in s
    # The one mark still opens the editor, so the empty cell stays click-to-edit.
    assert "/field/weight/paired-edit" in s


def test_paired_cell_keeps_both_spans_once_a_value_is_present():
    from ui.components.table import paired_display_cell
    td = paired_display_cell(
        entity_id="item:1", primary_field="weight", primary_value="2.5",
        secondary_field="weight_unit", secondary_value="",
    )
    assert _text(td) == "2.5 --"


def test_net_weight_of_a_piece_row_without_weight_is_a_single_mark():
    td = _renderers()["weight"]("item:1", {"sell_by": "piece", "quantity": 8, "weight": "", "weight_unit": ""})
    assert _text(td) == "--"


def test_pieces_of_a_carat_row_marks_empty_outside_the_monospace_font():
    # Number cells use the monospace font, which spaces "--" out to "- -". The empty
    # mark carries its own class so it renders in the body font like every other "--".
    td = _renderers()["pieces"]("item:1", {"sell_by": "carat", "quantity": 5, "pieces": ""})
    assert _text(td) == "--"
    assert 'class="cell-empty"' in to_xml(td)


def test_empty_number_and_money_cells_use_the_empty_mark():
    from ui.components.table import _display_val
    for cell_type in ("number", "money", "rate", "date", "weight", "text"):
        assert 'class="cell-empty"' in to_xml(_display_val("", cell_type)), cell_type


def test_empty_mark_css_resets_the_monospace_font():
    css = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.css").read_text()
    rule = re.search(r"\.cell-empty\s*\{([^}]*)\}", css)
    assert rule and "font-family: var(--font)" in rule.group(1)


def test_quantity_and_weight_chips_name_the_unit_without_a_plural_form():
    from ui.routes.inventory import _valuation_bar
    bar = to_xml(_valuation_bar({
        "item_count": 3, "quantity_by_unit": {"piece": 8, "carat": 1},
        "weight_by_unit": {"carat": 4.5}, "pieces_total": None,
    }, lang="en"))
    chips = [html.unescape(c) for c in re.findall(r'<span class="val-chip">(.*?)</span>', bar, re.S)]
    assert chips == ["Items: 3", "Quantity (piece): 8", "Quantity (carat): 1", "Weight (carat): 4.5"]

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pure tests for the Manufacturing-tab form parsing (no DB)."""
from __future__ import annotations

from ui.routes.inventory import _parse_recipe_form, _recipe_to_payload


def test_parse_keeps_partial_rows_and_gaps() -> None:
    # index 1 is missing (a removed middle row before re-render) — must not stop parsing.
    form = {
        "output_qty": "2",
        "comp_item_0": "item:a", "comp_qty_0": "3", "comp_unit_0": "pieces",
        "comp_item_2": "", "comp_qty_2": "",
        "labor_op_0": "Assembly", "labor_hours_0": "1", "labor_rate_0": "40",
        "oh_desc_0": "Packaging", "oh_amount_0": "1.5",
    }
    rows = _parse_recipe_form(form)
    assert rows["output_qty"] == "2"
    assert len(rows["components"]) == 2          # the blank row is kept (not lost) for re-render
    assert rows["components"][0]["item_id"] == "item:a"
    assert len(rows["labor"]) == 1 and len(rows["overhead"]) == 1


def test_payload_drops_blank_rows_and_casts_numbers() -> None:
    rows = {
        "output_qty": "5",
        "components": [
            {"item_id": "item:a", "quantity": "3", "unit": "pieces"},
            {"item_id": "", "quantity": "9", "unit": ""},        # no item → dropped
            {"item_id": "item:b", "quantity": "0", "unit": ""},  # qty 0 → dropped
        ],
        "labor": [
            {"operation": "Weld", "kind": "hourly", "hours": "2", "rate": "30", "amount": ""},
            {"operation": "Setup", "kind": "fixed", "hours": "", "rate": "", "amount": "25"},
            {"operation": "", "hours": "9", "rate": "9"},  # blank op → dropped
        ],
        "overhead": [{"description": "Box", "amount": "1.2"}, {"description": "", "amount": "5"}],
    }
    payload = _recipe_to_payload(rows)
    assert payload["output_qty"] == 5.0
    # Component unit is no longer entered here — it is derived server-side from the item's sell unit.
    assert payload["components"] == [{"item_id": "item:a", "quantity": 3.0}]
    assert payload["labor"] == [
        {"operation": "Weld", "kind": "hourly", "hours": 2.0, "rate": 30.0, "amount": 0.0, "source": "manual"},
        {"operation": "Setup", "kind": "fixed", "hours": 0.0, "rate": 0.0, "amount": 25.0, "source": "manual"},
    ]
    assert payload["overhead"] == [{"description": "Box", "amount": 1.2}]


def test_payload_output_qty_defaults_to_one() -> None:
    assert _recipe_to_payload({"output_qty": "", "components": [], "labor": [], "overhead": []})["output_qty"] == 1.0

"""The inventory list and recipe name the default unit in German, like the item page does.

The price-cell unit annotation, the derived weight cell, the valuation chips and the
recipe materials tables (editor and work sheet) still print the stored unit name
("piece", "gram")."""

from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from ui import i18n


@pytest.fixture
def german():
    i18n.set_lang("de")
    yield
    i18n.set_lang("en")


def test_list_price_cell_names_the_unit_in_german(german):
    from ui.routes.inventory import _inventory_cell_renderers
    schema = [{"key": "retail_price", "label": "Retail", "type": "money", "editable": True},
              {"key": "sell_by", "label": "Sell by", "type": "select", "editable": True}]
    r = _inventory_cell_renderers(schema, unit_names=["piece"], currency="USD")
    xml = to_xml(r["retail_price"]("item:1", {"retail_price": 10, "sell_by": "piece"}))
    assert "/ Stück" in xml and "/ piece" not in xml, xml


def test_list_derived_weight_names_the_unit_in_german(german):
    from ui.routes.inventory import _inventory_cell_renderers
    schema = [{"key": "weight", "label": "Weight", "type": "number", "editable": True},
              {"key": "weight_unit", "label": "Unit", "type": "select", "editable": True},
              {"key": "sell_by", "label": "Sell by", "type": "select", "editable": True}]
    umap = {"gram": {"name": "gram", "decimals": 2, "unit_type": "weight"}}
    r = _inventory_cell_renderers(schema, unit_names=["gram"], units_map=umap, currency="USD")
    xml = to_xml(r["weight"]("item:1", {"quantity": 5, "sell_by": "gram"}))
    assert "Gramm" in xml and " gram<" not in xml, xml


def test_valuation_chips_name_the_unit_in_german(german):
    from ui.routes.inventory import _valuation_bar
    xml = to_xml(_valuation_bar({"item_count": 1, "quantity_by_unit": {"piece": 3},
                                 "weight_by_unit": {"gram": 100}}, "USD", "de"))
    assert "(Stück): 3" in xml and "(Gramm): 100" in xml, xml


_RECIPE_ITEM = {"id": "item:p", "name": "Ring", "recipe": {"components": [
    {"item_id": "item:c", "sku": "C-1", "quantity": 2, "unit": "piece"}]}}
_COMPONENT = {"id": "item:c", "name": "Clasp", "sku": "C-1"}


def test_recipe_materials_name_the_unit_in_german(german):
    from ui.routes.inventory import _recipe_section
    xml = to_xml(_recipe_section("item:p", _RECIPE_ITEM, [_COMPONENT], "USD"))
    assert ">Stück<" in xml and ">piece<" not in xml, xml


def test_worksheet_materials_name_the_unit_in_german(german):
    from ui.routes.inventory import _worksheet_print_view
    xml = to_xml(_worksheet_print_view("item:p", _RECIPE_ITEM, [_COMPONENT], "2026-10-05"))
    assert ">Stück<" in xml and ">piece<" not in xml, xml

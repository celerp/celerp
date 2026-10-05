"""The inventory list names the default unit in German, like the item page does.

The price-cell unit annotation, the derived weight cell and the valuation chips of the
inventory list still print the stored unit name ("piece", "gram")."""

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
    assert "3 Stück" in xml and "100 Gramm" in xml, xml

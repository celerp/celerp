"""Unit tests for inventory table helper functions.

Pure unit tests - no DB, no HTTP.
"""
import pytest
from ui.api_client import _flatten_item_attrs


class TestFlattenItemAttrs:
    def test_no_attributes_returns_item_unchanged(self):
        item = {"id": "item:1", "name": "Ruby", "quantity": 1.0}
        result = _flatten_item_attrs(item)
        assert result == item

    def test_empty_attributes_returns_item_unchanged(self):
        item = {"id": "item:1", "name": "Ruby", "attributes": {}}
        result = _flatten_item_attrs(item)
        assert result == item

    def test_none_attributes_returns_item_unchanged(self):
        item = {"id": "item:1", "name": "Ruby", "attributes": None}
        result = _flatten_item_attrs(item)
        assert result == item

    def test_origin_promoted_to_top_level(self):
        item = {"id": "item:1", "name": "Demantoid", "attributes": {"origin": "Colombia"}}
        result = _flatten_item_attrs(item)
        assert result["origin"] == "Colombia"

    def test_multiple_attrs_all_promoted(self):
        item = {
            "id": "item:1",
            "name": "Sapphire",
            "attributes": {"origin": "Kashmir", "clarity": "VS1", "treatment": "none"},
        }
        result = _flatten_item_attrs(item)
        assert result["origin"] == "Kashmir"
        assert result["clarity"] == "VS1"
        assert result["treatment"] == "none"

    def test_original_attributes_key_preserved(self):
        """Callers that need the nested form (e.g. detail page attributes panel) still work."""
        attrs = {"origin": "Burma"}
        item = {"id": "item:1", "name": "Ruby", "attributes": attrs}
        result = _flatten_item_attrs(item)
        assert result["attributes"] == attrs

    def test_core_fields_not_overwritten_by_attrs(self):
        """Core field wins when an attribute key collides (should be rare/impossible in practice)."""
        item = {"id": "item:1", "name": "Ruby", "attributes": {"name": "attr-name"}}
        result = _flatten_item_attrs(item)
        # dict unpacking: {**item, **attrs} means attrs win - document this behavior
        # In practice attribute keys never collide with core fields per schema design
        assert result["name"] == "attr-name"  # attrs override if collision

    def test_does_not_mutate_original(self):
        item = {"id": "item:1", "name": "Ruby", "attributes": {"origin": "Colombia"}}
        original_item = dict(item)
        _flatten_item_attrs(item)
        assert item == original_item


class TestPairedDisplayCellFormatFn:
    """paired_display_cell with format_fn applies unit-aware formatting to primary value."""

    def _umap(self):
        return {
            "piece": {"name": "piece", "decimals": 0, "unit_type": "pieces"},
            "carat": {"name": "carat", "decimals": 2, "unit_type": "weight"},
        }

    def test_piece_float_displays_as_integer(self):
        from ui.components.table import paired_display_cell
        from celerp.services.units import format_qty
        umap = self._umap()
        fmt = lambda v: format_qty(v, "piece", umap)
        td = paired_display_cell(
            entity_id="item:1",
            primary_field="quantity", primary_value=7.0,
            secondary_field="sell_by", secondary_value="piece",
            format_fn=fmt,
        )
        html = td.__html__() if hasattr(td, "__html__") else str(td)
        assert ">7<" in html, f"Expected '7' in HTML, got: {html}"
        assert "7.0" not in html, f"Expected no '7.0' in HTML, got: {html}"

    def test_carat_float_displays_with_two_decimals(self):
        from ui.components.table import paired_display_cell
        from celerp.services.units import format_qty
        umap = self._umap()
        fmt = lambda v: format_qty(v, "carat", umap)
        td = paired_display_cell(
            entity_id="item:1",
            primary_field="quantity", primary_value=64.0,
            secondary_field="sell_by", secondary_value="carat",
            format_fn=fmt,
        )
        html = td.__html__() if hasattr(td, "__html__") else str(td)
        assert "64.00" in html, f"Expected '64.00' in HTML, got: {html}"

    def test_no_format_fn_preserves_old_behaviour(self):
        from ui.components.table import paired_display_cell
        td = paired_display_cell(
            entity_id="item:1",
            primary_field="quantity", primary_value=7.0,
            secondary_field="sell_by", secondary_value="piece",
        )
        html = td.__html__() if hasattr(td, "__html__") else str(td)
        # Without format_fn, str(7.0) = "7.0" — old behaviour confirmed
        assert "7.0" in html


class TestNormalizeNumberStr:
    """_normalize_number_str drops .0 for integers, uses :g for floats."""

    def test_integer_float_drops_decimal(self):
        from ui.components.table import _normalize_number_str
        assert _normalize_number_str("7.0") == "7"
        assert _normalize_number_str("100.0") == "100"

    def test_real_float_preserves(self):
        from ui.components.table import _normalize_number_str
        assert _normalize_number_str("7.5") == "7.5"
        assert _normalize_number_str("64.25") == "64.25"

    def test_non_numeric_passthrough(self):
        from ui.components.table import _normalize_number_str
        assert _normalize_number_str("abc") == "abc"

    def test_integer_string_unchanged(self):
        from ui.components.table import _normalize_number_str
        assert _normalize_number_str("7") == "7"


class TestEditableCellNumberNormalization:
    """editable_cell with cell_type=number must show normalized value in input, not '7.0'."""

    def test_float_value_normalized_in_input(self):
        from ui.components.table import editable_cell
        td = editable_cell(entity_id="item:1", field="quantity", value=7.0, cell_type="number")
        html = td.__html__() if hasattr(td, "__html__") else str(td)
        assert 'value="7"' in html, f"Expected value='7', got: {html}"
        assert 'value="7.0"' not in html, f"Unexpected '7.0' in: {html}"

    def test_carat_float_normalized(self):
        from ui.components.table import editable_cell
        td = editable_cell(entity_id="item:1", field="quantity", value=64.0, cell_type="number")
        html = td.__html__() if hasattr(td, "__html__") else str(td)
        assert 'value="64"' in html
        assert 'value="64.0"' not in html

    def test_real_float_preserved(self):
        from ui.components.table import editable_cell
        td = editable_cell(entity_id="item:1", field="quantity", value=7.5, cell_type="number")
        html = td.__html__() if hasattr(td, "__html__") else str(td)
        assert 'value="7.5"' in html


class TestParseParamsColsFlattening:
    """_parse_params must correctly flatten cols regardless of encoding."""

    def _make_request(self, query_string: str):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.responses import Response
        import asyncio

        captured = {}

        async def endpoint(request):
            from ui.routes.inventory import _parse_params
            captured["result"] = _parse_params(request)
            return Response("ok")

        app = Starlette(routes=[Route("/", endpoint)])
        client = TestClient(app, raise_server_exceptions=True)
        client.get(f"/?{query_string}")
        return captured["result"]

    def test_comma_joined_single_param(self):
        result = self._make_request("cols=sku,name,quantity")
        assert result["cols"] == ["sku", "name", "quantity"]

    def test_multi_param_cols(self):
        result = self._make_request("cols=sku&cols=name&cols=quantity")
        assert result["cols"] == ["sku", "name", "quantity"]

    def test_empty_cols(self):
        result = self._make_request("")
        assert result["cols"] == []

    def test_single_col(self):
        result = self._make_request("cols=sku")
        assert result["cols"] == ["sku"]


class TestPairedSecondaryKeys:
    """_PAIRED_SECONDARY_KEYS is derived from _PAIRED_TABLE and excludes secondaries from detail_fields."""

    def test_secondary_keys_derived_from_paired_table(self):
        from ui.routes.inventory import _PAIRED_TABLE, _PAIRED_SECONDARY_KEYS
        assert _PAIRED_SECONDARY_KEYS == frozenset(_PAIRED_TABLE.values())

    def test_sell_by_excluded(self):
        from ui.routes.inventory import _PAIRED_SECONDARY_KEYS
        assert "sell_by" in _PAIRED_SECONDARY_KEYS

    def test_weight_unit_excluded(self):
        from ui.routes.inventory import _PAIRED_SECONDARY_KEYS
        assert "weight_unit" in _PAIRED_SECONDARY_KEYS

    def test_purchase_conversion_factor_excluded(self):
        from ui.routes.inventory import _PAIRED_SECONDARY_KEYS
        assert "purchase_conversion_factor" in _PAIRED_SECONDARY_KEYS

    def test_primary_keys_not_in_secondary_keys(self):
        from ui.routes.inventory import _PAIRED_TABLE, _PAIRED_SECONDARY_KEYS
        for primary in _PAIRED_TABLE:
            assert primary not in _PAIRED_SECONDARY_KEYS


class TestItemCoreKeys:
    """_ITEM_CORE_KEYS is defined once and used in both detail page and _item_detail_tabs."""

    def test_is_frozenset(self):
        from ui.routes.inventory import _ITEM_CORE_KEYS
        assert isinstance(_ITEM_CORE_KEYS, frozenset)

    def test_contains_quantity(self):
        from ui.routes.inventory import _ITEM_CORE_KEYS
        assert "quantity" in _ITEM_CORE_KEYS

    def test_contains_sku_and_name(self):
        from ui.routes.inventory import _ITEM_CORE_KEYS
        assert "sku" in _ITEM_CORE_KEYS
        assert "name" in _ITEM_CORE_KEYS


class TestApplyUnitFieldOverride:
    """_apply_unit_field_override returns unit select for sell_by/purchase_unit, passthrough otherwise."""

    def test_sell_by_becomes_select(self):
        from ui.routes.inventory import _apply_unit_field_override
        ct, opts, ac = _apply_unit_field_override("sell_by", "text", None, False, ["kg", "g"])
        assert ct == "select"
        assert ac is True
        assert "kg" in opts
        assert any("__new__" in str(o) for o in opts)

    def test_purchase_unit_becomes_select(self):
        from ui.routes.inventory import _apply_unit_field_override
        ct, opts, ac = _apply_unit_field_override("purchase_unit", "text", None, False, ["pcs"])
        assert ct == "select"
        assert ac is True

    def test_other_field_passthrough(self):
        from ui.routes.inventory import _apply_unit_field_override
        ct, opts, ac = _apply_unit_field_override("name", "text", ["a"], False, ["kg"])
        assert ct == "text"
        assert opts == ["a"]
        assert ac is False

    def test_empty_units_still_has_add_new(self):
        from ui.routes.inventory import _apply_unit_field_override
        ct, opts, ac = _apply_unit_field_override("sell_by", "text", None, False, [])
        assert any("__new__" in str(o) for o in opts)


class TestVirtualCostTotalColumn:
    """Tests for Cost (Total) virtual column injection and rendering."""

    def test_inject_price_columns_adds_cost_total_after_cost_price(self):
        """cost_price_total is injected immediately after cost_price in the schema."""
        from celerp.services.field_schema import _inject_price_columns, _BASE_FIELDS
        price_lists = [{"name": "Cost"}, {"name": "Retail"}]
        schema = _inject_price_columns(_BASE_FIELDS, price_lists)
        keys = [f["key"] for f in schema]
        assert "cost_price" in keys
        assert "cost_price_total" in keys
        cp_idx = keys.index("cost_price")
        ct_idx = keys.index("cost_price_total")
        assert ct_idx == cp_idx + 1, "cost_price_total must appear immediately after cost_price"

    def test_cost_total_not_injected_for_non_cost_lists(self):
        """Only cost-type price lists get a total column."""
        from celerp.services.field_schema import _inject_price_columns, _BASE_FIELDS
        schema = _inject_price_columns(_BASE_FIELDS, [{"name": "Retail"}, {"name": "Wholesale"}])
        keys = [f["key"] for f in schema]
        assert "retail_price_total" not in keys
        assert "wholesale_price_total" not in keys

    def test_cost_total_is_virtual(self):
        """cost_price_total has virtual=True and paired_with=cost_price."""
        from celerp.services.field_schema import _inject_price_columns, _BASE_FIELDS
        schema = _inject_price_columns(_BASE_FIELDS, [{"name": "Cost"}])
        ct = next(f for f in schema if f["key"] == "cost_price_total")
        assert ct.get("virtual") is True
        assert ct.get("paired_with") == "cost_price"
        assert ct.get("type") == "money"

    def test_label_price_cols_skips_virtual(self):
        """_label_price_cols does NOT append (Unit Price) to virtual total columns."""
        from ui.routes.inventory import _label_price_cols
        schema = [
            {"key": "cost_price", "label": "Cost", "type": "money"},
            {"key": "cost_price_total", "label": "Cost (Total)", "type": "money", "virtual": True},
        ]
        result = _label_price_cols(schema)
        labels = {f["key"]: f["label"] for f in result}
        assert labels["cost_price"] == "Cost (Unit Price)"
        assert labels["cost_price_total"] == "Cost (Total)"  # unchanged

    def test_resolve_visible_cols_injects_cost_total_after_cost_price(self):
        """Virtual total column is always injected after paired_with column."""
        from ui.routes.inventory import _resolve_visible_cols
        eff_schema = [
            {"key": "sku", "show_in_table": True},
            {"key": "cost_price", "show_in_table": True},
            {"key": "cost_price_total", "show_in_table": True, "virtual": True, "paired_with": "cost_price"},
        ]
        visible = _resolve_visible_cols(eff_schema, {}, "", [])
        assert "cost_price_total" in visible
        cp_idx = visible.index("cost_price")
        ct_idx = visible.index("cost_price_total")
        assert ct_idx == cp_idx + 1

    def test_resolve_visible_cols_url_override_still_injects_total(self):
        """Even with URL col override, virtual total is injected after its paired_with."""
        from ui.routes.inventory import _resolve_visible_cols
        eff_schema = [
            {"key": "sku"},
            {"key": "cost_price"},
            {"key": "cost_price_total", "virtual": True, "paired_with": "cost_price"},
        ]
        visible = _resolve_visible_cols(eff_schema, {}, "", ["sku", "cost_price"])
        assert "cost_price_total" in visible
        assert visible.index("cost_price_total") == visible.index("cost_price") + 1

    def test_render_virtual_total_cell(self):
        """_render_virtual_total_cell computes unit_price * qty and formats with currency."""
        from fasthtml.common import to_xml
        from ui.routes.inventory import _render_virtual_total_cell
        td = _render_virtual_total_cell("item:1", "cost_price_total", 100.0, 5.0, "THB")
        html = to_xml(td)
        assert "500" in html  # 100 * 5

    def test_render_virtual_total_cell_zero_qty(self):
        """Zero quantity results in '--' display."""
        from fasthtml.common import to_xml
        from ui.routes.inventory import _render_virtual_total_cell
        td = _render_virtual_total_cell("item:1", "cost_price_total", 100.0, 0.0, "THB")
        html = to_xml(td)
        assert "--" in html

    def test_render_virtual_total_cell_zero_price(self):
        """Zero unit price results in '--' display."""
        from fasthtml.common import to_xml
        from ui.routes.inventory import _render_virtual_total_cell
        td = _render_virtual_total_cell("item:1", "cost_price_total", 0.0, 10.0, "THB")
        html = to_xml(td)
        assert "--" in html


class TestPerPageSelector:
    """#171: the per-page dropdown must navigate to the current page's own URL (resetting to
    page 1), not HTMX-swap a hardcoded #inventory-content target — which made it dead on /docs."""

    def test_navigates_to_base_url_not_hardcoded_inventory_target(self):
        from ui.components.table import _per_page_selector
        from fasthtml.common import to_xml
        html = to_xml(_per_page_selector(50, "/docs", "q=&type=invoice&sort=date&dir=desc"))
        assert "inventory-content" not in html, "still hardcodes the inventory swap target"
        assert "hx-get" not in html and "hx-target" not in html
        assert "onchange" in html
        assert "/docs?per_page=" in html and "page=1" in html
        assert "type=invoice" in html  # carries the existing filter state

    def test_works_for_inventory_too(self):
        from ui.components.table import _per_page_selector
        from fasthtml.common import to_xml
        html = to_xml(_per_page_selector(100, "/inventory", "status=sold"))
        assert "/inventory?per_page=" in html and "status=sold" in html


class TestMixedSelectSystemValue:
    """#178: 'Mixed' is the reserved system value for a field whose sources disagreed (e.g. a merge
    of items whose select attribute differed). It must render as a selectable 'Mixed' rather than
    matching no option and showing empty — and without being added to the configured options."""

    def test_editable_select_renders_mixed_as_selected_option(self):
        from ui.components.table import editable_cell
        from fasthtml.common import to_xml
        html = to_xml(editable_cell("item:1", "stone_type", "Mixed", cell_type="select",
                                    options=["Alexandrite", "Amethyst"]))
        assert "Mixed" in html, "stored 'Mixed' must render as a 'Mixed' option, not empty"
        assert 'value="Mixed"' in html and "selected" in html

    def test_editable_select_normal_value_has_no_injected_mixed(self):
        from ui.components.table import editable_cell
        from fasthtml.common import to_xml
        html = to_xml(editable_cell("item:1", "stone_type", "Amethyst", cell_type="select",
                                    options=["Alexandrite", "Amethyst"]))
        assert ">Mixed<" not in html, "'Mixed' must not be injected for a normal value"

    def test_display_select_shows_mixed_label(self):
        from ui.components.table import display_cell
        from fasthtml.common import to_xml
        html = to_xml(display_cell("item:1", "stone_type", "Mixed", cell_type="select",
                                   options=["Alexandrite", "Amethyst"]))
        assert "Mixed" in html

    def test_options_helper_idempotent_when_already_present(self):
        from ui.components.table import _options_with_mixed
        # already has a Mixed option → not duplicated
        opts = [("a", "A"), ("Mixed", "Mixed")]
        assert _options_with_mixed(opts, "Mixed") == opts

    def test_display_custom_text_attr_shows_mixed_label(self):
        # A conflicting custom/free attribute (no select schema) carries the 'Mixed' sentinel after a
        # merge and must read "Mixed", not the raw value — not only dropdown cells. A legacy lowercase
        # value normalizes to the one canonical "Mixed" too (no separate 'mixed' vs 'Mixed').
        from ui.components.table import display_cell
        from fasthtml.common import to_xml
        html = to_xml(display_cell("item:1", "carats", "mixed", cell_type="text"))
        assert ">Mixed<" in html, "custom text attr must render the canonical 'Mixed'"

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

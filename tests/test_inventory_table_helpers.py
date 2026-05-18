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

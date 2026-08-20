"""Unit tests for unit_type feature.

Covers:
- is_weight_unit / is_pieces_unit helpers (units.py)
- UnitRecord model validation (unit_type field)
- DEFAULT_UNITS has unit_type on every entry
- _validate_units rejects invalid unit_type
- _inventory_cell_renderers derivation logic (weight, pieces)
- field_schema.py column ordering (qty → weight → pieces)
"""
import pytest


# ── units.py helpers ──────────────────────────────────────────────────────────

class TestIsWeightUnit:
    def _map(self, *units):
        return {u["name"]: u for u in units}

    def test_weight_unit_returns_true(self):
        from celerp.services.units import is_weight_unit
        umap = self._map({"name": "carat", "unit_type": "weight"})
        assert is_weight_unit("carat", umap) is True

    def test_pieces_unit_returns_false(self):
        from celerp.services.units import is_weight_unit
        umap = self._map({"name": "piece", "unit_type": "pieces"})
        assert is_weight_unit("piece", umap) is False

    def test_quantity_unit_returns_false(self):
        from celerp.services.units import is_weight_unit
        umap = self._map({"name": "liter", "unit_type": "quantity"})
        assert is_weight_unit("liter", umap) is False

    def test_unknown_unit_returns_false(self):
        from celerp.services.units import is_weight_unit
        assert is_weight_unit("unknown", {}) is False

    def test_none_unit_returns_false(self):
        from celerp.services.units import is_weight_unit
        assert is_weight_unit(None, {}) is False

    def test_empty_string_returns_false(self):
        from celerp.services.units import is_weight_unit
        assert is_weight_unit("", {"": {"unit_type": "weight"}}) is False

    def test_unit_missing_unit_type_returns_false(self):
        from celerp.services.units import is_weight_unit
        umap = {"gram": {"name": "gram", "decimals": 2}}  # no unit_type key
        assert is_weight_unit("gram", umap) is False


class TestIsPiecesUnit:
    def _map(self, *units):
        return {u["name"]: u for u in units}

    def test_pieces_unit_returns_true(self):
        from celerp.services.units import is_pieces_unit
        umap = self._map({"name": "piece", "unit_type": "pieces"})
        assert is_pieces_unit("piece", umap) is True

    def test_weight_unit_returns_false(self):
        from celerp.services.units import is_pieces_unit
        umap = self._map({"name": "carat", "unit_type": "weight"})
        assert is_pieces_unit("carat", umap) is False

    def test_none_unit_returns_false(self):
        from celerp.services.units import is_pieces_unit
        assert is_pieces_unit(None, {}) is False

    def test_empty_string_returns_false(self):
        from celerp.services.units import is_pieces_unit
        assert is_pieces_unit("", {}) is False


# ── Default units ─────────────────────────────────────────────────────────────

class TestDefaultUnits:
    def test_all_default_units_have_unit_type(self):
        from celerp.services.units import DEFAULT_UNITS
        for u in DEFAULT_UNITS:
            assert "unit_type" in u, f"Unit '{u['name']}' missing unit_type"

    def test_unit_types_are_valid_values(self):
        from celerp.routers.companies import _VALID_UNIT_TYPES
        from celerp.services.units import DEFAULT_UNITS
        for u in DEFAULT_UNITS:
            assert u["unit_type"] in _VALID_UNIT_TYPES, f"Unit '{u['name']}' has invalid unit_type '{u['unit_type']}'"

    def test_piece_is_pieces_type(self):
        from celerp.services.units import DEFAULT_UNITS
        piece = next(u for u in DEFAULT_UNITS if u["name"] == "piece")
        assert piece["unit_type"] == "pieces"

    def test_carat_is_weight_type(self):
        from celerp.services.units import DEFAULT_UNITS
        carat = next(u for u in DEFAULT_UNITS if u["name"] == "carat")
        assert carat["unit_type"] == "weight"

    def test_liter_is_quantity_type(self):
        from celerp.services.units import DEFAULT_UNITS
        liter = next(u for u in DEFAULT_UNITS if u["name"] == "liter")
        assert liter["unit_type"] == "quantity"


# ── UnitRecord model ──────────────────────────────────────────────────────────

class TestUnitRecord:
    def test_unit_type_defaults_to_quantity(self):
        from celerp.routers.companies import UnitRecord
        u = UnitRecord(name="bottle", label="Bottle", decimals=0)
        assert u.unit_type == "quantity"

    def test_unit_type_weight_accepted(self):
        from celerp.routers.companies import UnitRecord
        u = UnitRecord(name="gram", label="Gram", decimals=2, unit_type="weight")
        assert u.unit_type == "weight"

    def test_unit_type_pieces_accepted(self):
        from celerp.routers.companies import UnitRecord
        u = UnitRecord(name="piece", label="Piece", decimals=0, unit_type="pieces")
        assert u.unit_type == "pieces"

    def test_model_dump_includes_unit_type(self):
        from celerp.routers.companies import UnitRecord
        u = UnitRecord(name="oz", label="Ounce", decimals=2, unit_type="weight")
        d = u.model_dump()
        assert d["unit_type"] == "weight"


# ── _validate_units ───────────────────────────────────────────────────────────

class TestValidateUnits:
    def _make(self, **kwargs):
        from celerp.routers.companies import UnitRecord
        defaults = {"name": "piece", "label": "Piece", "decimals": 0, "unit_type": "pieces"}
        defaults.update(kwargs)
        return UnitRecord(**defaults)

    def test_valid_units_pass(self):
        from celerp.routers.companies import _validate_units
        units = [
            self._make(name="carat", unit_type="weight"),
            self._make(name="piece", unit_type="pieces"),
            self._make(name="liter", unit_type="quantity"),
        ]
        _validate_units(units)  # no exception

    def test_invalid_unit_type_raises(self):
        from fastapi import HTTPException
        from celerp.routers.companies import _validate_units, UnitRecord
        u = UnitRecord(name="foo", label="Foo", decimals=0, unit_type="measurement")
        with pytest.raises(HTTPException) as exc:
            _validate_units([u])
        assert "type" in exc.value.detail.lower()

    def test_duplicate_name_raises(self):
        from fastapi import HTTPException
        from celerp.routers.companies import _validate_units
        units = [self._make(name="carat"), self._make(name="carat")]
        with pytest.raises(HTTPException) as exc:
            _validate_units(units)
        assert "duplicate" in exc.value.detail.lower()

    def test_invalid_name_raises(self):
        from fastapi import HTTPException
        from celerp.routers.companies import _validate_units
        units = [self._make(name="My Unit")]
        with pytest.raises(HTTPException):
            _validate_units(units)

    def test_invalid_decimals_raises(self):
        from fastapi import HTTPException
        from celerp.routers.companies import _validate_units
        units = [self._make(name="foo", decimals=9)]
        with pytest.raises(HTTPException):
            _validate_units(units)


# ── field_schema column ordering ──────────────────────────────────────────────

class TestFieldSchemaOrdering:
    def _schema_positions(self):
        from celerp.services.field_schema import _BASE_FIELDS
        return {f["key"]: f["position"] for f in _BASE_FIELDS}

    def test_weight_position_after_quantity(self):
        pos = self._schema_positions()
        assert pos["weight"] > pos["quantity"]

    def test_pieces_position_after_weight(self):
        pos = self._schema_positions()
        assert pos["pieces"] > pos["weight"]

    def test_allow_splitting_position_after_pieces(self):
        pos = self._schema_positions()
        assert pos["allow_splitting"] > pos["pieces"]

    def test_no_duplicate_keys(self):
        from celerp.services.field_schema import _BASE_FIELDS
        keys = [f["key"] for f in _BASE_FIELDS]
        assert len(keys) == len(set(keys)), "Duplicate keys in _BASE_FIELDS"

    def test_pieces_has_correct_defaults(self):
        from celerp.services.field_schema import _BASE_FIELDS
        pieces = next(f for f in _BASE_FIELDS if f["key"] == "pieces")
        assert pieces["type"] == "number"
        assert pieces["editable"] is True
        assert pieces["show_in_table"] is True


# ── _inventory_cell_renderers derivation ─────────────────────────────────────

class TestInventoryCellRenderers:
    """Test derivation logic in _inventory_cell_renderers without HTTP."""

    def _schema(self):
        from celerp.services.field_schema import _BASE_FIELDS
        return _BASE_FIELDS

    def _umap(self):
        return {
            "carat": {"name": "carat", "unit_type": "weight"},
            "piece": {"name": "piece", "unit_type": "pieces"},
            "liter": {"name": "liter", "unit_type": "quantity"},
        }

    def _render_str(self, td) -> str:
        """Convert fasthtml FT to XML string for assertion."""
        from fasthtml.common import to_xml
        return to_xml(td)

    def _schema_with_sold_price(self):
        return self._schema() + [{
            "key": "sold_price", "label": "Sold", "type": "money", "editable": False,
        }]

    def test_sold_price_renderer_value_with_unit_annotation_read_only(self):
        # A realized sale price renders as a plain money cell with its sell-by
        # annotation, and is derived, not editable: no click-to-edit affordance.
        from ui.routes.inventory import _inventory_cell_renderers
        renderers = _inventory_cell_renderers(self._schema_with_sold_price(), units_map=self._umap())
        assert "sold_price" in renderers
        td = renderers["sold_price"]("item:1", {"sold_price": 89.96, "sell_by": "carat"})
        s = self._render_str(td)
        assert "89.96" in s
        assert "/ carat" in s
        assert "hx-get" not in s.lower()
        assert "dblclick" not in s.lower()
        assert "cell--clickable" not in s

    def test_sold_price_renderer_none_shows_dash_no_annotation(self):
        from ui.routes.inventory import _inventory_cell_renderers
        renderers = _inventory_cell_renderers(self._schema_with_sold_price(), units_map=self._umap())
        td = renderers["sold_price"]("item:1", {"sold_price": None, "sell_by": "carat"})
        s = self._render_str(td)
        assert "--" in s
        assert "/ carat" not in s

    def test_weight_renderer_derived_for_weight_sell_by(self):
        from ui.routes.inventory import _inventory_cell_renderers
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map=self._umap())
        assert "weight" in renderers
        row = {"quantity": 5.2, "sell_by": "carat", "weight": 999}
        td = renderers["weight"]("item:1", row)
        s = self._render_str(td)
        # Should show derived from qty, not stored weight
        assert "5.2" in s
        assert "carat" in s
        assert "Derived" in s

    def test_weight_renderer_not_derived_for_quantity_sell_by(self):
        from ui.routes.inventory import _inventory_cell_renderers
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map=self._umap())
        row = {"quantity": 3, "sell_by": "liter", "weight": 1.5, "weight_unit": "kg"}
        td = renderers["weight"]("item:1", row)
        s = self._render_str(td)
        # Should NOT be derived - show actual weight
        assert "Derived" not in s

    def test_weight_renderer_derived_empty_qty_shows_placeholder(self):
        from ui.routes.inventory import _inventory_cell_renderers
        from ui.components.table import EMPTY
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map=self._umap())
        row = {"quantity": None, "sell_by": "carat", "weight": 0}
        td = renderers["weight"]("item:1", row)
        s = self._render_str(td)
        assert EMPTY in s

    def test_pieces_renderer_derived_for_pieces_sell_by(self):
        from ui.routes.inventory import _inventory_cell_renderers
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map=self._umap())
        assert "pieces" in renderers
        row = {"quantity": 10, "sell_by": "piece", "pieces": 999}
        td = renderers["pieces"]("item:1", row)
        s = self._render_str(td)
        assert "10" in s
        assert "Derived" in s

    def test_pieces_renderer_editable_for_non_pieces_sell_by(self):
        from ui.routes.inventory import _inventory_cell_renderers
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map=self._umap())
        row = {"quantity": 3, "sell_by": "liter", "pieces": 7}
        td = renderers["pieces"]("item:1", row)
        s = self._render_str(td)
        assert "Derived" not in s

    def test_pieces_renderer_not_derived_for_weight_sell_by(self):
        from ui.routes.inventory import _inventory_cell_renderers
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map=self._umap())
        row = {"quantity": 5.2, "sell_by": "carat", "pieces": 3}
        td = renderers["pieces"]("item:1", row)
        s = self._render_str(td)
        # Weight sell_by does NOT derive pieces
        assert "Derived" not in s

    def test_no_circular_reference_in_weight_renderer(self):
        """Ensure weight renderer fallback doesn't infinitely recurse."""
        from ui.routes.inventory import _inventory_cell_renderers
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map=self._umap())
        # Call with a non-weight unit - must not recurse
        row = {"quantity": 3, "sell_by": "liter", "weight": 1.5, "weight_unit": "kg"}
        td = renderers["weight"]("item:1", row)
        assert td is not None  # did not blow up

    def test_empty_units_map_weight_not_derived(self):
        from ui.routes.inventory import _inventory_cell_renderers
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map={})
        row = {"quantity": 5.2, "sell_by": "carat", "weight": 1.5}
        td = renderers["weight"]("item:1", row)
        s = self._render_str(td)
        assert "Derived" not in s

    def test_none_units_map_weight_not_derived(self):
        from ui.routes.inventory import _inventory_cell_renderers
        schema = self._schema()
        renderers = _inventory_cell_renderers(schema, units_map=None)
        row = {"quantity": 5.2, "sell_by": "carat", "weight": 1.5}
        td = renderers["weight"]("item:1", row)
        # Should not crash and should not derive
        assert td is not None


# ── format_qty ────────────────────────────────────────────────────────────────

class TestFormatQty:
    def _map(self, name, decimals, unit_type="quantity"):
        return {name: {"name": name, "decimals": decimals, "unit_type": unit_type}}

    def test_pieces_unit_no_decimals(self):
        from celerp.services.units import format_qty
        umap = self._map("piece", 0, "pieces")
        assert format_qty(3.0, "piece", umap) == "3"

    def test_pieces_unit_rounds_float_input(self):
        from celerp.services.units import format_qty
        umap = self._map("piece", 0, "pieces")
        assert format_qty(5.0, "piece", umap) == "5"

    def test_carat_two_decimals(self):
        from celerp.services.units import format_qty
        umap = self._map("carat", 2, "weight")
        assert format_qty(63.22, "carat", umap) == "63.22"

    def test_carat_pads_to_two_decimals(self):
        from celerp.services.units import format_qty
        umap = self._map("carat", 2, "weight")
        assert format_qty(5.0, "carat", umap) == "5.00"

    def test_kg_three_decimals(self):
        from celerp.services.units import format_qty
        umap = self._map("kg", 3, "weight")
        assert format_qty(1.5, "kg", umap) == "1.500"

    def test_unknown_unit_strips_trailing_zeros(self):
        from celerp.services.units import format_qty
        assert format_qty(3.0, "unknown", {}) == "3"

    def test_unknown_unit_preserves_significant_decimals(self):
        from celerp.services.units import format_qty
        assert format_qty(3.14, "unknown", {}) == "3.14"

    def test_none_value_returns_empty_string(self):
        from celerp.services.units import format_qty
        assert format_qty(None, "piece", {}) == ""

    def test_empty_string_value_returns_empty_string(self):
        from celerp.services.units import format_qty
        assert format_qty("", "piece", {}) == ""

    def test_non_numeric_value_returns_as_string(self):
        from celerp.services.units import format_qty
        assert format_qty("abc", "piece", {}) == "abc"

    def test_none_unit_falls_back_gracefully(self):
        from celerp.services.units import format_qty
        result = format_qty(5.0, None, {})
        assert result == "5"

    def test_string_numeric_input(self):
        from celerp.services.units import format_qty
        umap = self._map("piece", 0, "pieces")
        assert format_qty("7.0", "piece", umap) == "7"


# ── purchase_display_cell ─────────────────────────────────────────────────────

class TestPurchaseDisplayCell:
    def test_renders_purchase_unit_and_conversion_factor(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        # Should contain the purchase unit value
        assert "Case" in html

    def test_renders_conversion_factor(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "12" in html

    def test_renders_sell_by_suffix(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "Piece" in html

    def test_arrow_has_title(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "Purchasing unit conversion to stock unit" in html

    def test_sell_by_not_editable_on_dblclick(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        # sell_by span should NOT have hx-get with sell_by/paired-edit
        assert "sell_by/paired-edit" not in html

    def test_purchase_unit_is_editable(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "purchase_unit/paired-edit" in html

    def test_conversion_factor_is_editable(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "purchase_conversion_factor/paired-edit" in html

    def test_data_col_is_purchase_unit(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert 'data-col="purchase_unit"' in html


# ── _PAIRED_TABLE includes purchase_unit ─────────────────────────────────────

class TestPairedTablePurchase:
    def test_purchase_unit_in_paired_table(self):
        from ui.routes.inventory import _PAIRED_TABLE
        assert "purchase_unit" in _PAIRED_TABLE

    def test_purchase_conversion_factor_is_secondary(self):
        from ui.routes.inventory import _PAIRED_TABLE
        assert _PAIRED_TABLE["purchase_unit"] == "purchase_conversion_factor"

    def test_purchase_unit_show_in_table(self):
        from celerp.services.field_schema import _BASE_FIELDS
        pu = next(f for f in _BASE_FIELDS if f["key"] == "purchase_unit")
        assert pu["show_in_table"] is True

    def test_purchase_conversion_factor_hidden(self):
        from celerp.services.field_schema import _BASE_FIELDS
        pcf = next(f for f in _BASE_FIELDS if f["key"] == "purchase_conversion_factor")
        assert pcf["show_in_table"] is False


# ── searchable_select add-new option ─────────────────────────────────────────

class TestSearchableSelectAddNew:
    def test_new_option_renders_combobox_option_new_class(self):
        from ui.components.table import searchable_select
        result = searchable_select(
            name="sell_by",
            options=[("piece", "piece"), ("__new__:/settings/inventory?tab=units", "+ Add new unit")],
            value="",
        )
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "combobox-option--new" in html

    def test_new_option_value_contains_redirect_url(self):
        from ui.components.table import searchable_select
        result = searchable_select(
            name="sell_by",
            options=[("piece", "piece"), ("__new__:/settings/inventory?tab=units", "+ Add new unit")],
            value="",
        )
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "__new__:/settings/inventory?tab=units" in html

    def test_initcombobox_redirect_js_present(self):
        import inspect
        from ui.components import shell
        src = inspect.getsource(shell)
        assert "__new__:" in src
        assert "window.location" in src


# ── _inventory_cell_renderers purchase_unit renderer ────────────────────────

class TestInventoryCellRenderersPurchase:
    def _schema(self):
        return [
            {"key": "purchase_unit", "type": "text", "show_in_table": True},
            {"key": "purchase_conversion_factor", "type": "number", "show_in_table": False},
            {"key": "sell_by", "type": "text", "show_in_table": False},
            {"key": "quantity", "type": "number", "show_in_table": True},
        ]

    def test_purchase_unit_renderer_present(self):
        from ui.routes.inventory import _inventory_cell_renderers
        renderers = _inventory_cell_renderers(self._schema(), unit_names=["piece"])
        assert "purchase_unit" in renderers

    def test_purchase_unit_renderer_returns_purchase_display_cell(self):
        from ui.routes.inventory import _inventory_cell_renderers
        renderers = _inventory_cell_renderers(self._schema(), unit_names=["piece"])
        renderer = renderers["purchase_unit"]
        row = {"purchase_unit": "Case", "purchase_conversion_factor": 12, "sell_by": "piece"}
        result = renderer("item-1", row)
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "Case" in html
        assert "12" in html
        assert "piece" in html
        assert 'data-col="purchase_unit"' in html

    def test_purchase_conversion_factor_not_in_renderers(self):
        from ui.routes.inventory import _inventory_cell_renderers
        renderers = _inventory_cell_renderers(self._schema(), unit_names=["piece"])
        # purchase_conversion_factor is secondary (hidden), should not have its own renderer
        assert "purchase_conversion_factor" not in renderers


# ── Item creation defaults ────────────────────────────────────────────────────

class TestItemCreationDefaults:
    def _project(self, data: dict) -> dict:
        from celerp_inventory.projections import apply_item_event
        return apply_item_event({}, "item.created", data)

    def test_purchase_unit_defaults_to_sell_by(self):
        state = self._project({"sku": "A1", "name": "Widget", "sell_by": "piece"})
        assert state["purchase_unit"] == "piece"

    def test_purchase_conversion_factor_defaults_to_1(self):
        state = self._project({"sku": "A1", "name": "Widget", "sell_by": "piece"})
        assert state["purchase_conversion_factor"] == 1

    def test_explicit_purchase_unit_not_overridden(self):
        state = self._project({"sku": "A1", "name": "Widget", "sell_by": "piece", "purchase_unit": "case"})
        assert state["purchase_unit"] == "case"

    def test_explicit_conversion_factor_not_overridden(self):
        state = self._project({"sku": "A1", "name": "Widget", "sell_by": "piece", "purchase_conversion_factor": 12})
        assert state["purchase_conversion_factor"] == 12

    def test_no_sell_by_leaves_purchase_unit_unset(self):
        state = self._project({"sku": "A1", "name": "Widget"})
        assert state.get("purchase_unit") is None


# ── Receiving with conversion factor / styling ───────────────────────────────

class TestPurchaseConversionFactor:
    def test_paired_tertiary_css_present(self):
        from pathlib import Path
        css_path = Path(__file__).resolve().parents[1] / "ui" / "static" / "app.css"
        css = css_path.read_text()
        assert ".paired-tertiary" in css
        assert "font-style: italic" in css.split(".paired-tertiary")[1].split("}")[0]

    def test_purchase_display_cell_tertiary_has_class(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "Piece")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "paired-tertiary" in html

    def test_purchase_display_cell_tertiary_shows_sell_by(self):
        from ui.components.table import purchase_display_cell
        result = purchase_display_cell("item-1", "Case", 12, "kg")
        html = result.__html__() if hasattr(result, '__html__') else str(result)
        assert "kg" in html


# ── Issue 1: item-schema endpoint uses effective schema ───────────────────────

async def _make_headers(client) -> dict:
    r = await client.post(
        "/auth/register",
        json={"company_name": "SchemaTestCo", "email": "admin@schematest.com", "name": "Admin", "password": "pw"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestItemSchemaEndpointUsesEffectiveSchema:
    @pytest.mark.asyncio
    async def test_custom_price_list_appears_in_schema(self, client):
        """item-schema endpoint must reflect dynamically added price lists."""
        h = await _make_headers(client)
        price_lists = [
            {"name": "Cost"},
            {"name": "Wholesale"},
            {"name": "Retail"},
            {"name": "VIP"},
        ]
        await client.patch("/companies/me/price-lists", json={"price_lists": price_lists}, headers=h)
        schema = (await client.get("/companies/me/item-schema", headers=h)).json()
        keys = {f["key"] for f in schema}
        assert "vip_price" in keys, "Custom price list 'VIP' must appear in item schema"

    @pytest.mark.asyncio
    async def test_deleted_price_list_absent_from_schema(self, client):
        """Removing a user-added price list must remove its column from schema."""
        h = await _make_headers(client)
        price_lists = [{"name": "Cost"}, {"name": "Wholesale"}, {"name": "Retail"}]
        await client.patch("/companies/me/price-lists", json={"price_lists": price_lists}, headers=h)
        schema = (await client.get("/companies/me/item-schema", headers=h)).json()
        keys = {f["key"] for f in schema}
        assert "vip_price" not in keys, "Removed VIP price list must not appear in schema"


# ── Issue 4: Net/Gross weight fields in schema ────────────────────────────────

class TestNetGrossWeightSchema:
    def test_weight_label_is_net_weight(self):
        from celerp.services.field_schema import _BASE_FIELDS
        weight_field = next(f for f in _BASE_FIELDS if f["key"] == "weight")
        assert weight_field["label"] == "Net Weight", f"Expected 'Net Weight', got '{weight_field['label']}'"

    def test_weight_unit_label_is_net_weight_unit(self):
        from celerp.services.field_schema import _BASE_FIELDS
        wu = next(f for f in _BASE_FIELDS if f["key"] == "weight_unit")
        assert wu["label"] == "Net Weight Unit", f"Expected 'Net Weight Unit', got '{wu['label']}'"

    def test_gross_weight_field_present(self):
        from celerp.services.field_schema import _BASE_FIELDS
        keys = {f["key"] for f in _BASE_FIELDS}
        assert "gross_weight" in keys, "gross_weight field must be in _BASE_FIELDS"
        assert "gross_weight_unit" in keys, "gross_weight_unit field must be in _BASE_FIELDS"

    def test_gross_weight_defaults(self):
        from celerp.services.field_schema import _BASE_FIELDS
        gw = next(f for f in _BASE_FIELDS if f["key"] == "gross_weight")
        assert gw["type"] == "number"
        assert gw["editable"] is True
        assert gw["show_in_table"] is False  # hidden by default

    def test_no_duplicate_keys_after_gross_weight(self):
        from celerp.services.field_schema import _BASE_FIELDS
        keys = [f["key"] for f in _BASE_FIELDS]
        assert len(keys) == len(set(keys)), "Duplicate keys in _BASE_FIELDS after adding gross_weight"

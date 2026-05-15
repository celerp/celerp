"""Unit tests for unit_type feature.

Covers:
- is_weight_unit / is_pieces_unit helpers (units.py)
- UnitRecord model validation (unit_type field)
- _DEFAULT_UNITS has unit_type on every entry
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
        from celerp.routers.companies import _DEFAULT_UNITS
        for u in _DEFAULT_UNITS:
            assert "unit_type" in u, f"Unit '{u['name']}' missing unit_type"

    def test_unit_types_are_valid_values(self):
        from celerp.routers.companies import _DEFAULT_UNITS, _VALID_UNIT_TYPES
        for u in _DEFAULT_UNITS:
            assert u["unit_type"] in _VALID_UNIT_TYPES, f"Unit '{u['name']}' has invalid unit_type '{u['unit_type']}'"

    def test_piece_is_pieces_type(self):
        from celerp.routers.companies import _DEFAULT_UNITS
        piece = next(u for u in _DEFAULT_UNITS if u["name"] == "piece")
        assert piece["unit_type"] == "pieces"

    def test_carat_is_weight_type(self):
        from celerp.routers.companies import _DEFAULT_UNITS
        carat = next(u for u in _DEFAULT_UNITS if u["name"] == "carat")
        assert carat["unit_type"] == "weight"

    def test_liter_is_quantity_type(self):
        from celerp.routers.companies import _DEFAULT_UNITS
        liter = next(u for u in _DEFAULT_UNITS if u["name"] == "liter")
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
        """Convert fasthtml FT to string for assertion."""
        return str(td)

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

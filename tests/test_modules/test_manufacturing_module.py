# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for the celerp-manufacturing module.

Covers:
- PLUGIN_MANIFEST structure and required fields
- Projection handler correctness (all event types, edge cases)
- projection_handler slot registration and engine dispatch
- Module route registration and HTTP integration
- Electron / default_modules structure
- Edge cases: unknown events, duplicate prefixes, bad handler path
"""
from __future__ import annotations

import uuid
import sys
import os
import pytest

# Ensure celerp_manufacturing is importable in this test context.
_MFG_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "default_modules", "celerp-manufacturing")
)
if _MFG_SRC not in sys.path:
    sys.path.insert(0, _MFG_SRC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client, suffix: str = "") -> str:
    addr = f"admin-{suffix or uuid.uuid4().hex[:8]}@mfg-mod.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Mfg Mod Co", "email": addr, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# TestManufacturingManifest — PLUGIN_MANIFEST structure
# ---------------------------------------------------------------------------

class TestManufacturingManifest:
    def test_manifest_via_path(self):
        """Load the __init__.py from the module root and check manifest."""
        import importlib.util
        init = os.path.join(_MFG_SRC, "__init__.py")
        spec = importlib.util.spec_from_file_location("_mfg_pkg", init)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        m = mod.PLUGIN_MANIFEST
        assert m["name"] == "celerp-manufacturing"
        assert m["version"]
        assert m["display_name"]
        assert m["api_routes"] == "celerp_manufacturing.routes"
        assert m["ui_routes"] == "celerp_manufacturing.ui_routes"

    def test_manifest_has_nav_slot(self):
        import importlib.util
        init = os.path.join(_MFG_SRC, "__init__.py")
        spec = importlib.util.spec_from_file_location("_mfg_pkg2", init)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        nav = mod.PLUGIN_MANIFEST["slots"]["nav"]
        assert nav["href"] == "/manufacturing"
        assert nav["label"] == "Manufacturing"
        assert nav.get("order") is not None

    def test_manifest_projection_handler_slots(self):
        import importlib.util
        init = os.path.join(_MFG_SRC, "__init__.py")
        spec = importlib.util.spec_from_file_location("_mfg_pkg3", init)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        handlers = mod.PLUGIN_MANIFEST["slots"]["projection_handler"]
        assert isinstance(handlers, list)
        prefixes = {h["prefix"] for h in handlers}
        assert "mfg." in prefixes
        assert "bom." in prefixes

    def test_manifest_handler_paths_are_importable(self):
        import importlib.util
        init = os.path.join(_MFG_SRC, "__init__.py")
        spec = importlib.util.spec_from_file_location("_mfg_pkg4", init)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for h in mod.PLUGIN_MANIFEST["slots"]["projection_handler"]:
            path, func = h["handler"].rsplit(":", 1)
            import importlib
            m = importlib.import_module(path)
            assert callable(getattr(m, func))

    def test_manifest_no_migrations_key(self):
        """Manufacturing uses core tables — no module-owned migrations."""
        import importlib.util
        init = os.path.join(_MFG_SRC, "__init__.py")
        spec = importlib.util.spec_from_file_location("_mfg_pkg5", init)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "migrations" not in mod.PLUGIN_MANIFEST

    def test_manifest_license_is_mit(self):
        import importlib.util
        init = os.path.join(_MFG_SRC, "__init__.py")
        spec = importlib.util.spec_from_file_location("_mfg_pkg6", init)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.PLUGIN_MANIFEST["license"] == "MIT"


# ---------------------------------------------------------------------------
# TestManufacturingProjectionHandler — pure unit tests
# ---------------------------------------------------------------------------

class TestManufacturingProjectionHandler:
    from celerp_manufacturing import projection_handler as _ph

    def test_order_created(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event({}, "mfg.order.created", {"description": "Test"})
        assert s["entity_type"] == "mfg_order"
        assert s["status"] == "created"
        assert s["is_in_production"] is False
        assert s["steps_completed"] == []
        assert s["actual_outputs"] == []

    def test_order_started(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event({"status": "created", "is_in_production": False}, "mfg.order.started", {})
        assert s["status"] == "started"
        assert s["is_in_production"] is True

    def test_step_completed_appends(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = {"steps_completed": ["step1"]}
        s = apply_manufacturing_event(s, "mfg.step.completed", {"step_id": "step2"})
        assert "step2" in s["steps_completed"]
        assert "step1" in s["steps_completed"]

    def test_step_completed_no_duplicate(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = {"steps_completed": ["step1"]}
        s = apply_manufacturing_event(s, "mfg.step.completed", {"step_id": "step1"})
        assert s["steps_completed"].count("step1") == 1

    def test_order_completed(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        outputs = [{"sku": "FG", "name": "Finished", "quantity": 2}]
        s = apply_manufacturing_event(
            {"status": "started", "is_in_production": True},
            "mfg.order.completed",
            {"actual_outputs": outputs, "labor_hours": 4.5},
        )
        assert s["status"] == "completed"
        assert s["is_in_production"] is False
        assert s["actual_outputs"] == outputs
        assert s["labor_hours"] == 4.5

    def test_order_completed_waste_recorded(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event(
            {},
            "mfg.order.completed",
            {"actual_outputs": [], "waste": {"quantity": 1.5, "unit": "kg", "reason": "defect"}},
        )
        assert s["waste"]["quantity"] == 1.5

    def test_order_completed_no_outputs_preserves_existing(self):
        """actual_outputs: None in data means don't overwrite; existing value preserved."""
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event(
            {"actual_outputs": [{"sku": "X"}]},
            "mfg.order.completed",
            {"actual_outputs": None},
        )
        assert s["actual_outputs"] == [{"sku": "X"}]

    def test_order_cancelled(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event(
            {"status": "started", "is_in_production": True},
            "mfg.order.cancelled",
            {"reason": "Too expensive"},
        )
        assert s["status"] == "cancelled"
        assert s["is_in_production"] is False
        assert s["cancel_reason"] == "Too expensive"

    def test_order_cancelled_no_reason(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event({"status": "started"}, "mfg.order.cancelled", {})
        assert s["status"] == "cancelled"
        assert "cancel_reason" not in s

    def test_bom_created(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event({}, "bom.created", {"name": "BOM A", "components": []})
        assert s["entity_type"] == "bom"
        assert s["name"] == "BOM A"
        assert s["components"] == []

    def test_bom_updated(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event({"name": "Old", "components": []}, "bom.updated", {"name": "New"})
        assert s["name"] == "New"

    def test_bom_deleted(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        s = apply_manufacturing_event({"name": "BOM A"}, "bom.deleted", {})
        assert s["deleted"] is True

    def test_unknown_event_raises(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        with pytest.raises(ValueError, match="Unsupported mfg event"):
            apply_manufacturing_event({}, "mfg.nonexistent", {})

    def test_state_is_copied_not_mutated(self):
        from celerp_manufacturing.projection_handler import apply_manufacturing_event
        original = {"status": "created", "steps_completed": []}
        apply_manufacturing_event(original, "mfg.order.started", {})
        assert original["status"] == "created"  # Original not mutated


# ---------------------------------------------------------------------------
# TestProjectionEngineSlotDispatch — engine picks up module handler
# ---------------------------------------------------------------------------

class TestProjectionEngineSlotDispatch:
    def setup_method(self):
        from celerp.modules.slots import all_slots
        # Snapshot current slot state so teardown can restore fully
        self._slot_snapshot = {k: list(v) for k, v in all_slots().items()}
        from celerp.modules.slots import clear
        clear()

    def teardown_method(self):
        from celerp.modules.slots import clear, register
        clear()
        # Restore full slot snapshot so subsequent tests see original state
        for slot_name, contributions in self._slot_snapshot.items():
            for contrib in contributions:
                register(slot_name, contrib)

    def test_engine_dispatches_mfg_event_via_slot(self):
        from celerp.modules.slots import register
        register("projection_handler", {
            "prefix": "mfg.",
            "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            "_module": "celerp-manufacturing",
        })
        from celerp.projections.engine import ProjectionEngine
        result = ProjectionEngine._apply({}, "mfg.order.created", {"description": "x"})
        assert result["entity_type"] == "mfg_order"

    def test_engine_dispatches_bom_event_via_slot(self):
        from celerp.modules.slots import register
        register("projection_handler", {
            "prefix": "bom.",
            "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            "_module": "celerp-manufacturing",
        })
        from celerp.projections.engine import ProjectionEngine
        result = ProjectionEngine._apply({}, "bom.created", {"name": "BOM X"})
        assert result["entity_type"] == "bom"

    def test_engine_dispatches_item_event_via_slot(self):
        """item.* events dispatch via the inventory projection_handler slot."""
        from celerp.modules.slots import register
        register("projection_handler", {
            "prefix": "item.",
            "handler": "celerp_inventory.projections:apply_item_event",
            "_module": "celerp-inventory",
        })
        from celerp.projections.engine import ProjectionEngine
        result = ProjectionEngine._apply(
            {"sku": "X", "name": "Y", "quantity": 10, "status": "available"},
            "item.consumed",
            {"quantity_consumed": 3},
        )
        assert result["quantity"] == 7

    def test_engine_mfg_event_falls_through_to_passthrough_when_no_slot(self):
        """Without mfg slot, mfg.* has no built-in handler — falls through to passthrough."""
        from celerp.projections.engine import ProjectionEngine
        result = ProjectionEngine._apply({"existing": "data"}, "mfg.order.created", {"description": "x"})
        # Passthrough merge: no entity_type set (no handler ran the proper logic)
        assert result["description"] == "x"
        assert "entity_type" not in result  # Confirms handler was not invoked

    def test_engine_module_handler_takes_precedence_over_builtin(self):
        """Module handler for a prefix beats any built-in with same prefix."""
        called = []

        def _custom_handler(state, event_type, data):
            called.append(event_type)
            return {**state, "handled_by": "custom"}

        from celerp.modules.slots import register, get
        import celerp_manufacturing.projection_handler as _ph_mod
        # Patch: register a custom handler for item. prefix
        _orig = _ph_mod.apply_manufacturing_event
        try:
            register("projection_handler", {
                "prefix": "item.",
                "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
                "_module": "test",
            })
            # Will call apply_manufacturing_event with item event → raises ValueError
            from celerp.projections.engine import ProjectionEngine
            with pytest.raises(ValueError):
                ProjectionEngine._apply({}, "item.nonexistent_for_test", {})
            # If we got ValueError it means the module handler ran (not the built-in)
        finally:
            pass  # teardown_method clears slots

    def test_engine_bad_handler_path_logs_and_skips(self):
        """A malformed handler path is logged and skipped; falls through to built-in."""
        from celerp.modules.slots import register
        register("projection_handler", {
            "prefix": "mfg.",
            "handler": "nonexistent.module:no_such_func",
            "_module": "bad-module",
        })
        from celerp.projections.engine import ProjectionEngine
        # Should not raise — bad handler is skipped, falls through to passthrough
        result = ProjectionEngine._apply({"x": 1}, "mfg.order.created", {"description": "y"})
        assert result["description"] == "y"

    def test_engine_missing_prefix_key_is_skipped(self):
        from celerp.modules.slots import register
        register("projection_handler", {"handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event", "_module": "x"})
        from celerp.projections.engine import ProjectionEngine
        # Should not raise
        result = ProjectionEngine._apply({}, "item.consumed", {"quantity_consumed": 1})
        assert isinstance(result, dict)

    def test_engine_missing_handler_key_is_skipped(self):
        from celerp.modules.slots import register
        register("projection_handler", {"prefix": "mfg.", "_module": "x"})
        from celerp.projections.engine import ProjectionEngine
        result = ProjectionEngine._apply({}, "mfg.order.created", {"description": "z"})
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# TestManufacturingModuleHTTP — HTTP integration via test client
# ---------------------------------------------------------------------------

class TestManufacturingModuleHTTP:
    @pytest.mark.asyncio
    async def test_list_orders_empty(self, client):
        token = await _register(client, "list")
        r = await client.get("/manufacturing", headers=_h(token))
        assert r.status_code == 200
        assert r.json()["items"] == []

    @pytest.mark.asyncio
    async def test_create_order_requires_inputs(self, client):
        token = await _register(client, "noinp")
        r = await client.post(
            "/manufacturing",
            headers=_h(token),
            json={"description": "No inputs", "inputs": []},
        )
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_create_and_get_order(self, client):
        token = await _register(client, "cago")
        # Create an item to consume
        item_r = await client.post("/items", headers=_h(token), json={"sku": "R1", "name": "Raw", "quantity": 5, "sell_by": "piece"})
        item_id = item_r.json()["id"]

        r = await client.post(
            "/manufacturing",
            headers=_h(token),
            json={
                "description": "Make FG",
                "inputs": [{"item_id": item_id, "quantity": 2}],
                "expected_outputs": [{"sku": "FG1", "name": "Finished 1", "quantity": 1}],
            },
        )
        assert r.status_code == 200
        order_id = r.json()["id"]

        get_r = await client.get(f"/manufacturing/{order_id}", headers=_h(token))
        assert get_r.status_code == 200
        assert get_r.json()["description"] == "Make FG"
        assert get_r.json()["status"] == "created"

    @pytest.mark.asyncio
    async def test_bom_crud(self, client):
        token = await _register(client, "bom")
        # Create
        r = await client.post(
            "/manufacturing/boms",
            headers=_h(token),
            json={"name": "BOM-1", "components": [{"sku": "C1", "qty": 2, "sell_by": "piece"}]},
        )
        assert r.status_code == 200
        bom_id = r.json()["bom_id"]

        # Get
        g = await client.get(f"/manufacturing/boms/{bom_id}", headers=_h(token))
        assert g.status_code == 200
        assert g.json()["name"] == "BOM-1"

        # Update
        u = await client.put(
            f"/manufacturing/boms/{bom_id}",
            headers=_h(token),
            json={"name": "BOM-1-updated"},
        )
        assert u.status_code == 200

        # Verify update
        g2 = await client.get(f"/manufacturing/boms/{bom_id}", headers=_h(token))
        assert g2.json()["name"] == "BOM-1-updated"

        # Delete
        d = await client.delete(f"/manufacturing/boms/{bom_id}", headers=_h(token))
        assert d.status_code == 200

        # 404 after delete
        g3 = await client.get(f"/manufacturing/boms/{bom_id}", headers=_h(token))
        assert g3.status_code == 404

    @pytest.mark.asyncio
    async def test_bom_empty_name_rejected(self, client):
        token = await _register(client, "bomname")
        r = await client.post(
            "/manufacturing/boms",
            headers=_h(token),
            json={"name": "  "},
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_full_order_lifecycle(self, client):
        token = await _register(client, "lifecycle")
        item_r = await client.post("/items", headers=_h(token), json={"sku": "RAW-L", "name": "Raw L", "quantity": 10, "sell_by": "piece"})
        item_id = item_r.json()["id"]

        # Create
        order_r = await client.post(
            "/manufacturing",
            headers=_h(token),
            json={
                "description": "Full lifecycle",
                "inputs": [{"item_id": item_id, "quantity": 3}],
                "expected_outputs": [{"sku": "OUT-L", "name": "Output L", "quantity": 1}],
            },
        )
        assert order_r.status_code == 200
        oid = order_r.json()["id"]

        # Start
        assert (await client.post(f"/manufacturing/{oid}/start", headers=_h(token))).status_code == 200

        # Consume
        c = await client.post(f"/manufacturing/{oid}/consume", headers=_h(token),
                               json={"item_id": item_id, "quantity": 3})
        assert c.status_code == 200

        # Complete
        comp = await client.post(f"/manufacturing/{oid}/complete", headers=_h(token), json={})
        assert comp.status_code == 200

        # Status check
        final = await client.get(f"/manufacturing/{oid}", headers=_h(token))
        assert final.json()["status"] == "completed"

    @pytest.mark.asyncio
    async def test_cannot_complete_order_twice(self, client):
        token = await _register(client, "twice")
        item_r = await client.post("/items", headers=_h(token), json={"sku": "RAW-T", "name": "Raw T", "quantity": 5, "sell_by": "piece"})
        item_id = item_r.json()["id"]

        order_r = await client.post(
            "/manufacturing", headers=_h(token),
            json={"description": "Twice", "inputs": [{"item_id": item_id, "quantity": 2}],
                  "expected_outputs": [{"sku": "OUT-T", "name": "Out T", "quantity": 1}]},
        )
        oid = order_r.json()["id"]
        await client.post(f"/manufacturing/{oid}/start", headers=_h(token))
        await client.post(f"/manufacturing/{oid}/consume", headers=_h(token), json={"item_id": item_id, "quantity": 2})
        await client.post(f"/manufacturing/{oid}/complete", headers=_h(token), json={})
        r2 = await client.post(f"/manufacturing/{oid}/complete", headers=_h(token), json={})
        assert r2.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_completed_order_rejected(self, client):
        token = await _register(client, "cancelcomp")
        item_r = await client.post("/items", headers=_h(token), json={"sku": "RAW-CC", "name": "Raw CC", "quantity": 5, "sell_by": "piece"})
        item_id = item_r.json()["id"]

        order_r = await client.post(
            "/manufacturing", headers=_h(token),
            json={"description": "CC", "inputs": [{"item_id": item_id, "quantity": 1}],
                  "expected_outputs": [{"sku": "OUT-CC", "name": "Out CC", "quantity": 1}]},
        )
        oid = order_r.json()["id"]
        await client.post(f"/manufacturing/{oid}/start", headers=_h(token))
        await client.post(f"/manufacturing/{oid}/consume", headers=_h(token), json={"item_id": item_id, "quantity": 1})
        await client.post(f"/manufacturing/{oid}/complete", headers=_h(token), json={})
        r = await client.post(f"/manufacturing/{oid}/cancel", headers=_h(token), json={})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_consume_more_than_available_rejected(self, client):
        token = await _register(client, "overcons")
        item_r = await client.post("/items", headers=_h(token), json={"sku": "RAW-OV", "name": "Raw OV", "quantity": 3, "sell_by": "piece"})
        item_id = item_r.json()["id"]

        order_r = await client.post(
            "/manufacturing", headers=_h(token),
            json={"description": "Over", "inputs": [{"item_id": item_id, "quantity": 3}],
                  "expected_outputs": [{"sku": "OUT-OV", "name": "Out OV", "quantity": 1}]},
        )
        oid = order_r.json()["id"]
        await client.post(f"/manufacturing/{oid}/start", headers=_h(token))
        r = await client.post(f"/manufacturing/{oid}/consume", headers=_h(token),
                               json={"item_id": item_id, "quantity": 100})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_complete_without_consuming_all_inputs_rejected(self, client):
        token = await _register(client, "unconsumed")
        item1 = (await client.post("/items", headers=_h(token), json={"sku": "R-UC1", "name": "UC1", "quantity": 5, "sell_by": "piece"})).json()["id"]
        item2 = (await client.post("/items", headers=_h(token), json={"sku": "R-UC2", "name": "UC2", "quantity": 5, "sell_by": "piece"})).json()["id"]

        order_r = await client.post(
            "/manufacturing", headers=_h(token),
            json={"description": "Partial", "inputs": [{"item_id": item1, "quantity": 1}, {"item_id": item2, "quantity": 1}],
                  "expected_outputs": [{"sku": "OUT-UC", "name": "Out UC", "quantity": 1}]},
        )
        oid = order_r.json()["id"]
        await client.post(f"/manufacturing/{oid}/start", headers=_h(token))
        # Only consume item1, not item2
        await client.post(f"/manufacturing/{oid}/consume", headers=_h(token), json={"item_id": item1, "quantity": 1})
        r = await client.post(f"/manufacturing/{oid}/complete", headers=_h(token), json={})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_start_already_completed_order_rejected(self, client):
        token = await _register(client, "startcomp")
        item_r = await client.post("/items", headers=_h(token), json={"sku": "RAW-SC", "name": "Raw SC", "quantity": 5, "sell_by": "piece"})
        item_id = item_r.json()["id"]

        order_r = await client.post(
            "/manufacturing", headers=_h(token),
            json={"description": "SC", "inputs": [{"item_id": item_id, "quantity": 1}],
                  "expected_outputs": [{"sku": "OUT-SC", "name": "Out SC", "quantity": 1}]},
        )
        oid = order_r.json()["id"]
        await client.post(f"/manufacturing/{oid}/start", headers=_h(token))
        await client.post(f"/manufacturing/{oid}/consume", headers=_h(token), json={"item_id": item_id, "quantity": 1})
        await client.post(f"/manufacturing/{oid}/complete", headers=_h(token), json={})
        r = await client.post(f"/manufacturing/{oid}/start", headers=_h(token))
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_get_nonexistent_order_returns_404(self, client):
        token = await _register(client, "404")
        r = await client.get("/manufacturing/mfg:doesnotexist", headers=_h(token))
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_import_template_csv(self, client):
        token = await _register(client, "tpl")
        r = await client.get("/manufacturing/import/template", headers=_h(token))
        assert r.status_code == 200
        assert "entity_id" in r.text


# ---------------------------------------------------------------------------
# TestManufacturingDefaultModulesStructure
# ---------------------------------------------------------------------------

class TestManufacturingDefaultModulesStructure:
    def test_module_root_init_exists(self):
        assert os.path.isfile(os.path.join(_MFG_SRC, "__init__.py"))

    def test_package_init_exists(self):
        assert os.path.isfile(os.path.join(_MFG_SRC, "celerp_manufacturing", "__init__.py"))

    def test_routes_py_exists(self):
        assert os.path.isfile(os.path.join(_MFG_SRC, "celerp_manufacturing", "routes.py"))

    def test_ui_routes_py_exists(self):
        assert os.path.isfile(os.path.join(_MFG_SRC, "celerp_manufacturing", "ui_routes.py"))

    def test_projection_handler_py_exists(self):
        assert os.path.isfile(os.path.join(_MFG_SRC, "celerp_manufacturing", "projection_handler.py"))

    def test_requirements_txt_exists(self):
        assert os.path.isfile(os.path.join(_MFG_SRC, "requirements.txt"))

    def test_routes_has_setup_api_routes(self):
        from celerp_manufacturing import routes
        assert callable(getattr(routes, "setup_api_routes", None))

    def test_ui_routes_has_setup_ui_routes(self):
        from celerp_manufacturing import ui_routes
        assert callable(getattr(ui_routes, "setup_ui_routes", None))

    def test_projection_handler_has_apply_fn(self):
        from celerp_manufacturing import projection_handler
        assert callable(getattr(projection_handler, "apply_manufacturing_event", None))


# ---------------------------------------------------------------------------
# TestManufacturingCoreCleanup — core no longer contains mfg router
# ---------------------------------------------------------------------------

class TestManufacturingCoreCleanup:
    def test_core_main_does_not_import_manufacturing_router(self):
        import ast
        main_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "celerp", "main.py"
        )
        with open(main_path) as f:
            tree = ast.parse(f.read())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
        assert not any("manufacturing" in imp for imp in imports), \
            f"celerp/main.py still imports manufacturing: {[i for i in imports if 'manufacturing' in i]}"

    def test_core_main_does_not_include_manufacturing_router(self):
        import ast
        main_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "celerp", "main.py"
        )
        with open(main_path) as f:
            source = f.read()
        assert 'manufacturing.router' not in source

    def test_shell_nav_no_hardcoded_manufacturing(self):
        shell_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "ui", "components", "shell.py"
        )
        with open(shell_path) as f:
            source = f.read()
        # The hardcoded nav tuple was: ("manufacturing", "/manufacturing", "Manufacturing")
        assert '"/manufacturing", "Manufacturing"' not in source, \
            "shell.py still has hardcoded manufacturing nav entry"

    def test_ui_app_does_not_import_manufacturing_routes(self):
        import ast
        app_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "ui", "app.py"
        )
        with open(app_path) as f:
            source = f.read()
        assert "manufacturing" not in source.split("# Register")[0], \
            "ui/app.py still imports manufacturing routes in core setup"

    def test_core_projection_engine_no_mfg_handler_import(self):
        import ast
        engine_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "celerp", "projections", "engine.py"
        )
        with open(engine_path) as f:
            source = f.read()
        assert "from celerp.projections.handlers.manufacturing" not in source
        assert "apply_manufacturing_event" not in source.split("def _get_module_handlers")[0]

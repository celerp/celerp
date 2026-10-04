# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The module slot contract, enforced when a module loads.

Every test writes a real module package and loads it through ``load_all`` (the
complete loader boundary: trust decision, import, protected-import scan, slot
validation, registration), then checks what was registered and what error was
recorded. First-party modules are made first-party the way production does it:
the lock names the folder with its live content digest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from celerp.modules import loader, slots
from celerp.modules.loader import ModuleLoadError, load_all, load_errors

@pytest.fixture(autouse=True)
def clean_state():
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    yield
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    for key in list(sys.modules):
        if key.startswith("slotmod_"):
            sys.modules.pop(key, None)


def _write(base: Path, name: str, slot_map: dict, files: dict[str, str] | None = None) -> Path:
    pkg = base / name
    pkg.mkdir(parents=True)
    for rel, text in (files or {}).items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    manifest = {"name": name, "version": "1.0", "slots": slot_map}
    init = pkg / "__init__.py"
    init.write_text((init.read_text() if init.exists() else "") + f"\nPLUGIN_MANIFEST = {manifest!r}\n")
    return pkg


def _load(tmp_path: Path, name: str, monkeypatch) -> None:
    """Load the third-party module through load_all; a refusal is recorded and the
    module skipped."""
    monkeypatch.setattr(loader, "_first_party_lock", lambda: {})
    load_all(tmp_path, {name})


def _refused(tmp_path, name, monkeypatch) -> str:
    """Load and return the refusal message; assert nothing was registered."""
    _load(tmp_path, name, monkeypatch)
    assert name in load_errors(), f"{name} loaded but should have been refused"
    assert slots.all_slots() == {}, "a refused module registered slot entries"
    assert name not in sys.modules
    return load_errors()[name]


def _accepted(tmp_path, name, monkeypatch) -> None:
    _load(tmp_path, name, monkeypatch)
    assert name not in load_errors(), load_errors().get(name)
    assert [m["name"] for m in loader.loaded_modules()] == [name]


# ── S1: callable slots ────────────────────────────────────────────────────────

# slot -> (callable key, awaited by core, extra keys an entry needs)
CALLABLE = {
    "doc_detail_actions": ("render", False, {}),
    "doc_detail_badges": ("render", False, {}),
    "on_company_created": ("handler", True, {}),
    "on_modules_ready": ("handler", True, {}),
    "doc_finalize_hook": ("handler", True, {}),
    "on_doc_payment": ("handler", True, {}),
    "projection_handler": ("handler", False, {"prefix": "slotx."}),
}

_SYNC_FN = "def fn(*args, **kwargs):\n    return None\n"
_ASYNC_FN = "async def fn(*args, **kwargs):\n    return None\n"


def _right_fn(slot: str) -> str:
    return _ASYNC_FN if CALLABLE[slot][1] else _SYNC_FN


def _wrong_fn(slot: str) -> str:
    return _SYNC_FN if CALLABLE[slot][1] else _ASYNC_FN


def _entry(slot: str, dotted: str) -> dict:
    key, _, extra = CALLABLE[slot]
    return {key: dotted, **extra}


class TestCallableSlots:
    @pytest.mark.parametrize("slot", list(CALLABLE))
    def test_owned_callable_of_the_right_kind_is_accepted(self, slot, tmp_path, monkeypatch):
        name = f"slotmod_ok_{slot}"
        _write(tmp_path, name, {slot: [_entry(slot, f"{name}.hooks:fn")]},
               {"hooks.py": _right_fn(slot)})
        _accepted(tmp_path, name, monkeypatch)
        [registered] = slots.get(slot)
        assert registered["_module"] == name

    @pytest.mark.parametrize("slot", list(CALLABLE))
    def test_foreign_owned_callable_refused(self, slot, tmp_path, monkeypatch):
        name = f"slotmod_foreign_{slot}"
        target = ("celerp.ai.service:run_query" if CALLABLE[slot][1]
                  else "celerp.services.app_paths:is_app_local_path")
        _write(tmp_path, name, {slot: [_entry(slot, target)]})
        msg = _refused(tmp_path, name, monkeypatch)
        assert "does not resolve to source inside" in msg

    @pytest.mark.parametrize("slot", list(CALLABLE))
    def test_decoy_resolving_to_core_refused(self, slot, tmp_path, monkeypatch):
        # The module ships a file at a dotted path an already-loaded core module
        # owns. The file exists in the module's tree, but importlib returns the
        # real core module, so the callable is not the module's own.
        import importlib
        if CALLABLE[slot][1]:
            core_mod, fn, code = "celerp.ai.service", "run_query", _ASYNC_FN.replace("fn", "run_query")
        else:
            core_mod, fn, code = ("celerp.services.app_paths", "is_app_local_path",
                                  _SYNC_FN.replace("fn", "is_app_local_path"))
        importlib.import_module(core_mod)
        parts = core_mod.split(".")
        files = {"/".join(parts[:i]) + "/__init__.py": "" for i in range(1, len(parts))}
        files["/".join(parts) + ".py"] = code
        name = f"slotmod_decoy_{slot}"
        _write(tmp_path, name, {slot: [_entry(slot, f"{core_mod}:{fn}")]}, files)
        msg = _refused(tmp_path, name, monkeypatch)
        assert "outside module" in msg

    @pytest.mark.parametrize("slot", list(CALLABLE))
    def test_protected_import_refused(self, slot, tmp_path, monkeypatch):
        name = f"slotmod_bsl_{slot}"
        _write(tmp_path, name, {slot: [_entry(slot, f"{name}.hooks:fn")]},
               {"hooks.py": "def _later():\n    from celerp.session_gate import require_session_token\n"
                            + _right_fn(slot)})
        msg = _refused(tmp_path, name, monkeypatch)
        assert "protected BSL internals" in msg

    @pytest.mark.parametrize("slot", list(CALLABLE))
    def test_wrong_sync_kind_refused(self, slot, tmp_path, monkeypatch):
        name = f"slotmod_kind_{slot}"
        _write(tmp_path, name, {slot: [_entry(slot, f"{name}.hooks:fn")]},
               {"hooks.py": _wrong_fn(slot)})
        msg = _refused(tmp_path, name, monkeypatch)
        assert ("must be async" in msg) if CALLABLE[slot][1] else ("must not be async" in msg)

    @pytest.mark.parametrize("slot", list(CALLABLE))
    @pytest.mark.parametrize("dotted", [None, "", 7, "no_colon", "a:b:c", ":fn"])
    def test_missing_or_malformed_callable_path_refused(self, slot, dotted, tmp_path, monkeypatch):
        name = f"slotmod_shape_{slot}"
        key, _, extra = CALLABLE[slot]
        entry = {**extra} if dotted is None else {key: dotted, **extra}
        _write(tmp_path, name, {slot: [entry]})
        msg = _refused(tmp_path, name, monkeypatch)
        assert "module.path:function" in msg

    @pytest.mark.parametrize("slot", list(CALLABLE))
    def test_not_callable_refused(self, slot, tmp_path, monkeypatch):
        name = f"slotmod_nc_{slot}"
        _write(tmp_path, name, {slot: [_entry(slot, f"{name}.hooks:fn")]},
               {"hooks.py": "fn = 'not a function'\n"})
        msg = _refused(tmp_path, name, monkeypatch)
        assert "not callable" in msg

    @pytest.mark.parametrize("slot", ["projection_handler", "doc_finalize_hook", "on_doc_payment"])
    def test_third_party_module_can_fill_core_event_slot(self, slot, tmp_path, monkeypatch):
        name = f"slotmod_tp_{slot}"
        _write(tmp_path, name, {slot: [_entry(slot, f"{name}.hooks:fn")]},
               {"hooks.py": _right_fn(slot)})
        _accepted(tmp_path, name, monkeypatch)
        [registered] = slots.get(slot)
        assert registered["_module"] == name

    @pytest.mark.parametrize("slot, entry", [
        ("send_to_targets", {"label": "Packing list", "doc_type": "packing_list"}),
        ("catalog_channel", {"id": "shop", "label": "Shop", "write_permission": "adjust_inventory"}),
    ])
    def test_third_party_module_can_fill_inventory_data_slot(self, slot, entry, tmp_path, monkeypatch):
        name = f"slotmod_tp_{slot}"
        _write(tmp_path, name, {slot: [entry]})
        _accepted(tmp_path, name, monkeypatch)
        [registered] = slots.get(slot)
        assert registered["_module"] == name

    @pytest.mark.parametrize("prefix", [None, "", 5])
    def test_projection_handler_needs_a_prefix(self, prefix, tmp_path, monkeypatch):
        name = "slotmod_prefix"
        entry = {"handler": f"{name}.hooks:fn"}
        if prefix is not None:
            entry["prefix"] = prefix
        _write(tmp_path, name, {"projection_handler": [entry]}, {"hooks.py": _SYNC_FN})
        assert "prefix" in _refused(tmp_path, name, monkeypatch)


# ── S2: slot permission fails closed ──────────────────────────────────────────

_MALFORMED_PERMISSIONS = ["", 0, False, [], {}, None, "not_a_real_permission", 1.5]


def _slot_with_permission(slot: str, name: str, perm) -> tuple[dict, dict]:
    """(slot map, files) for a valid entry of *slot* carrying perm."""
    entries = {
        "nav": {"key": "k", "label": "L", "href": "/x"},
        "item_action": {"label": "L", "href_template": "/x/{entity_id}"},
        "pricing_action": {"label": "L", "href_template": "/x/{entity_id}"},
        "bulk_action": {"label": "L", "form_action": "/x/bulk"},
        "doc_detail_actions": {"render": f"{name}.hooks:fn"},
        "doc_detail_badges": {"render": f"{name}.hooks:fn"},
        "category_schema": {"category": "C", "fields": []},
        "send_to_targets": {"label": "L", "doc_type": "invoice"},
        "catalog_channel": {"id": "c", "label": "C"},
        "search_provider": {"handler": f"{name}.hooks:prov", "result_key": "items"},
    }
    entry = {**entries[slot], "permission": perm}
    value = entry if slot == "search_provider" else [entry]
    files = {"hooks.py": _SYNC_FN + "async def prov(*a, **k):\n    return {'items': []}\n"}
    return {slot: value}, files


_PERMISSION_SLOTS = ["nav", "item_action", "pricing_action", "bulk_action",
                     "doc_detail_actions", "doc_detail_badges", "category_schema",
                     "send_to_targets", "catalog_channel", "search_provider"]


class TestSlotPermission:
    @pytest.mark.parametrize("slot", _PERMISSION_SLOTS)
    @pytest.mark.parametrize("perm", _MALFORMED_PERMISSIONS, ids=repr)
    def test_malformed_permission_refused_at_load(self, slot, perm, tmp_path, monkeypatch):
        name = f"slotmod_perm_{slot}"
        slot_map, files = _slot_with_permission(slot, name, perm)
        _write(tmp_path, name, slot_map, files)
        msg = _refused(tmp_path, name, monkeypatch)
        assert "permission" in msg

    @pytest.mark.parametrize("slot", _PERMISSION_SLOTS)
    def test_valid_permission_accepted(self, slot, tmp_path, monkeypatch):
        name = f"slotmod_permok_{slot}"
        slot_map, files = _slot_with_permission(slot, name, "view_inventory")
        _write(tmp_path, name, slot_map, files)
        _accepted(tmp_path, name, monkeypatch)

    @pytest.mark.parametrize("perm", _MALFORMED_PERMISSIONS, ids=repr)
    def test_malformed_write_permission_refused(self, perm, tmp_path, monkeypatch):
        name = "slotmod_wperm"
        _write(tmp_path, name, {"catalog_channel": [{"id": "c", "label": "C", "write_permission": perm}]})
        assert "permission" in _refused(tmp_path, name, monkeypatch)

    @pytest.mark.parametrize("value", [["woocommerce"], {"id": "x"}, 5, True], ids=repr)
    def test_malformed_requires_connector_refused(self, value, tmp_path, monkeypatch):
        name = "slotmod_conn"
        _write(tmp_path, name, {"bulk_action": [
            {"label": "L", "form_action": "/x", "requires_connector": value}]})
        assert "requires_connector" in _refused(tmp_path, name, monkeypatch)

    @pytest.mark.parametrize("value", ["", 0, None, False], ids=repr)
    def test_empty_requires_connector_means_no_connector_needed(self, value, tmp_path, monkeypatch):
        from ui.module_slots import module_contribution_visible
        name = "slotmod_noconn"
        _write(tmp_path, name, {"bulk_action": [
            {"label": "L", "form_action": "/x", "requires_connector": value}]})
        _accepted(tmp_path, name, monkeypatch)
        [registered] = slots.get("bulk_action")
        assert module_contribution_visible(registered, {}, "owner", set()) is True

    @pytest.mark.parametrize("slot", ["nav", "bulk_action", "item_action"])
    def test_list_item_that_is_not_a_dict_refused(self, slot, tmp_path, monkeypatch):
        name = f"slotmod_notdict_{slot}"
        _write(tmp_path, name, {slot: ["just a string"]})
        assert "dict" in _refused(tmp_path, name, monkeypatch)

    @pytest.mark.parametrize("key", ["_module", "_first_party"])
    def test_runtime_owned_key_cannot_be_spoofed(self, key, tmp_path, monkeypatch):
        name = "slotmod_underscore"
        _write(tmp_path, name, {"nav": [
            {"key": "k", "label": "L", "href": "/x", key: "celerp-docs", "_note": "kept"}]})
        _accepted(tmp_path, name, monkeypatch)
        [registered] = slots.get("nav")
        assert registered["_module"] == name
        assert registered["_first_party"] is False
        assert registered["_note"] == "kept"


class TestSlotVisibilityFailsClosed:
    """Visibility applies the load rule: a present permission must be a valid key
    the role holds. An entry that reached the registry some other way with a
    falsy or unknown permission is hidden, never shown to everyone."""

    @pytest.mark.parametrize("perm", _MALFORMED_PERMISSIONS, ids=repr)
    def test_module_contribution_hidden(self, perm):
        from ui.module_slots import module_contribution_visible
        assert module_contribution_visible({"permission": perm}, {}, "owner") is False

    def test_valid_permission_still_shown_to_a_holder(self):
        from ui.module_slots import module_contribution_visible
        assert module_contribution_visible({"permission": "view_inventory"}, {}, "owner") is True
        assert module_contribution_visible({}, {}, "viewer") is True

    @pytest.mark.parametrize("value", [["x"], {"x": 1}, 5, True], ids=repr)
    def test_malformed_requires_connector_hidden(self, value):
        from ui.module_slots import module_contribution_visible
        assert module_contribution_visible({"requires_connector": value}, {}, "owner", {"x"}) is False

    @pytest.mark.parametrize("perm", _MALFORMED_PERMISSIONS, ids=repr)
    def test_sidebar_nav_entry_hidden(self, perm):
        from ui.components.shell import _sidebar
        slots.register("nav", {"group": "Ops", "key": "slotx", "label": "SlotX",
                               "href": "/slotx", "permission": perm})
        assert "/slotx" not in str(_sidebar("dashboard", settings={}, role="owner"))

    def test_sidebar_nav_entry_with_valid_permission_shown(self):
        from ui.components.shell import _sidebar
        slots.register("nav", {"group": "Ops", "key": "slotx", "label": "SlotX",
                               "href": "/slotx", "permission": "view_inventory"})
        assert "/slotx" in str(_sidebar("dashboard", settings={}, role="owner"))


# ── S3: app-local destinations ────────────────────────────────────────────────

_OFFSITE = ["https://evil.example/x", "//evil.example/x", "javascript:alert(1)",
            "/\\evil.example", "relative/path", "", 5, None, ["/x"]]


def _dest_entry(slot: str, key: str, value) -> dict:
    base = {"nav": {"key": "k", "label": "L", "href": "/x"},
            "bulk_action": {"label": "L", "form_action": "/x"}}[slot]
    return {**base, key: value}


class TestAppLocalDestinations:
    @pytest.mark.parametrize("slot,key", [("nav", "href"), ("nav", "settings_href"),
                                          ("bulk_action", "form_action")])
    @pytest.mark.parametrize("value", _OFFSITE, ids=repr)
    def test_off_site_or_malformed_destination_refused(self, slot, key, value, tmp_path, monkeypatch):
        name = f"slotmod_dest_{slot}_{key}"
        _write(tmp_path, name, {slot: [_dest_entry(slot, key, value)]})
        assert key in _refused(tmp_path, name, monkeypatch)

    def test_bulk_action_without_form_action_refused(self, tmp_path, monkeypatch):
        name = "slotmod_dest_nofa"
        _write(tmp_path, name, {"bulk_action": [{"label": "L"}]})
        assert "form_action" in _refused(tmp_path, name, monkeypatch)

    def test_bulk_action_single_dict_form_also_checked(self, tmp_path, monkeypatch):
        name = "slotmod_dest_dict"
        _write(tmp_path, name, {"bulk_action": {"label": "L", "form_action": "https://evil.example"}})
        assert "form_action" in _refused(tmp_path, name, monkeypatch)

    @pytest.mark.parametrize("slot,key,value", [
        ("nav", "href", "/maintenance"), ("nav", "href", "/docs?type=invoice"),
        ("nav", "settings_href", "/settings/x"), ("bulk_action", "form_action", "/labels/print-bulk"),
    ])
    def test_app_local_destination_accepted(self, slot, key, value, tmp_path, monkeypatch):
        name = f"slotmod_destok_{slot}_{key}"
        _write(tmp_path, name, {slot: [_dest_entry(slot, key, value)]})
        _accepted(tmp_path, name, monkeypatch)
        assert slots.get(slot)[0][key] == value

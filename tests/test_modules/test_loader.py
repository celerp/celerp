# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp.modules.loader

Each test gets a fresh temp directory and a clean slot + sys.modules state.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from celerp.modules import slots
from celerp.modules.loader import (
    ModuleLoadError,
    _PROTECTED_BSL_INTERNALS,
    _load_one,
    load_all,
    loaded_modules,
    register_api_routes,
    register_ui_routes,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    """Clean slots and loaded list before/after every test."""
    slots.clear()
    from celerp.modules import loader
    loader._loaded.clear()
    yield
    slots.clear()
    loader._loaded.clear()
    # Remove any test module packages added to sys.modules
    for key in list(sys.modules.keys()):
        if key.startswith("test_mod_") or key.startswith("good_module") or key.startswith("bad_module"):
            sys.modules.pop(key, None)


def _make_module(base: Path, name: str, manifest: str, extra_code: str = "") -> Path:
    """Write a minimal module package to base/name/__init__.py."""
    pkg = base / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        textwrap.dedent(f"""
        {extra_code}
        PLUGIN_MANIFEST = {manifest}
        """).strip()
    )
    return pkg


# ── Happy path ────────────────────────────────────────────────────────────────

class TestLoadAll:
    def test_empty_dir_returns_empty(self, tmp_path):
        result = load_all(tmp_path, {"anything"})
        assert result == []

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        result = load_all(tmp_path / "does-not-exist", {"anything"})
        assert result == []

    def test_disabled_module_skipped(self, tmp_path):
        _make_module(tmp_path, "my-mod", '{"name": "my-mod", "version": "1.0"}')
        result = load_all(tmp_path, set())   # empty enabled set
        assert result == []

    def test_valid_module_loaded(self, tmp_path):
        _make_module(tmp_path, "good-mod", '{"name": "good-mod", "version": "1.0"}')
        result = load_all(tmp_path, {"good-mod"})
        assert len(result) == 1
        assert result[0]["name"] == "good-mod"

    def test_slots_registered_on_load(self, tmp_path):
        manifest = (
            '{"name": "slot-mod", "version": "1.0", '
            '"slots": {"nav": {"label": "Test", "href": "/test", "order": 50}}}'
        )
        _make_module(tmp_path, "slot-mod", manifest)
        load_all(tmp_path, {"slot-mod"})
        nav = slots.get("nav")
        assert len(nav) == 1
        assert nav[0]["label"] == "Test"
        assert nav[0]["_module"] == "slot-mod"

    def test_multiple_modules_all_loaded(self, tmp_path):
        for i in range(3):
            _make_module(tmp_path, f"mod-{i}", f'{{"name": "mod-{i}", "version": "1.0"}}')
        result = load_all(tmp_path, {f"mod-{i}" for i in range(3)})
        assert len(result) == 3

    def test_broken_module_skipped_others_continue(self, tmp_path):
        _make_module(tmp_path, "good", '{"name": "good", "version": "1.0"}')
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "__init__.py").write_text("raise RuntimeError('import failed')")
        result = load_all(tmp_path, {"good", "bad"})
        assert len(result) == 1
        assert result[0]["name"] == "good"

    def test_module_without_init_skipped(self, tmp_path):
        (tmp_path / "no-init").mkdir()
        result = load_all(tmp_path, {"no-init"})
        assert result == []

    def test_module_without_manifest_skipped(self, tmp_path):
        pkg = tmp_path / "no-manifest"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("# no manifest here")
        result = load_all(tmp_path, {"no-manifest"})
        assert result == []

    def test_module_missing_name_field_skipped(self, tmp_path):
        _make_module(tmp_path, "no-name", '{"version": "1.0"}')
        result = load_all(tmp_path, {"no-name"})
        assert result == []

    def test_module_missing_version_field_skipped(self, tmp_path):
        _make_module(tmp_path, "no-version", '{"name": "no-version"}')
        result = load_all(tmp_path, {"no-version"})
        assert result == []


# ── BSL protection ────────────────────────────────────────────────────────────

class TestBSLProtection:
    def test_protected_internals_set_is_complete(self):
        assert "celerp.session_gate" in _PROTECTED_BSL_INTERNALS
        assert "celerp.ai.service" in _PROTECTED_BSL_INTERNALS
        assert "celerp.ai.quota" in _PROTECTED_BSL_INTERNALS

    def test_module_importing_session_gate_rejected(self, tmp_path):
        pkg = tmp_path / "bad-session"
        pkg.mkdir()
        # Simulate: from celerp.session_gate import require_session_token
        # The real module object will be present in the module's globals
        (pkg / "__init__.py").write_text(
            "from celerp.session_gate import require_session_token\n"
            "PLUGIN_MANIFEST = {'name': 'bad-session', 'version': '1.0'}"
        )
        result = load_all(tmp_path, {"bad-session"})
        # Module should be rejected
        assert all(m.get("name") != "bad-session" for m in result)

    def test_load_one_raises_on_bsl_violation(self, tmp_path):
        pkg = tmp_path / "violator"
        pkg.mkdir()
        # Simulate: from celerp.session_gate import require_session_token
        (pkg / "__init__.py").write_text(
            "from celerp.session_gate import require_session_token\n"
            "PLUGIN_MANIFEST = {'name': 'violator', 'version': '1.0'}"
        )
        with pytest.raises(ModuleLoadError, match="protected BSL internals"):
            _load_one(pkg, "violator")

    def test_bsl_violation_error_message_contains_urls(self, tmp_path):
        pkg = tmp_path / "violator2"
        pkg.mkdir()
        # Simulate: from celerp.ai.service import run_query
        (pkg / "__init__.py").write_text(
            "from celerp.ai.service import run_query\n"
            "PLUGIN_MANIFEST = {'name': 'violator2', 'version': '1.0'}"
        )
        with pytest.raises(ModuleLoadError) as exc_info:
            _load_one(pkg, "violator2")
        msg = str(exc_info.value)
        assert "celerp.com/licenses/bsl" in msg
        assert "celerp.com/docs/modules/ai-api" in msg

    def test_clean_module_not_rejected(self, tmp_path):
        _make_module(tmp_path, "clean-mod", '{"name": "clean-mod", "version": "1.0"}')
        pkg = tmp_path / "clean-mod"
        result = _load_one(pkg, "clean-mod")
        assert result is not None
        assert result["name"] == "clean-mod"


# ── Route registration ────────────────────────────────────────────────────────

class TestRouteRegistration:
    def test_register_api_routes_calls_setup(self, tmp_path):
        called = []

        class _FakeApp:
            pass

        pkg = tmp_path / "route-mod"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(
            "PLUGIN_MANIFEST = {'name': 'route-mod', 'version': '1.0', "
            "'api_routes': 'route_mod_api'}"
        )
        import types
        api_mod = types.ModuleType("route_mod_api")
        api_mod.setup_api_routes = lambda app: called.append("api")
        sys.modules["route_mod_api"] = api_mod

        manifests = [{"name": "route-mod", "version": "1.0", "api_routes": "route_mod_api"}]
        register_api_routes(_FakeApp(), manifests)
        assert called == ["api"]
        sys.modules.pop("route_mod_api", None)

    def test_register_ui_routes_calls_setup(self, tmp_path):
        called = []

        class _FakeApp:
            pass

        import types
        ui_mod = types.ModuleType("route_mod_ui")
        ui_mod.setup_ui_routes = lambda app: called.append("ui")
        sys.modules["route_mod_ui"] = ui_mod

        manifests = [{"name": "route-mod", "version": "1.0", "ui_routes": "route_mod_ui"}]
        register_ui_routes(_FakeApp(), manifests)
        assert called == ["ui"]
        sys.modules.pop("route_mod_ui", None)

    def test_broken_route_module_skipped_gracefully(self):
        class _FakeApp:
            pass

        manifests = [{"name": "broken", "version": "1.0", "api_routes": "nonexistent.module.xyz"}]
        # Should not raise
        register_api_routes(_FakeApp(), manifests)

    def test_no_routes_key_is_noop(self):
        class _FakeApp:
            pass

        manifests = [{"name": "no-routes", "version": "1.0"}]
        register_api_routes(_FakeApp(), manifests)
        register_ui_routes(_FakeApp(), manifests)


# ── Slot list contributions ───────────────────────────────────────────────────

class TestSlotListContributions:
    """Modules can register a list of contributions for a single slot."""

    def test_slot_list_registers_all_items(self, tmp_path):
        manifest = (
            '{"name": "multi-slot", "version": "1.0", '
            '"slots": {"bulk_action": ['
            '{"label": "A", "form_action": "/a"}, '
            '{"label": "B", "form_action": "/b"}]}}'
        )
        _make_module(tmp_path, "multi-slot", manifest)
        load_all(tmp_path, {"multi-slot"})
        actions = slots.get("bulk_action")
        assert len(actions) == 2
        labels = {a["label"] for a in actions}
        assert labels == {"A", "B"}

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


# ── Dependency system ─────────────────────────────────────────────────────────

class TestDependencySystem:
    """Comprehensive tests for module dependency resolution and enforcement.

    These tests cover the full dependency lifecycle:
    - _topo_sort correctly orders modules
    - load_all enforces declared dependencies at load time
    - Missing deps (not enabled, not on disk) cause the dependent to be skipped
    - Cascade: A→B→C where C fails causes A and B to be skipped
    - Circular deps don't cause RecursionError
    - resolve_install_order (config.py) auto-resolves transitive deps
    - celerp-docs declares celerp-contacts as a dependency (regression guard)
    """

    def _make(self, base: Path, name: str, depends_on: list[str] | None = None) -> Path:
        pkg = base / name
        pkg.mkdir(parents=True, exist_ok=True)
        deps = repr(depends_on or [])
        (pkg / "__init__.py").write_text(
            f'PLUGIN_MANIFEST = {{"name": "{name}", "version": "1.0", "depends_on": {deps}}}\n'
        )
        return pkg

    # ── _topo_sort ordering ────────────────────────────────────────────────────

    def test_dependency_loads_before_dependent(self, tmp_path):
        """dep must appear before the module that requires it."""
        self._make(tmp_path, "base")
        self._make(tmp_path, "ext", depends_on=["base"])
        result = load_all(tmp_path, {"base", "ext"})
        names = [m["name"] for m in result]
        assert names.index("base") < names.index("ext")

    def test_transitive_deps_resolved_in_order(self, tmp_path):
        """A→B→C: load order must be C, B, A."""
        self._make(tmp_path, "mod-c")
        self._make(tmp_path, "mod-b", depends_on=["mod-c"])
        self._make(tmp_path, "mod-a", depends_on=["mod-b"])
        result = load_all(tmp_path, {"mod-a", "mod-b", "mod-c"})
        names = [m["name"] for m in result]
        assert names.index("mod-c") < names.index("mod-b") < names.index("mod-a")

    def test_diamond_dep_loads_each_module_once(self, tmp_path):
        """A→{B,C}→D: D must appear only once."""
        self._make(tmp_path, "mod-d")
        self._make(tmp_path, "mod-b", depends_on=["mod-d"])
        self._make(tmp_path, "mod-c", depends_on=["mod-d"])
        self._make(tmp_path, "mod-a", depends_on=["mod-b", "mod-c"])
        result = load_all(tmp_path, {"mod-a", "mod-b", "mod-c", "mod-d"})
        names = [m["name"] for m in result]
        assert names.count("mod-d") == 1
        assert names.count("mod-a") == 1
        assert len(names) == 4

    # ── Missing deps: not enabled ──────────────────────────────────────────────

    def test_module_skipped_when_dep_not_in_enabled_set(self, tmp_path):
        """Module with dep not in enabled set is silently skipped."""
        self._make(tmp_path, "dep-mod")
        self._make(tmp_path, "needs-dep", depends_on=["dep-mod"])
        # dep-mod is on disk but NOT in enabled set
        result = load_all(tmp_path, {"needs-dep"})
        assert not any(m["name"] == "needs-dep" for m in result)

    def test_module_loads_when_dep_enabled(self, tmp_path):
        """Module loads correctly when dep IS in enabled set."""
        self._make(tmp_path, "dep-mod")
        self._make(tmp_path, "needs-dep", depends_on=["dep-mod"])
        result = load_all(tmp_path, {"dep-mod", "needs-dep"})
        names = [m["name"] for m in result]
        assert "dep-mod" in names
        assert "needs-dep" in names

    # ── Missing deps: not on disk ──────────────────────────────────────────────

    def test_module_skipped_when_dep_not_on_disk(self, tmp_path):
        """Module whose dep is in enabled set but missing from disk is skipped."""
        self._make(tmp_path, "needs-ghost", depends_on=["ghost-dep"])
        # ghost-dep is in enabled set but has no directory
        result = load_all(tmp_path, {"needs-ghost", "ghost-dep"})
        assert not any(m["name"] == "needs-ghost" for m in result)

    # ── Cascade failures ────────────────────────────────────────────────────────

    def test_cascade_skip_when_transitive_dep_missing(self, tmp_path):
        """A→B→C: if C is not enabled, both B and A are skipped."""
        self._make(tmp_path, "mod-b", depends_on=["mod-c"])
        self._make(tmp_path, "mod-a", depends_on=["mod-b"])
        # mod-c not in enabled set, not on disk
        result = load_all(tmp_path, {"mod-a", "mod-b"})
        names = [m["name"] for m in result]
        assert "mod-a" not in names
        assert "mod-b" not in names

    def test_independent_modules_unaffected_by_sibling_skip(self, tmp_path):
        """Module with broken dep doesn't affect unrelated modules."""
        self._make(tmp_path, "good-mod")
        self._make(tmp_path, "bad-dep-mod", depends_on=["nonexistent"])
        result = load_all(tmp_path, {"good-mod", "bad-dep-mod"})
        names = [m["name"] for m in result]
        assert "good-mod" in names
        assert "bad-dep-mod" not in names

    # ── Circular deps ──────────────────────────────────────────────────────────

    def test_circular_dep_does_not_crash(self, tmp_path):
        """Circular A→B→A must not cause RecursionError."""
        self._make(tmp_path, "circ-a", depends_on=["circ-b"])
        self._make(tmp_path, "circ-b", depends_on=["circ-a"])
        # Should not raise; both may be skipped
        result = load_all(tmp_path, {"circ-a", "circ-b"})
        # We don't assert on result content — just no exception
        assert isinstance(result, list)

    def test_self_dep_does_not_crash(self, tmp_path):
        """Module that depends on itself must not cause RecursionError."""
        self._make(tmp_path, "self-dep", depends_on=["self-dep"])
        result = load_all(tmp_path, {"self-dep"})
        assert isinstance(result, list)

    # ── resolve_install_order (config.py) ──────────────────────────────────────

    def test_resolve_install_order_includes_transitive_deps(self, tmp_path):
        """resolve_install_order must auto-include undeclared transitive deps."""
        from celerp.config import resolve_install_order
        self._make(tmp_path, "req-c")
        self._make(tmp_path, "req-b", depends_on=["req-c"])
        self._make(tmp_path, "req-a", depends_on=["req-b"])
        # Only req-a requested; req-b and req-c must appear automatically
        result = resolve_install_order(["req-a"], tmp_path)
        assert "req-c" in result
        assert "req-b" in result
        assert "req-a" in result
        assert result.index("req-c") < result.index("req-b") < result.index("req-a")

    def test_resolve_install_order_no_duplicates(self, tmp_path):
        """resolve_install_order must not produce duplicate entries."""
        from celerp.config import resolve_install_order
        self._make(tmp_path, "shared")
        self._make(tmp_path, "mod-x", depends_on=["shared"])
        self._make(tmp_path, "mod-y", depends_on=["shared"])
        result = resolve_install_order(["mod-x", "mod-y", "shared"], tmp_path)
        assert result.count("shared") == 1

    def test_resolve_install_order_handles_missing_gracefully(self, tmp_path):
        """resolve_install_order with unknown module name doesn't crash."""
        from celerp.config import resolve_install_order
        # ghost-xyz doesn't exist in tmp_path
        result = resolve_install_order(["ghost-xyz"], tmp_path)
        # Returns with ghost-xyz (no crash, no auto-skip — install may fail later)
        assert "ghost-xyz" in result

    # ── Real module manifest validation ────────────────────────────────────────

    def test_celerp_docs_declares_contacts_as_dependency(self):
        """celerp-docs must declare celerp-contacts in depends_on.

        Regression: contacts was absent from docs' dependency list. Documents
        use contact_id/contact_name fields and the Customers/Vendors nav tabs
        are provided by celerp-contacts. Without this dependency, new installs
        that enable celerp-docs via a preset silently skip celerp-contacts
        (it is not auto-installed), leaving users unable to create customers.
        """
        import ast as _ast
        manifest_path = (
            Path(__file__).parent.parent.parent
            / "default_modules" / "celerp-docs" / "__init__.py"
        )
        tree = _ast.parse(manifest_path.read_text())
        manifest = None
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Assign):
                for t in node.targets:
                    if isinstance(t, _ast.Name) and t.id == "PLUGIN_MANIFEST":
                        manifest = _ast.literal_eval(node.value)
        assert manifest is not None, "Could not parse PLUGIN_MANIFEST from celerp-docs"
        deps = manifest.get("depends_on") or []
        assert "celerp-contacts" in deps, (
            f"celerp-docs must declare celerp-contacts in depends_on to ensure "
            f"contacts is auto-installed with docs. Found: {deps}"
        )

    def test_celerp_contacts_loads_before_docs_via_dep_system(self, tmp_path):
        """With real modules: contacts must load before docs when both enabled."""
        real_dir = Path(__file__).parent.parent.parent / "default_modules"
        if not real_dir.exists():
            pytest.skip("default_modules not available")
        from celerp.modules import loader as _loader, slots as _slots
        _loader._loaded.clear()
        _slots.clear()
        import sys as _sys
        try:
            result = _loader.load_all(
                real_dir,
                {"celerp-inventory", "celerp-contacts", "celerp-docs"},
            )
            names = [m["name"] for m in result]
            assert "celerp-contacts" in names
            assert "celerp-docs" in names
            assert names.index("celerp-contacts") < names.index("celerp-docs"), (
                "celerp-contacts must load before celerp-docs (it's a declared dep)"
            )
        finally:
            _loader._loaded.clear()
            _slots.clear()
            for k in list(_sys.modules.keys()):
                if "celerp_docs" in k or "celerp_contacts" in k or "celerp_inventory" in k:
                    _sys.modules.pop(k, None)

    def test_celerp_docs_skipped_when_contacts_not_enabled(self, tmp_path):
        """With real modules: docs is skipped when contacts not in enabled set.

        This is the enforcement side of the dependency declaration — not just
        a sorted install order, but an actual runtime block.
        """
        real_dir = Path(__file__).parent.parent.parent / "default_modules"
        if not real_dir.exists():
            pytest.skip("default_modules not available")
        from celerp.modules import loader as _loader, slots as _slots
        _loader._loaded.clear()
        _slots.clear()
        import sys as _sys
        try:
            result = _loader.load_all(
                real_dir,
                {"celerp-inventory", "celerp-docs"},  # contacts intentionally absent
            )
            names = [m["name"] for m in result]
            assert "celerp-docs" not in names, (
                "celerp-docs must be skipped when celerp-contacts is not in the enabled set"
            )
        finally:
            _loader._loaded.clear()
            _slots.clear()
            for k in list(_sys.modules.keys()):
                if "celerp_docs" in k or "celerp_inventory" in k:
                    _sys.modules.pop(k, None)

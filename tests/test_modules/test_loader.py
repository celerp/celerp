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

from contextlib import contextmanager

from celerp.modules import slots
from celerp.modules.loader import (
    ModuleLoadError,
    _PROTECTED_BSL_INTERNALS,
    _load_one,
    load_all,
    load_errors,
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
    loader._load_errors.clear()
    yield
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
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


@contextmanager
def _preserved_sys_modules(prefixes: list[str]):
    """Save already-imported sys.modules entries matching any prefix; afterwards
    remove whatever the test imported and restore the originals. Evicting
    without restoring breaks later tests: mock.patch by import path would
    re-import a FRESH module object while the app under test still calls
    functions from the original namespace, so the patch lands on a dead copy."""
    saved = {k: v for k, v in sys.modules.items() if any(p in k for p in prefixes)}
    try:
        yield
    finally:
        for k in list(sys.modules.keys()):
            if any(p in k for p in prefixes):
                sys.modules.pop(k, None)
        sys.modules.update(saved)


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


# ── Premium license gate ──────────────────────────────────────────────────────
# is_premium_path() (celerp.modules.license) treats a directory carrying the
# marketplace installer's PREMIUM_MARKER as license-gated. load_all resolves a
# relay JWT from settings.gateway_token (the real GATEWAY_TOKEN credential),
# exchanged via exchange_api_key_for_jwt. A prior version read CELERP_RELAY_URL
# / CELERP_INSTANCE_JWT, which are set in no environment, so as written the gate
# could not have verified a license once premium modules shipped (no premium
# modules exist yet, so nothing was actually ungated in production).

class TestPremiumLicenseGate:
    def _make_premium_module(self, tmp_path, name="paid-mod") -> Path:
        from celerp.modules.importer import PREMIUM_MARKER
        pkg = _make_module(tmp_path, name, f'{{"name": "{name}", "version": "1.0"}}')
        (pkg / PREMIUM_MARKER).write_text("")
        return pkg

    def test_no_gateway_token_skips_check_dev_mode(self, tmp_path, monkeypatch):
        """Never-activated instance (no gateway_token at all): the check is
        skipped gracefully, not treated as a license failure."""
        from celerp.config import settings as _s
        monkeypatch.setattr(_s, "gateway_token", "")
        self._make_premium_module(tmp_path)
        result = load_all(tmp_path, {"paid-mod"})
        assert len(result) == 1

    def test_no_premium_module_makes_zero_token_exchanges(self, tmp_path, monkeypatch):
        """No premium module present -> the relay token exchange (a blocking
        network call at startup) must never happen. Guards the lazy-resolve:
        every ordinary user with only free/bundled modules pays no network cost
        at load time."""
        from celerp.config import settings as _s
        calls = []
        monkeypatch.setattr(_s, "gateway_token", "api-key-1")
        monkeypatch.setattr(_s, "gateway_http_url", "https://relay.test")
        monkeypatch.setattr(
            "celerp.modules.loader.exchange_api_key_for_jwt",
            lambda relay_url, api_key: calls.append(1) or "relay-jwt-1")
        _make_module(tmp_path, "free-a", '{"name": "free-a", "version": "1.0"}')
        _make_module(tmp_path, "free-b", '{"name": "free-b", "version": "1.0"}')
        result = load_all(tmp_path, {"free-a", "free-b"})
        assert len(result) == 2
        assert calls == [], "no premium module: must not exchange a token"

    def test_multiple_premium_modules_exchange_token_once(self, tmp_path, monkeypatch):
        """The JWT is the same for every module, so N premium modules must
        trigger exactly ONE token exchange, not N."""
        from celerp.config import settings as _s
        calls = []
        monkeypatch.setattr(_s, "gateway_token", "api-key-1")
        monkeypatch.setattr(_s, "gateway_http_url", "https://relay.test")
        monkeypatch.setattr(
            "celerp.modules.loader.exchange_api_key_for_jwt",
            lambda relay_url, api_key: calls.append(1) or "relay-jwt-1")
        monkeypatch.setattr("celerp.modules.loader.check_license", lambda **kw: True)
        self._make_premium_module(tmp_path, name="paid-a")
        self._make_premium_module(tmp_path, name="paid-b")
        self._make_premium_module(tmp_path, name="paid-c")
        result = load_all(tmp_path, {"paid-a", "paid-b", "paid-c"})
        assert len(result) == 3
        assert len(calls) == 1, f"expected exactly one token exchange, got {len(calls)}"

    def test_valid_license_loads_module(self, tmp_path, monkeypatch):
        from celerp.config import settings as _s
        monkeypatch.setattr(_s, "gateway_token", "api-key-1")
        monkeypatch.setattr(_s, "gateway_http_url", "https://relay.test")
        monkeypatch.setattr(
            "celerp.modules.loader.exchange_api_key_for_jwt",
            lambda relay_url, api_key: "relay-jwt-1")
        monkeypatch.setattr(
            "celerp.modules.loader.check_license",
            lambda **kw: True)
        self._make_premium_module(tmp_path)
        result = load_all(tmp_path, {"paid-mod"})
        assert len(result) == 1

    def test_no_license_skips_module_with_load_error(self, tmp_path, monkeypatch):
        from celerp.config import settings as _s
        from celerp.modules import loader as _loader
        monkeypatch.setattr(_s, "gateway_token", "api-key-1")
        monkeypatch.setattr(_s, "gateway_http_url", "https://relay.test")
        monkeypatch.setattr(
            "celerp.modules.loader.exchange_api_key_for_jwt",
            lambda relay_url, api_key: "relay-jwt-1")
        monkeypatch.setattr(
            "celerp.modules.loader.check_license",
            lambda **kw: False)
        self._make_premium_module(tmp_path)
        result = load_all(tmp_path, {"paid-mod"})
        assert result == []
        assert "no valid license" in _loader.load_errors()["paid-mod"]

    def test_token_exchange_failure_still_verifies_offline(self, tmp_path, monkeypatch):
        """gateway_token is set but the live token exchange fails (rotated key, or
        a transient startup failure). The check is not skipped: the license is
        still verified offline (offline_only=True), so with no offline lifetime
        JWT and no grace cache the module is not loaded."""
        from celerp.config import settings as _s
        from celerp.modules import loader as _loader
        seen = {}
        monkeypatch.setattr(_s, "gateway_token", "stale-key")
        monkeypatch.setattr(_s, "gateway_http_url", "https://relay.test")
        monkeypatch.setattr(
            "celerp.modules.loader.exchange_api_key_for_jwt",
            lambda relay_url, api_key: None)

        def _spy(**kw):
            seen.update(kw)
            return False   # no offline lifetime JWT, no grace cache -> deny

        monkeypatch.setattr("celerp.modules.loader.check_license", _spy)
        self._make_premium_module(tmp_path)
        result = load_all(tmp_path, {"paid-mod"})
        assert result == []                       # not loaded without a valid license
        assert seen.get("offline_only") is True   # decided offline, relay not called live
        assert seen.get("instance_jwt") == ""     # no live token was available
        assert "no valid license" in _loader.load_errors()["paid-mod"]

    def test_token_exchange_failure_still_honors_offline_license(self, tmp_path, monkeypatch):
        """The flip side: when the exchange fails but the offline path (lifetime
        JWT or grace cache) grants, the module still loads - the relay being
        briefly unreachable never strands a genuinely licensed module."""
        from celerp.config import settings as _s
        monkeypatch.setattr(_s, "gateway_token", "stale-key")
        monkeypatch.setattr(_s, "gateway_http_url", "https://relay.test")
        monkeypatch.setattr(
            "celerp.modules.loader.exchange_api_key_for_jwt",
            lambda relay_url, api_key: None)
        monkeypatch.setattr("celerp.modules.loader.check_license",
                            lambda **kw: True)   # offline path grants
        self._make_premium_module(tmp_path)
        result = load_all(tmp_path, {"paid-mod"})
        assert len(result) == 1


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
        import types
        called = []

        class _FakeApp:
            def __init__(self):
                self.router = types.SimpleNamespace(routes=[])

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
        import types
        called = []

        class _FakeApp:
            def __init__(self):
                self.router = types.SimpleNamespace(routes=[])

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

    def test_circular_dep_detected_and_both_skipped(self, tmp_path):
        """Circular A→B→A must not crash, and the cycle must be detected: both
        modules are skipped with a recorded error, not loaded in a bogus order."""
        from celerp.modules.loader import load_errors
        self._make(tmp_path, "circ-a", depends_on=["circ-b"])
        self._make(tmp_path, "circ-b", depends_on=["circ-a"])
        result = load_all(tmp_path, {"circ-a", "circ-b"})
        names = [m["name"] for m in result]
        assert "circ-a" not in names
        assert "circ-b" not in names
        errors = load_errors()
        # at least one member of the cycle is flagged as such (the other is
        # reported as depending on a skipped module)
        assert "cycle" in errors.get("circ-a", "").lower() or \
               "cycle" in errors.get("circ-b", "").lower()

    def test_self_dep_detected_and_skipped(self, tmp_path):
        """A module depending on itself is a trivial cycle: skipped, not loaded."""
        from celerp.modules.loader import load_errors
        self._make(tmp_path, "self-dep", depends_on=["self-dep"])
        result = load_all(tmp_path, {"self-dep"})
        assert not any(m["name"] == "self-dep" for m in result)
        assert "cycle" in load_errors().get("self-dep", "").lower()

    def test_cycle_does_not_poison_unrelated_module(self, tmp_path):
        """A dependency cycle among some modules must not prevent an unrelated,
        healthy module from loading."""
        self._make(tmp_path, "circ-a", depends_on=["circ-b"])
        self._make(tmp_path, "circ-b", depends_on=["circ-a"])
        self._make(tmp_path, "healthy")
        result = load_all(tmp_path, {"circ-a", "circ-b", "healthy"})
        names = [m["name"] for m in result]
        assert "healthy" in names
        assert "circ-a" not in names and "circ-b" not in names

    def test_three_node_cycle_detected_and_all_skipped(self, tmp_path):
        """A multi-hop cycle A->B->C->A exercises the back-edge through more than
        one on_stack frame; all three must be skipped, none loaded."""
        self._make(tmp_path, "n-a", depends_on=["n-b"])
        self._make(tmp_path, "n-b", depends_on=["n-c"])
        self._make(tmp_path, "n-c", depends_on=["n-a"])
        result = load_all(tmp_path, {"n-a", "n-b", "n-c"})
        names = [m["name"] for m in result]
        assert "n-a" not in names and "n-b" not in names and "n-c" not in names

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

    def test_resolve_install_order_survives_a_cycle(self, tmp_path):
        """resolve_install_order must terminate (no RecursionError) on a cyclic
        depends_on graph; the loader's own _topo_sort is the enforcement point, so
        here we only pin that this helper returns rather than blowing the stack."""
        from celerp.config import resolve_install_order
        self._make(tmp_path, "cyc-a", depends_on=["cyc-b"])
        self._make(tmp_path, "cyc-b", depends_on=["cyc-a"])
        result = resolve_install_order(["cyc-a"], tmp_path)
        assert "cyc-a" in result and "cyc-b" in result

    def test_resolve_install_order_finds_deps_in_module_dir(self, tmp_path, monkeypatch):
        """A marketplace/sideloaded module lives in MODULE_DIR, not the bundled
        trees. Its depends_on must still be discovered and pre-enabled, or the
        loader skips it at next boot as 'requires X, which is not enabled'."""
        from celerp.config import resolve_install_order
        bundled = tmp_path / "default_modules"
        bundled.mkdir()
        market = tmp_path / "market"
        market.mkdir()
        # third-party module and its dependency both land in MODULE_DIR
        self._make(market, "third-party", depends_on=["tp-dep"])
        self._make(market, "tp-dep")
        monkeypatch.setenv("MODULE_DIR", str(market))
        result = resolve_install_order(["third-party"], bundled)
        assert "tp-dep" in result
        assert result.index("tp-dep") < result.index("third-party")

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
        with _preserved_sys_modules(["celerp_docs", "celerp_contacts", "celerp_inventory"]):
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
        with _preserved_sys_modules(["celerp_docs", "celerp_inventory"]):
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


# ── Electron: trusted module path via CELERP_TRUSTED_MODULE_DIRS ──────────────

class TestElectronTrustedModuleDirs:
    """Directory-resolution and write-guard behaviour for the seeded-module case.

    Electron seeds default_modules/ from app resources into DATA_DIR/modules/ (a
    user-data directory outside APP_DIR). _BUNDLED_MODULES_DIRS resolves relative
    to loader.py plus any CELERP_TRUSTED_MODULE_DIRS entries; is_bundled_dir uses
    it to refuse writes into the shipped source tree. It is NOT a trust decision:
    first-party trust is by content digest against the committed lock (see the
    content-identity tests below), so a seeded copy is trusted by matching content,
    never by its directory. These tests cover the directory resolution itself and
    that a BSL-importing module is still scanned when it is not first-party.
    """

    def _make_module_with_bsl_import(self, base: Path, name: str, import_line: str) -> Path:
        """Create a module package that imports a BSL-protected internal."""
        pkg = base / name
        pkg.mkdir(parents=True)
        # The import will fail at runtime (module doesn't exist in tests),
        # but the AST scanner and runtime checks happen before that.
        # We write a fake module into sys.modules to make the import succeed
        # so we can test the trust path, not the import itself.
        (pkg / "__init__.py").write_text(
            f"PLUGIN_MANIFEST = {{'name': '{name}', 'version': '1.0'}}\n"
        )
        # Write a separate routes.py that contains the BSL import
        routes = pkg / "routes.py"
        routes.write_text(
            f"{import_line}\n"
            f"def setup_api_routes(app): pass\n"
        )
        # Update manifest to reference routes
        pkg_mod = name.replace("-", "_")
        (pkg / "__init__.py").write_text(
            f"PLUGIN_MANIFEST = {{'name': '{name}', 'version': '1.0', "
            f"'api_routes': '{pkg_mod}.routes'}}\n"
        )
        return pkg

    def test_module_with_bsl_import_rejected_when_not_trusted(self, tmp_path, monkeypatch):
        """Module importing BSL internal is rejected when its dir is NOT trusted."""
        monkeypatch.delenv("CELERP_TRUSTED_MODULE_DIRS", raising=False)
        monkeypatch.delenv("MODULE_DIR", raising=False)
        from celerp.modules import loader as _loader
        # Restrict trusted dirs to only the default bundled path (not tmp_path)
        monkeypatch.setattr(
            _loader, "_BUNDLED_MODULES_DIRS",
            (Path(__file__).resolve().parent.parent.parent / "default_modules",),
        )
        # Build a proper inner-package structure so the AST scanner finds the file
        pkg = tmp_path / "celerp-ai-sim"
        inner = pkg / "celerp_ai_sim"
        inner.mkdir(parents=True)
        (inner / "__init__.py").write_text("")
        (inner / "routes.py").write_text(
            "from celerp.session_gate import require_session_token\n"
            "def setup_api_routes(app): pass\n"
        )
        (pkg / "__init__.py").write_text(
            "PLUGIN_MANIFEST = {'name': 'celerp-ai-sim', 'version': '1.0', "
            "'api_routes': 'celerp_ai_sim.routes'}\n"
        )
        # Module dir is tmp_path, which is NOT in _BUNDLED_MODULES_DIRS → rejected
        result = _loader.load_all(tmp_path, {"celerp-ai-sim"})
        assert not any(m["name"] == "celerp-ai-sim" for m in result), (
            "BSL-importing module in untrusted directory must be rejected"
        )

    def test_trusted_module_dirs_resolve_into_bundled_dirs(
        self, tmp_path, monkeypatch
    ):
        """A CELERP_TRUSTED_MODULE_DIRS entry is included in _BUNDLED_MODULES_DIRS,
        so the write-guard treats it as part of the shipped source tree."""
        from celerp.modules import loader as _loader
        # Point CELERP_TRUSTED_MODULE_DIRS at tmp_path
        monkeypatch.setenv("CELERP_TRUSTED_MODULE_DIRS", str(tmp_path))
        # Re-resolve so tmp_path is included
        new_trusted = _loader._resolve_bundled_dirs()
        monkeypatch.setattr(_loader, "_BUNDLED_MODULES_DIRS", new_trusted)
        assert tmp_path.resolve() in new_trusted, (
            "_resolve_bundled_dirs must include CELERP_TRUSTED_MODULE_DIRS path"
        )

    def test_module_dir_is_not_in_bundled_dirs(self, tmp_path, monkeypatch):
        """MODULE_DIR must NOT be folded into _BUNDLED_MODULES_DIRS.

        The write-guard would otherwise refuse imports into the user's own module
        dir. Trust is unaffected either way - it is decided by content digest, not
        by directory membership.
        """
        from celerp.modules import loader as _loader
        monkeypatch.delenv("CELERP_TRUSTED_MODULE_DIRS", raising=False)
        monkeypatch.setenv("MODULE_DIR", str(tmp_path))
        new_trusted = _loader._resolve_bundled_dirs()
        assert tmp_path.resolve() not in new_trusted, (
            "MODULE_DIR must not be folded into the bundled-source set"
        )

    def test_third_party_in_module_dir_still_checked(self, tmp_path, monkeypatch):
        """A NON-bundled name in the seeded/module dir keeps full BSL checks."""
        from celerp.modules import loader as _loader
        monkeypatch.delenv("CELERP_TRUSTED_MODULE_DIRS", raising=False)
        monkeypatch.setenv("MODULE_DIR", str(tmp_path))
        monkeypatch.setattr(
            _loader, "_BUNDLED_MODULES_DIRS", _loader._resolve_bundled_dirs()
        )
        # Inner-package layout so the AST scanner finds the routes file
        # (same shape as test_module_with_bsl_import_rejected_when_not_trusted).
        pkg = tmp_path / "sneaky-tools"
        inner = pkg / "sneaky_tools"
        inner.mkdir(parents=True)
        (inner / "__init__.py").write_text("")
        (inner / "routes.py").write_text(
            "from celerp.session_gate import require_session_token\n"
            "def setup_api_routes(app): pass\n"
        )
        (pkg / "__init__.py").write_text(
            "PLUGIN_MANIFEST = {'name': 'sneaky-tools', 'version': '1.0', "
            "'api_routes': 'sneaky_tools.routes'}\n"
        )
        result = _loader.load_all(tmp_path, {"sneaky-tools"})
        assert not any(m["name"] == "sneaky-tools" for m in result), (
            "third-party module in MODULE_DIR must still fail BSL checks"
        )

    def test_resolve_bundled_dirs_always_includes_default_bundled(self, monkeypatch):
        """Default bundled dir (relative to loader.py) is always in the result."""
        from celerp.modules import loader as _loader
        monkeypatch.delenv("CELERP_TRUSTED_MODULE_DIRS", raising=False)
        monkeypatch.delenv("MODULE_DIR", raising=False)
        result = _loader._resolve_bundled_dirs()
        default = (Path(_loader.__file__).resolve().parent.parent.parent / "default_modules").resolve()
        assert default in result

    def test_resolve_bundled_dirs_multiple_trusted_dirs(self, tmp_path, monkeypatch):
        """CELERP_TRUSTED_MODULE_DIRS can be comma-separated (multiple paths)."""
        from celerp.modules import loader as _loader
        dir_a = tmp_path / "src_a"
        dir_b = tmp_path / "src_b"
        dir_a.mkdir()
        dir_b.mkdir()
        monkeypatch.setenv("CELERP_TRUSTED_MODULE_DIRS", f"{dir_a},{dir_b}")
        result = _loader._resolve_bundled_dirs()
        assert dir_a.resolve() in result
        assert dir_b.resolve() in result

    def test_resolve_bundled_dirs_ignores_empty_entries(self, monkeypatch):
        """Empty/whitespace entries in CELERP_TRUSTED_MODULE_DIRS are ignored."""
        from celerp.modules import loader as _loader
        monkeypatch.setenv("CELERP_TRUSTED_MODULE_DIRS", " , , ")
        # Should not raise, should not include empty Paths
        result = _loader._resolve_bundled_dirs()
        assert isinstance(result, tuple)

    def test_celerp_ai_bundled_dir_is_trusted_by_default(self):
        """celerp-ai's actual bundled directory must be in _BUNDLED_MODULES_DIRS.

        This confirms the fix resolves the Electron trust issue for the real
        celerp-ai module when loaded from the seeded MODULE_DIR.
        """
        from celerp.modules import loader as _loader
        real_default_modules = (
            Path(_loader.__file__).resolve().parent.parent.parent / "default_modules"
        )
        if not real_default_modules.exists():
            import pytest as _pytest
            _pytest.skip("default_modules not on this path")
        # The bundled default_modules dir must be trusted
        assert real_default_modules.resolve() in _loader._BUNDLED_MODULES_DIRS

    def test_ast_scan_catches_bsl_import_in_routes_file(self, tmp_path):
        """_ast_scan_module_file correctly detects BSL imports in route files."""
        from celerp.modules.loader import _ast_scan_module_file
        # _ast_scan_module_file resolves 'scan_test.routes' relative to pkg_path:
        # parts = ['scan_test', 'routes'] → pkg_path / 'scan_test' / 'routes.py'
        pkg = tmp_path / "scan-test"
        inner = pkg / "scan_test"
        inner.mkdir(parents=True)
        (inner / "routes.py").write_text(
            "from celerp.session_gate import require_session_token\n"
            "from celerp.ai.quota import check_ai_quota\n"
        )
        violations = _ast_scan_module_file(pkg, "scan_test.routes")
        assert "celerp.session_gate" in violations
        assert "celerp.ai.quota" in violations

    def test_ast_scan_catches_lazy_bsl_import_inside_function(self, tmp_path):
        """AST scan catches BSL imports nested inside function bodies (lazy imports)."""
        from celerp.modules.loader import _ast_scan_module_file
        pkg = tmp_path / "lazy-test"
        inner = pkg / "lazy_test"
        inner.mkdir(parents=True)
        (inner / "routes.py").write_text(
            "def my_endpoint():\n"
            "    from celerp.gateway.client import get_client\n"
            "    return get_client()\n"
        )
        violations = _ast_scan_module_file(pkg, "lazy_test.routes")
        assert "celerp.gateway" in violations, (
            "AST scan must catch lazy imports inside function bodies"
        )

    def test_ast_scan_follows_indirection_into_sibling_file(self, tmp_path):
        """The scan follows local imports transitively: a protected import in a
        helper file the entry module pulls in is still detected."""
        from celerp.modules.loader import _ast_scan_module_file
        pkg = tmp_path / "indir-test"
        inner = pkg / "indir_test"
        inner.mkdir(parents=True)
        (inner / "routes.py").write_text("from .impl import setup_api_routes\n")
        (inner / "impl.py").write_text(
            "import celerp.session_gate\n"
            "def setup_api_routes(app):\n    pass\n"
        )
        violations = _ast_scan_module_file(pkg, "indir_test.routes")
        assert "celerp.session_gate" in violations

    def test_ast_scan_follows_absolute_intra_package_indirection(self, tmp_path):
        """Same, via an absolute intra-package import (from pkg.impl import ...)."""
        from celerp.modules.loader import _ast_scan_module_file
        pkg = tmp_path / "abs-test"
        inner = pkg / "abs_test"
        inner.mkdir(parents=True)
        (inner / "routes.py").write_text("from abs_test import helper\n")
        (inner / "helper.py").write_text("from celerp.ai.quota import check\n")
        violations = _ast_scan_module_file(pkg, "abs_test.routes")
        assert "celerp.ai.quota" in violations

    def test_ast_scan_catches_dynamic_importlib(self, tmp_path):
        """A dynamic importlib.import_module(...) with a string-literal target is
        flagged (a plain Import/ImportFrom walk would not see a Call)."""
        from celerp.modules.loader import _ast_scan_module_file
        pkg = tmp_path / "dyn-test"
        inner = pkg / "dyn_test"
        inner.mkdir(parents=True)
        (inner / "routes.py").write_text(
            "import importlib\n"
            "def go():\n"
            "    m = importlib.import_module('celerp.ai.quota')\n"
            "    return m\n"
        )
        violations = _ast_scan_module_file(pkg, "dyn_test.routes")
        assert "celerp.ai.quota" in violations

    def test_ast_scan_catches_dunder_import(self, tmp_path):
        """__import__('celerp.session_gate') is likewise flagged."""
        from celerp.modules.loader import _ast_scan_module_file
        pkg = tmp_path / "dunder-test"
        inner = pkg / "dunder_test"
        inner.mkdir(parents=True)
        (inner / "routes.py").write_text("x = __import__('celerp.session_gate')\n")
        violations = _ast_scan_module_file(pkg, "dunder_test.routes")
        assert "celerp.session_gate" in violations

    def test_ast_scan_clean_transitive_no_false_positive(self, tmp_path):
        """A clean module that imports local + stdlib helpers must NOT be flagged
        (guards against the transitive walk over-reporting)."""
        from celerp.modules.loader import _ast_scan_module_file
        pkg = tmp_path / "clean-test"
        inner = pkg / "clean_test"
        inner.mkdir(parents=True)
        (inner / "routes.py").write_text(
            "import os\n"
            "from .helper import util\n"
            "from celerp.modules.api import public_api\n"   # PUBLIC api is allowed
        )
        (inner / "helper.py").write_text("import json\ndef util():\n    return json.dumps({})\n")
        violations = _ast_scan_module_file(pkg, "clean_test.routes")
        assert violations == set()

    def test_bundled_first_party_modules_load_with_bsl_imports(self):
        """A first-party module that imports BSL internals must load from the real default_modules
        directory (trusted=True, no AST scan). celerp-admin is the regression subject: it imports
        kernel internals (celerp.gateway.client) yet must be trusted when loaded from default_modules.

        celerp-ai/backup/connectors are NOT pluggable modules - they are folded into the proprietary
        core (wired directly in celerp/main.py and ui/app.py) and the loader explicitly skips them, so
        this test also asserts they are NOT loaded as modules.

        This is the full end-to-end regression test for the Electron trust bug.
        """
        real_dir = Path(__file__).parent.parent.parent / "default_modules"
        if not real_dir.exists():
            import pytest as _pytest
            _pytest.skip("default_modules not available")
        from celerp.modules import loader as _loader, slots as _slots
        _loader._loaded.clear()
        _slots.clear()
        with _preserved_sys_modules([
            "celerp_ai", "celerp_connectors", "celerp_backup", "celerp_admin",
            "celerp_inventory", "celerp_contacts", "celerp_docs",
        ]):
            try:
                # celerp-connectors depends on celerp-inventory + celerp-docs; include both
                result = _loader.load_all(
                    real_dir,
                    {
                        "celerp-ai", "celerp-connectors", "celerp-backup", "celerp-admin",
                        "celerp-inventory", "celerp-contacts", "celerp-docs",
                    },
                )
                loaded_names = {m["name"] for m in result}
                # celerp-admin imports kernel/BSL internals yet must be trusted when bundled (the trust bug).
                assert "celerp-admin" in loaded_names, (
                    "celerp-admin failed to load from default_modules - "
                    "it imports BSL internals and is being wrongly treated as untrusted. "
                    "This is the Electron trust bug."
                )
                # The proprietary cloud components are folded into core and must NOT load as pluggable
                # modules (the loader skips them; they are direct-wired in celerp/main.py and ui/app.py).
                for folded in ("celerp-ai", "celerp-backup", "celerp-connectors"):
                    assert folded not in loaded_names, (
                        f"{folded} is core-folded and must not be loaded by the module loader"
                    )
            finally:
                _loader._loaded.clear()
                _slots.clear()


# ── Content-identity first-party trust ────────────────────────────────────────
# Trust is granted by content digest against a committed lock, never by folder
# name or location. These cover the digest, the predicate, the lock loader and
# its failure paths, the reader migration, and the pycache purge.

import hashlib
import json
import logging

from celerp.modules import loader as _fpt_loader


def _copy_default(name: str, dest: Path) -> Path:
    """Byte-copy a shipped default module into dest/name (the electron reseed
    shape). __pycache__/*.pyc are skipped so the copy is a clean source tree."""
    import shutil
    src = _fpt_loader._BUNDLED_MODULES_DIRS[0] / name
    out = dest / name
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return out


class TestModuleContentDigest:
    def test_module_content_digest_is_deterministic(self, tmp_path):
        pkg = _make_module(tmp_path, "det-mod", '{"name": "det-mod", "version": "1.0"}')
        assert _fpt_loader.module_content_digest(pkg) == _fpt_loader.module_content_digest(pkg)

    def test_digest_excludes_pycache_pyc_meta_and_premium(self, tmp_path):
        from celerp.modules.meta import META_FILENAME
        from celerp.modules.importer import PREMIUM_MARKER
        pkg = _make_module(tmp_path, "ex-mod", '{"name": "ex-mod", "version": "1.0"}')
        before = _fpt_loader.module_content_digest(pkg)
        (pkg / "__pycache__").mkdir()
        (pkg / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00\x01")
        (pkg / "stale.pyc").write_bytes(b"\x00\x02")
        (pkg / META_FILENAME).write_text('{"source": "default"}')
        (pkg / PREMIUM_MARKER).write_text("")
        assert _fpt_loader.module_content_digest(pkg) == before

    def test_digest_changes_when_a_source_file_changes(self, tmp_path):
        pkg = _make_module(tmp_path, "chg-mod", '{"name": "chg-mod", "version": "1.0"}')
        before = _fpt_loader.module_content_digest(pkg)
        (pkg / "__init__.py").write_text('PLUGIN_MANIFEST = {"name": "chg-mod", "version": "2.0"}')
        assert _fpt_loader.module_content_digest(pkg) != before

    def test_digest_rejects_symlink_escape(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")
        pkg = _make_module(tmp_path, "sym-mod", '{"name": "sym-mod", "version": "1.0"}')
        clean = _fpt_loader.module_content_digest(pkg)
        try:
            (pkg / "leak.txt").symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        after = _fpt_loader.module_content_digest(pkg)
        # The escaping symlink's target bytes never enter the hash: either the
        # digest is unchanged (symlink skipped) or None (rejected) - never a
        # value that inlined the secret.
        inlined = hashlib.sha256(b"TOP SECRET").hexdigest()
        assert after in (clean, None)
        assert after is None or inlined not in after

    def test_digest_returns_none_on_unreadable_file(self, tmp_path, monkeypatch):
        pkg = _make_module(tmp_path, "unr-mod", '{"name": "unr-mod", "version": "1.0"}')
        real_read = Path.read_bytes

        def boom(self, *a, **k):
            if self.name == "__init__.py":
                raise OSError("unreadable")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_bytes", boom)
        assert _fpt_loader.module_content_digest(pkg) is None


class TestIsFirstParty:
    def test_impostor_named_default_in_writable_dir_is_not_trusted(self, tmp_path, monkeypatch):
        # A folder named after a real default, wrong content, whose route file
        # imports a protected BSL internal. Under content-identity it is NOT
        # first-party (digest mismatch), so the BSL scan runs and rejects it.
        # (Under the old name-trust it would have loaded clean.)
        monkeypatch.setattr(_fpt_loader, "_first_party_lock",
                            lambda: {"celerp-manufacturing": "deadbeef"})
        pkg = tmp_path / "celerp-manufacturing"
        inner = pkg / "celerp_manufacturing"
        inner.mkdir(parents=True)
        (inner / "__init__.py").write_text("")
        (inner / "routes.py").write_text(
            "from celerp.session_gate import require_session_token\n"
            "def setup_api_routes(app): pass\n")
        (pkg / "__init__.py").write_text(
            "PLUGIN_MANIFEST = {'name': 'celerp-manufacturing', 'version': '1.0', "
            "'api_routes': 'celerp_manufacturing.routes'}\n")
        result = load_all(tmp_path, {"celerp-manufacturing"})
        assert not any(m["name"] == "celerp-manufacturing" for m in result)
        assert "celerp-manufacturing" in load_errors()

    def test_seeded_default_with_pycache_and_markers_is_still_first_party(self, tmp_path):
        # THE load-bearing upgrade test: a byte-copy of a shipped default plus the
        # runtime files the electron reseed / first boot add must stay trusted.
        from celerp.modules.meta import META_FILENAME
        from celerp.modules.importer import PREMIUM_MARKER
        pkg = _copy_default("celerp-labels", tmp_path)
        assert _fpt_loader.is_first_party(pkg) is True
        (pkg / "__pycache__").mkdir(exist_ok=True)
        (pkg / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"\x00")
        (pkg / META_FILENAME).write_text('{"source": "default"}')
        (pkg / PREMIUM_MARKER).write_text("")
        assert _fpt_loader.is_first_party(pkg) is True

    def test_modified_default_demotes_and_warns(self, tmp_path, caplog):
        pkg = _copy_default("celerp-labels", tmp_path)
        (pkg / "__init__.py").write_text(
            (pkg / "__init__.py").read_text() + "\n# tampered\n")
        with caplog.at_level(logging.WARNING, logger="celerp.modules.loader"):
            assert _fpt_loader.is_first_party(pkg) is False
        assert any("celerp-labels" in r.message for r in caplog.records)


class TestFirstPartyLock:
    def _point_lock(self, monkeypatch, tmp_path, content: str | None):
        lock = tmp_path / "first_party.lock.json"
        if content is not None:
            lock.write_text(content)
        monkeypatch.setattr(_fpt_loader, "_lock_path", lambda: lock)
        _fpt_loader._first_party_lock.cache_clear()

    def test_lock_missing_returns_empty_and_logs_error(self, tmp_path, monkeypatch, caplog):
        self._point_lock(monkeypatch, tmp_path, None)
        with caplog.at_level(logging.ERROR, logger="celerp.modules.loader"):
            assert _fpt_loader._first_party_lock() == {}
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        _fpt_loader._first_party_lock.cache_clear()

    def test_lock_empty_valid_logs_warning_not_error(self, tmp_path, monkeypatch, caplog):
        self._point_lock(monkeypatch, tmp_path, "{}")
        with caplog.at_level(logging.WARNING, logger="celerp.modules.loader"):
            assert _fpt_loader._first_party_lock() == {}
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert not any(r.levelno == logging.ERROR for r in caplog.records)
        _fpt_loader._first_party_lock.cache_clear()

    def test_lock_malformed_returns_empty_fail_closed(self, tmp_path, monkeypatch):
        for bad in ('["not", "a", "dict"]', '{"celerp-x": 123}', 'not json'):
            self._point_lock(monkeypatch, tmp_path, bad)
            assert _fpt_loader._first_party_lock() == {}
            _fpt_loader._first_party_lock.cache_clear()

    def test_lock_resolves_package_root_ignores_trusted_module_dirs(self, tmp_path, monkeypatch):
        # A rogue lock planted beside a CELERP_TRUSTED_MODULE_DIRS copy must NOT
        # be read: only the package-root lock is authoritative.
        rogue = tmp_path / "rogue"
        rogue.mkdir()
        (rogue / "first_party.lock.json").write_text('{"evil": "0"}')
        monkeypatch.setenv("CELERP_TRUSTED_MODULE_DIRS", str(rogue))
        _fpt_loader._first_party_lock.cache_clear()
        assert "evil" not in _fpt_loader._first_party_lock()
        _fpt_loader._first_party_lock.cache_clear()


class TestReaderMigrationAndPurge:
    def test_route_failure_load_and_continues_for_demoted_module(self):
        # A demoted (name in lock, content mismatch) module carries first_party
        # False on its manifest; a route failure is recorded and boot continues -
        # it is no longer treated as a boot-critical default just because its name
        # matches. The policy reads the manifest's own verdict, not the name.
        manifest = {"name": "celerp-manufacturing", "first_party": False}
        # Should not raise (would raise if treated as a genuine default).
        _fpt_loader._route_failure(manifest, "api route", RuntimeError("boom"))
        assert "celerp-manufacturing" in load_errors()

    def test_import_error_gate_uses_first_party_bit(self, tmp_path, monkeypatch):
        # A name-matching but content-mismatched module raising ModuleLoadError is
        # recorded, not re-raised to halt boot.
        monkeypatch.setattr(_fpt_loader, "_first_party_lock",
                            lambda: {"celerp-manufacturing": "deadbeef"})
        bad = tmp_path / "celerp-manufacturing"
        bad.mkdir()
        (bad / "__init__.py").write_text(
            'PLUGIN_MANIFEST = {"name": "celerp-manufacturing", "version": "1.0"}\n'
            'raise RuntimeError("import boom")')
        result = load_all(tmp_path, {"celerp-manufacturing"})
        assert result == []
        assert "celerp-manufacturing" in load_errors()

    def test_pycache_purged_before_load(self, tmp_path):
        pkg = _make_module(tmp_path, "pc-mod", '{"name": "pc-mod", "version": "1.0"}')
        cache = pkg / "__pycache__"
        cache.mkdir()
        (cache / "__init__.cpython-311.pyc").write_bytes(b"\x00tampered")
        _fpt_loader._purge_pycache(pkg)
        assert not cache.exists()

    def test_load_all_purges_pycache(self, tmp_path):
        # Import itself recompiles a fresh __init__ pyc, so __pycache__ reappears;
        # what must be gone is the stale junk pyc, purged before the import ran.
        pkg = _make_module(tmp_path, "pc2-mod", '{"name": "pc2-mod", "version": "1.0"}')
        cache = pkg / "__pycache__"
        cache.mkdir()
        (cache / "junk.pyc").write_bytes(b"\x00")
        load_all(tmp_path, {"pc2-mod"})
        assert not (cache / "junk.pyc").exists()


class TestResolveModulePath:
    def test_resolve_module_path_finds_module_in_non_first_entry(self, tmp_path, monkeypatch):
        first = tmp_path / "writable"
        first.mkdir()
        second = tmp_path / "bundled"
        second.mkdir()
        pkg = _make_module(second, "found-mod", '{"name": "found-mod", "version": "1.0"}')
        monkeypatch.setenv("MODULE_DIR", f"{first},{second}")
        resolved = _fpt_loader.resolve_module_path("found-mod")
        assert resolved is not None
        assert resolved.resolve() == pkg.resolve()

    def test_resolve_module_path_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MODULE_DIR", str(tmp_path))
        assert _fpt_loader.resolve_module_path("ghost") is None

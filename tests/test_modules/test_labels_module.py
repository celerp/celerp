# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Tests for default_modules/celerp-labels.

Covers:
- PLUGIN_MANIFEST structure and slot definitions
- Label service: render_label_text, render_label_pdf (stub path)
- API routes: CRUD + print endpoints (via TestClient)
- UI routes: page renders, unauthenticated redirect
- Migration file structure
- End-to-end load via module loader
"""
from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

LABELS_DIR = Path(__file__).parent.parent.parent / "default_modules" / "celerp-labels"
INVENTORY_DIR = Path(__file__).parent.parent.parent / "default_modules" / "celerp-inventory"


def _import_labels_pkg() -> ModuleType:
    """Import celerp-labels __init__ from the default_modules directory."""
    spec = importlib.util.spec_from_file_location(
        "celerp_labels_root",
        LABELS_DIR / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_service() -> ModuleType:
    """Import celerp_labels.service, adding default_modules to path first."""
    _add_to_path()
    import importlib as _il
    if "celerp_labels" in sys.modules:
        _il.invalidate_caches()
    # Force fresh import from correct path
    for key in list(sys.modules):
        if key.startswith("celerp_labels"):
            sys.modules.pop(key)
    return _il.import_module("celerp_labels.service")


def _add_to_path():
    src = str(LABELS_DIR)
    if src not in sys.path:
        sys.path.insert(0, src)


# ── Phase 3 Manifest tests ─────────────────────────────────────────────────────

class TestLabelsManifest:
    def test_manifest_exists(self):
        mod = _import_labels_pkg()
        assert hasattr(mod, "PLUGIN_MANIFEST")

    def test_required_fields(self):
        mod = _import_labels_pkg()
        m = mod.PLUGIN_MANIFEST
        assert m["name"] == "celerp-labels"
        assert m["version"]
        assert m["display_name"]
        assert m["description"]
        assert m["license"]
        assert m["author"]

    def test_route_pointers(self):
        mod = _import_labels_pkg()
        m = mod.PLUGIN_MANIFEST
        assert m["api_routes"] == "celerp_labels.routes"
        assert m["ui_routes"] == "celerp_labels.ui_routes"

    def test_slot_nav(self):
        mod = _import_labels_pkg()
        nav = mod.PLUGIN_MANIFEST["slots"]["nav"]
        assert nav["href"] == "/settings/labels"
        assert nav["label"] == "Labels"
        assert isinstance(nav["order"], int)

    def test_slot_bulk_action(self):
        mod = _import_labels_pkg()
        ba = mod.PLUGIN_MANIFEST["slots"]["bulk_action"]
        assert ba["form_action"] == "/labels/print-bulk"
        assert ba["label"] == "Print Labels"
        assert "icon" in ba

    def test_slot_item_action_removed(self):
        mod = _import_labels_pkg()
        assert mod.PLUGIN_MANIFEST["slots"]["item_action"] is None

    def test_slot_settings_tab(self):
        mod = _import_labels_pkg()
        st = mod.PLUGIN_MANIFEST["slots"]["settings_tab"]
        assert st["href"] == "/settings/labels"
        assert isinstance(st["order"], int)

    def test_migrations_pointer(self):
        mod = _import_labels_pkg()
        assert mod.PLUGIN_MANIFEST["migrations"] == "celerp_labels.migrations"

    def test_requires_list(self):
        mod = _import_labels_pkg()
        reqs = mod.PLUGIN_MANIFEST["requires"]
        assert isinstance(reqs, list)
        assert any("reportlab" in r for r in reqs)


# ── Label service tests ────────────────────────────────────────────────────────

class TestLabelService:
    def setup_method(self):
        _add_to_path()

    def _svc(self):
        return _import_service()

    def test_render_label_text_basic(self):
        svc = self._svc()
        items = [{"name": "Ruby", "sku": "R001", "barcode": "123"}]
        template = {"name": "Test", "fields": ["name", "sku", "barcode"], "copies": 1}
        text = svc.render_label_text(items, template)
        assert "Ruby" in text
        assert "R001" in text
        assert "123" in text

    def test_render_label_text_multiple_copies(self):
        svc = self._svc()
        items = [{"name": "Ruby", "sku": "R001"}]
        template = {"name": "T", "fields": ["name"], "copies": 3}
        text = svc.render_label_text(items, template)
        assert text.count("Ruby") == 3

    def test_render_label_text_multiple_items(self):
        svc = self._svc()
        items = [{"name": "Ruby"}, {"name": "Diamond"}]
        template = {"name": "T", "fields": ["name"], "copies": 1}
        text = svc.render_label_text(items, template)
        assert "Ruby" in text
        assert "Diamond" in text

    def test_render_label_text_empty_items(self):
        svc = self._svc()
        text = svc.render_label_text([], {"name": "T", "fields": ["name"], "copies": 1})
        assert text == ""

    def test_render_label_text_missing_field_graceful(self):
        svc = self._svc()
        items = [{"name": "Ruby"}]
        template = {"name": "T", "fields": ["name", "sku", "nonexistent"], "copies": 1}
        text = svc.render_label_text(items, template)
        assert "nonexistent:" in text  # Field listed, value empty

    def test_render_label_pdf_returns_bytes(self):
        svc = self._svc()
        items = [{"name": "Ruby", "sku": "R001"}]
        template = {"name": "T", "format": "40x30mm", "fields": ["name", "sku"], "copies": 1}
        pdf = svc.render_label_pdf(items, template)
        assert isinstance(pdf, bytes)
        assert len(pdf) > 0

    def test_render_label_pdf_starts_with_pdf_magic(self):
        svc = self._svc()
        items = [{"name": "Ruby", "sku": "R001"}]
        template = {"name": "T", "format": "40x30mm", "fields": ["name"], "copies": 1}
        pdf = svc.render_label_pdf(items, template)
        assert pdf[:4] == b"%PDF"

    def test_stub_pdf_when_reportlab_absent(self):
        svc = self._svc()
        # Force stub path by patching _REPORTLAB to False
        with patch.object(svc, "_REPORTLAB", False):
            items = [{"name": "Test", "sku": "T001"}]
            template = {"name": "T", "format": "40x30mm", "fields": ["name"], "copies": 1}
            pdf = svc._stub_pdf(items, template)
        assert isinstance(pdf, bytes)
        assert b"%PDF" in pdf

    def test_parse_size_known_formats(self):
        svc = self._svc()
        assert svc._parse_size("A4") == (210, 297)
        assert svc._parse_size("40x30mm") == (40, 30)
        assert svc._parse_size("letter") == (216, 279)

    def test_parse_size_custom_wh(self):
        svc = self._svc()
        w, h = svc._parse_size("60x40mm")
        assert w == 60.0
        assert h == 40.0

    def test_parse_size_unknown_falls_back(self):
        svc = self._svc()
        w, h = svc._parse_size("garbage")
        assert (w, h) == (40, 30)  # default fallback

    def test_render_all_standard_formats(self):
        """All standard formats should produce valid PDF bytes."""
        svc = self._svc()
        items = [{"name": "X"}]
        for fmt in ["A4", "A5", "letter", "40x30mm", "62x29mm", "100x50mm"]:
            template = {"name": "T", "format": fmt, "fields": ["name"], "copies": 1}
            pdf = svc.render_label_pdf(items, template)
            assert isinstance(pdf, bytes), f"format {fmt!r} did not return bytes"


# ── API routes tests ───────────────────────────────────────────────────────────

class TestLabelsLoaderIntegration:
    """Test that celerp-labels loads cleanly via the module loader."""

    @pytest.fixture(autouse=True)
    def clean_state(self):
        from celerp.modules import slots, loader
        slots.clear()
        loader._loaded.clear()
        yield
        slots.clear()
        loader._loaded.clear()
        for key in list(sys.modules):
            if key.startswith("celerp-labels") or key.startswith("celerp_labels"):
                sys.modules.pop(key)

    def test_labels_module_loads_via_loader(self, tmp_path):
        """Loader picks up celerp-labels and registers all 4 slots."""
        import shutil
        from celerp.modules.loader import load_all
        from celerp.modules.slots import get

        # Copy both celerp-labels and its dependency celerp-inventory into tmp module dir
        shutil.copytree(LABELS_DIR, tmp_path / "celerp-labels")
        shutil.copytree(INVENTORY_DIR, tmp_path / "celerp-inventory")

        loaded = load_all(str(tmp_path), {"celerp-labels", "celerp-inventory"})
        labels_manifests = [m for m in loaded if m["name"] == "celerp-labels"]
        assert len(labels_manifests) == 1
        assert labels_manifests[0]["name"] == "celerp-labels"

        # All slots registered
        assert len(get("nav")) >= 1
        assert any(s["href"] == "/settings/labels" for s in get("nav"))
        assert len(get("bulk_action")) >= 1
        assert any(s["form_action"] == "/labels/print-bulk" for s in get("bulk_action"))
        # item_action slot removed (set to None) — print is now inline in item detail
        assert len(get("settings_tab")) >= 1

    def test_labels_module_not_bsl_violation(self, tmp_path):
        """celerp-labels does not import any protected BSL internals."""
        import shutil
        from celerp.modules.loader import load_all

        shutil.copytree(LABELS_DIR, tmp_path / "celerp-labels")
        shutil.copytree(INVENTORY_DIR, tmp_path / "celerp-inventory")

        # Should load without raising BSL violation
        loaded = load_all(str(tmp_path), {"celerp-labels", "celerp-inventory"})
        labels_manifests = [m for m in loaded if m["name"] == "celerp-labels"]
        assert len(labels_manifests) == 1

    def test_labels_disabled_module_not_loaded(self, tmp_path):
        """Disabled celerp-labels is not loaded."""
        import shutil
        from celerp.modules.loader import load_all

        src = LABELS_DIR
        dst = tmp_path / "celerp-labels"
        shutil.copytree(src, dst)

        loaded = load_all(str(tmp_path), set())  # empty enabled set
        assert len(loaded) == 0


# ── Migration file tests ──────────────────────────────────────────────────────

class TestLabelsMigration:
    def test_migration_file_exists(self):
        mig_dir = LABELS_DIR / "celerp_labels" / "migrations"
        files = list(mig_dir.glob("*.py"))
        non_init = [f for f in files if f.name != "__init__.py"]
        assert len(non_init) >= 1, "Expected at least one migration file"

    def test_migration_has_revision(self):
        mig_dir = LABELS_DIR / "celerp_labels" / "migrations"
        for f in mig_dir.glob("*.py"):
            if f.name == "__init__.py":
                continue
            content = f.read_text()
            assert "revision" in content
            assert "upgrade" in content
            assert "downgrade" in content
            assert "label_templates" in content




# ── UI routes tests ───────────────────────────────────────────────────────────

def _import_ui_routes():
    """Import celerp_labels.ui_routes with correct sys.path."""
    _add_to_path()
    for key in list(sys.modules):
        if key.startswith("celerp_labels"):
            sys.modules.pop(key)
    import importlib as _il
    return _il.import_module("celerp_labels.ui_routes")


def _html(ft_el) -> str:
    """Convert a FastHTML FT element to HTML string."""
    from fasthtml.common import to_xml
    return to_xml(ft_el)


class TestLabelsUIRoutes:
    """Tests for the FastHTML label template editor UI routes."""

    @pytest.fixture()
    def mock_templates(self):
        return [
            {"id": "tmpl-1", "name": "Small Tag", "format": "40x30mm",
             "copies": 1, "fields": ["name", "sku", "barcode"]},
            {"id": "tmpl-2", "name": "Shelf Label", "format": "100x50mm",
             "copies": 2, "fields": [{"key": "name", "label": "Name"}, {"key": "price", "label": "Price"}]},
        ]

    # -- Component rendering tests

    def test_templates_list_renders_names(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._templates_list(mock_templates))
        assert "Small Tag" in html
        assert "Shelf Label" in html

    def test_templates_list_active_class(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._templates_list(mock_templates, active_id="tmpl-1"))
        assert "template-list-item--active" in html

    def test_templates_list_empty_state(self):
        ur = _import_ui_routes()
        html = _html(ur._templates_list([]))
        assert "No templates" in html

    def test_templates_list_has_create_form(self):
        ur = _import_ui_routes()
        html = _html(ur._templates_list([]))
        assert "/settings/labels" in html
        assert "hx-post" in html

    def test_templates_list_delete_buttons(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._templates_list(mock_templates))
        assert "hx-delete" in html
        assert "tmpl-1" in html
        assert "tmpl-2" in html

    def test_editor_panel_renders_meta_fields(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._editor_panel(mock_templates[0]))
        assert "Small Tag" in html
        assert "40x30mm" in html

    def test_editor_panel_renders_field_rows(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._editor_panel(mock_templates[0]))
        assert "name" in html
        assert "sku" in html
        assert "barcode" in html

    def test_editor_panel_field_dicts_normalized(self, mock_templates):
        """Template with list-of-dicts fields renders without error."""
        ur = _import_ui_routes()
        html = _html(ur._editor_panel(mock_templates[1]))
        assert "Name" in html
        assert "Price" in html

    def test_editor_panel_has_drag_handle(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._editor_panel(mock_templates[0]))
        assert "drag to reposition" in html

    def test_editor_panel_has_sortable_init_script(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._editor_panel(mock_templates[0]))
        assert "labelEditorUpdatePreview" in html

    def test_editor_panel_has_add_field_button(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._editor_panel(mock_templates[0]))
        assert "Add field" in html

    def test_editor_panel_form_targets_correct_id(self, mock_templates):
        ur = _import_ui_routes()
        t = mock_templates[0]
        html = _html(ur._editor_panel(t))
        assert f"/settings/labels/{t['id']}" in html

    def test_editor_panel_hx_put_method(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._editor_panel(mock_templates[0]))
        assert "hx-put" in html

    def test_label_settings_root_injects_css(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root(mock_templates))
        assert "label-settings-layout" in html
        assert "labels-css" in html

    def test_label_settings_root_sortable_cdn(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root(mock_templates))
        assert "label-settings-layout" in html

    def test_label_settings_root_flash_success(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root(mock_templates, flash="Saved!", flash_kind="success"))
        assert "Saved!" in html
        assert "success-banner" in html

    def test_label_settings_root_flash_error(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root(mock_templates, flash="Oops", flash_kind="error"))
        assert "Oops" in html
        assert "error-banner" in html

    def test_empty_editor_placeholder(self):
        ur = _import_ui_routes()
        html = _html(ur._empty_editor())
        assert "label-editor-panel" in html
        assert "Select a template" in html

    def test_labels_list_page_with_templates(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root(mock_templates))
        assert "Small Tag" in html
        assert "Shelf Label" in html
        assert "template-list" in html

    def test_labels_list_page_empty(self):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root([]))
        assert "No templates" in html

    def test_labels_list_page_has_breadcrumbs(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root(mock_templates))
        assert "breadcrumb" in html
        assert "/inventory" in html

    def test_labels_list_page_empty_has_settings_link(self):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root([]))
        assert "/settings/labels" in html
        assert "+ Add" in html

    def test_labels_list_page_has_manage_link(self, mock_templates):
        ur = _import_ui_routes()
        html = _html(ur._label_settings_root(mock_templates))
        assert "breadcrumb" in html
        assert "/inventory" in html
        assert "/settings/inventory" in html

    # -- _extract_fields_from_form tests

    def test_extract_fields_basic(self):
        ur = _import_ui_routes()
        form_items = [
            ("fields[0][key]", "sku"),
            ("fields[0][label]", "SKU"),
            ("fields[1][key]", "name"),
            ("fields[1][label]", "Name"),
        ]
        class FakeForm:
            def multi_items(self): return form_items
        result = ur._extract_fields_from_form(FakeForm())
        assert result == [{"key": "sku", "label": "SKU", "type": "text"}, {"key": "name", "label": "Name", "type": "text"}]

    def test_extract_fields_drops_empty_keys(self):
        ur = _import_ui_routes()
        form_items = [
            ("fields[0][key]", "sku"),
            ("fields[0][label]", "SKU"),
            ("fields[1][key]", ""),      # empty — dropped
            ("fields[1][label]", "X"),
        ]
        class FakeForm:
            def multi_items(self): return form_items
        result = ur._extract_fields_from_form(FakeForm())
        assert len(result) == 1 and result[0]["key"] == "sku"

    def test_extract_fields_label_defaults_to_key(self):
        ur = _import_ui_routes()
        form_items = [("fields[0][key]", "barcode")]
        class FakeForm:
            def multi_items(self): return form_items
        result = ur._extract_fields_from_form(FakeForm())
        assert result[0]["label"] == "barcode"

    def test_extract_fields_order_preserved(self):
        ur = _import_ui_routes()
        form_items = [
            ("fields[2][key]", "c"),
            ("fields[0][key]", "a"),
            ("fields[1][key]", "b"),
        ]
        class FakeForm:
            def multi_items(self): return form_items
        result = ur._extract_fields_from_form(FakeForm())
        assert [r["key"] for r in result] == ["a", "b", "c"]

    # -- Route registration + unauthenticated redirect tests

    def _bare_app(self):
        """App with UI routes but NO auth cookies."""
        _add_to_path()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        ur = _import_ui_routes()
        ur.setup_ui_routes(app)
        return TestClient(app, follow_redirects=False)

    def test_labels_page_unauthenticated_redirects(self):
        c = self._bare_app()
        r = c.get("/labels")
        assert r.status_code == 302
        assert "/settings/labels" in r.headers["location"]

    def test_print_preview_route_removed(self):
        c = self._bare_app()
        r = c.get("/labels/print/entity-123")
        assert r.status_code == 404

    def test_settings_labels_unauthenticated_redirects(self):
        c = self._bare_app()
        r = c.get("/settings/labels")
        assert r.status_code == 302

    def test_settings_labels_edit_unauthenticated_redirects(self):
        c = self._bare_app()
        r = c.get("/settings/labels/some-id")
        assert r.status_code == 302

    def test_setup_registers_labels_route(self):
        _add_to_path()
        from fastapi import FastAPI
        app = FastAPI()
        ur = _import_ui_routes()
        ur.setup_ui_routes(app)
        paths = {r.path for r in app.routes}
        assert "/labels" in paths

    def test_setup_registers_settings_labels_route(self):
        _add_to_path()
        from fastapi import FastAPI
        app = FastAPI()
        ur = _import_ui_routes()
        ur.setup_ui_routes(app)
        paths = {r.path for r in app.routes}
        assert "/settings/labels" in paths

    def test_setup_registers_settings_labels_edit_route(self):
        _add_to_path()
        from fastapi import FastAPI
        app = FastAPI()
        ur = _import_ui_routes()
        ur.setup_ui_routes(app)
        paths = {r.path for r in app.routes}
        assert "/settings/labels/{tmpl_id}" in paths

    def test_setup_does_not_register_print_preview_route(self):
        _add_to_path()
        from fastapi import FastAPI
        app = FastAPI()
        ur = _import_ui_routes()
        ur.setup_ui_routes(app)
        paths = {r.path for r in app.routes}
        assert "/labels/print/{entity_id:path}" not in paths

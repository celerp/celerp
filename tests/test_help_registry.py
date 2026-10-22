"""Help registry: help dirs from every installed module's manifest, one index swapped whole."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from celerp.modules import loader


def _module(root: Path, name: str, help_entry) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True)
    manifest = {"name": name, "display_name": name.title(), "version": "1.0.0"}
    if help_entry is not None:
        manifest["help"] = help_entry
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    return pkg


def _write_page(help_dir: Path, page_id: str, title: str = "A page") -> None:
    help_dir.mkdir(parents=True, exist_ok=True)
    page = {"id": page_id, "title": title, "summary": "s", "keywords": [], "routes": ["/inventory"],
            "permission": None, "order": 1, "related": [],
            "steps": [{"text": "Do it.", "labels": {}, "image": None,
                       "capture": {"route": "/inventory", "selector": "main", "actions": []}}]}
    (help_dir / f"{page_id.split('.', 1)[1]}.json").write_text(json.dumps(page))


@pytest.fixture
def module_root(tmp_path, monkeypatch):
    root = tmp_path / "modules"
    root.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(root))
    return root


def test_installed_help_dirs_includes_disabled_modules(module_root):
    pkg = _module(module_root, "acme-widgets", {"dir": "acme_widgets/help"})
    (pkg / "acme_widgets" / "help").mkdir(parents=True)
    # Never loaded or enabled: the help dir is still found from the manifest alone.
    sources = {s.name: s for s in loader.installed_help_dirs()}
    src = sources["acme-widgets"]
    assert src.help_dir == pkg / "acme_widgets" / "help"
    assert src.display_name == "Acme-Widgets"
    assert src.first_party is False


def test_help_dir_outside_package_is_skipped(module_root, caplog):
    _module(module_root, "acme-escape", {"dir": "../../etc"})
    _module(module_root, "acme-notdict", "help")
    _module(module_root, "acme-nodir", {"path": "help"})
    with caplog.at_level(logging.ERROR, logger="celerp.modules.loader"):
        names = {s.name for s in loader.installed_help_dirs()}
    assert not names & {"acme-escape", "acme-notdict", "acme-nodir"}
    logged = caplog.text
    assert "acme-escape" in logged and "acme-notdict" in logged and "acme-nodir" in logged


def _source(name, help_dir, first_party):
    return loader.HelpSource(name=name, display_name=name, first_party=first_party, help_dir=help_dir)


def test_duplicate_page_id_first_source_wins(tmp_path, monkeypatch, caplog):
    from celerp.help import registry

    core = tmp_path / "core"
    _write_page(core, "core.start", "Core wins")
    first = tmp_path / "first"
    _write_page(first, "inventory.add", "First-party wins")
    third = tmp_path / "third"
    _write_page(third, "inventory.add", "Third-party loses")
    # A third-party module named like a first-party one cannot shadow core either.
    _write_page(third / "c", "core.start", "Never")
    monkeypatch.setattr(registry, "CORE_HELP_DIR", core)
    monkeypatch.setattr(registry, "installed_help_dirs", lambda: [
        _source("celerp-inventory", third, False),
        _source("celerp-inventory", first, True),
        _source("celerp-core", third / "c", False),
    ])
    registry.reset()
    with caplog.at_level(logging.WARNING, logger="celerp.help.registry"):
        index = registry.get_index()
    assert index.pages["core.start"].title == "Core wins"
    assert index.pages["inventory.add"].title == "First-party wins"
    assert index.pages["inventory.add"].source == "first_party"
    assert "inventory.add" in caplog.text
    registry.reset()


def test_invalid_third_party_page_logged_and_skipped(tmp_path, monkeypatch, caplog):
    from celerp.help import registry

    third = tmp_path / "third"
    _write_page(third, "acme.good", "Good page")
    (third / "bad.json").write_text("{not json")
    (third / "worse.json").write_text(json.dumps({"id": "acme.worse", "title": "x"}))
    monkeypatch.setattr(registry, "CORE_HELP_DIR", tmp_path / "none")
    monkeypatch.setattr(registry, "installed_help_dirs", lambda: [_source("acme", third, False)])
    registry.reset()
    with caplog.at_level(logging.WARNING, logger="celerp.help.registry"):
        index = registry.get_index()
    assert set(index.pages) == {"acme.good"}
    assert index.pages["acme.good"].source == "third_party"
    assert "bad.json" in caplog.text and "worse.json" in caplog.text
    registry.reset()


def test_registry_reset_on_load_all(monkeypatch):
    """load_all drops the built index beside its catalog reset, so the next read
    rebuilds from the modules now installed."""
    from celerp.help import registry

    resets: list[bool] = []
    monkeypatch.setattr(registry, "reset", lambda: resets.append(True))
    monkeypatch.setattr(loader, "_loaded", [])
    monkeypatch.setattr(loader, "_load_errors", {})
    monkeypatch.setattr(loader, "_admitted", {})
    monkeypatch.setattr(loader, "_module_routes", {})
    monkeypatch.setattr(loader, "_module_dirs", loader._module_dirs)
    monkeypatch.setattr("ui.i18n.clear_registry", lambda: None)
    loader.load_all(loader._BUNDLED_MODULES_DIRS[0], set(),
                    admission=loader.Admission([], {}))
    assert resets == [True]

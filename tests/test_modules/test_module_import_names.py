# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A module may only use package names nothing else already answers to.

A module named after a standard library module, Celerp's own packages or an
installed package is refused before any of its code runs, and the package that
name already means stays in place. The same goes for the package a module's
routes, slots or migrations name.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from celerp.modules import loader, slots

_TAKEN = ["json", "celerp", "ui", "httpx", "docker"]


@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    base = tmp_path / "modules"
    base.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(base))
    before_path, before_mods = list(sys.path), dict(sys.modules)
    slots.clear()
    yield base
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    sys.path[:] = before_path
    for key in set(sys.modules) - set(before_mods):
        sys.modules.pop(key, None)
    sys.modules.update(before_mods)


def _write(base: Path, folder: str, manifest: dict, files: dict[str, str] | None = None) -> None:
    pkg = base / folder
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\nLOADED = True\n")
    for rel, body in (files or {}).items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_text(body)


@pytest.mark.parametrize("name", _TAKEN)
def test_module_named_after_a_package_already_in_use_is_refused(module_dir, name):
    _write(module_dir, name, {"name": name, "version": "1.0.0"})
    before = sys.modules.get(name)

    admission = loader.admit_modules(str(module_dir), {name})
    assert admission.admitted == []
    assert "already" in admission.refused[name], admission.refused

    loader.load_all(str(module_dir), {name})
    assert not loader.is_running(name)
    assert sys.modules.get(name) is before


@pytest.mark.parametrize("inner", ["json", "celerp", "ui", "httpx", "celerp_inventory"])
def test_module_whose_routes_name_a_package_already_in_use_is_refused(module_dir, inner):
    name = f"acme-{uuid.uuid4().hex[:8]}"
    _write(module_dir, name, {"name": name, "version": "1.0.0", "api_routes": f"{inner}.acme_api"},
           {f"{inner}/__init__.py": "", f"{inner}/acme_api.py": "def setup_api_routes(app):\n    pass\n"})

    admission = loader.admit_modules(str(module_dir), {name})
    assert admission.admitted == []
    assert inner in admission.refused[name]


def test_module_with_its_own_package_names_is_admitted(module_dir):
    name, inner = f"acme-{uuid.uuid4().hex[:8]}", f"acme_{uuid.uuid4().hex[:8]}"
    _write(module_dir, name, {"name": name, "version": "1.0.0", "api_routes": f"{inner}.api"},
           {f"{inner}/__init__.py": "", f"{inner}/api.py": "def setup_api_routes(app):\n    pass\n"})

    admission = loader.admit_modules(str(module_dir), {name})
    assert [m.name for m in admission.admitted] == [name]
    loader.load_all(str(module_dir), {name}, admission=admission)
    assert loader.is_running(name), loader.load_errors()


def test_loading_never_replaces_a_package_already_imported(module_dir):
    """Even a module admitted on another pass cannot replace what is imported."""
    _write(module_dir, "json", {"name": "json", "version": "1.0.0"})
    real = sys.modules["json"]
    with pytest.raises(loader.ModuleLoadError):
        loader._load_one(module_dir / "json", "json", trusted=False,
                         declared={"name": "json", "version": "1.0.0", "slots": {}, "depends_on": []})
    assert sys.modules["json"] is real

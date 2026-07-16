# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Unit tests for celerp.modules.importer — the single validation + install
path for Import Module uploads and (later) marketplace downloads.

These tests double as the marketplace installer tests: same function.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from celerp.modules.importer import (
    MAX_ARCHIVE_BYTES,
    ModuleImportError,
    install_from_folder,
    install_from_zip,
)

MANIFEST = '''PLUGIN_MANIFEST = {
    "name": "my-module",
    "version": "1.0.0",
    "display_name": "My Module",
    "author": "Test",
}
'''


@pytest.fixture()
def module_dir(tmp_path, monkeypatch):
    d = tmp_path / "modules"
    d.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(d))
    return d


def _zip_bytes(files: dict[str, str], root: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr((root + name) if root else name, content)
    return buf.getvalue()


# ── zip: happy paths ───────────────────────────────────────────────────────────

def test_flat_zip_installs(module_dir):
    info = install_from_zip(_zip_bytes({"__init__.py": MANIFEST, "routes.py": "x = 1"}))
    assert info["name"] == "my-module"
    assert (module_dir / "my-module" / "__init__.py").exists()
    assert (module_dir / "my-module" / "routes.py").exists()


def test_nested_zip_installs_under_manifest_name(module_dir):
    # GitHub archives wrap in a folder whose name never matches the module name.
    data = _zip_bytes({"__init__.py": MANIFEST}, root="repo-name-abc123/")
    info = install_from_zip(data)
    assert info["name"] == "my-module"
    assert (module_dir / "my-module" / "__init__.py").exists()


# ── zip: refusals ──────────────────────────────────────────────────────────────

def test_zip_slip_refused(module_dir):
    data = _zip_bytes({"__init__.py": MANIFEST, "../evil.py": "boom"})
    with pytest.raises(ModuleImportError, match="unsafe paths"):
        install_from_zip(data)
    assert not (module_dir.parent / "evil.py").exists()


def test_symlink_entry_refused(module_dir):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("__init__.py", MANIFEST)
        info = zipfile.ZipInfo("link.py")
        info.external_attr = (0o120777 << 16)  # symlink mode
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ModuleImportError, match="symlink"):
        install_from_zip(buf.getvalue())


def test_oversize_archive_refused(module_dir):
    with pytest.raises(ModuleImportError, match="too large"):
        install_from_zip(b"x" * (MAX_ARCHIVE_BYTES + 1))


def test_not_a_zip_refused(module_dir):
    with pytest.raises(ModuleImportError, match="not a valid zip"):
        install_from_zip(b"definitely not a zip")


def test_missing_manifest_refused(module_dir):
    data = _zip_bytes({"__init__.py": "x = 1"})
    with pytest.raises(ModuleImportError, match="PLUGIN_MANIFEST"):
        install_from_zip(data)


def test_missing_init_refused(module_dir):
    data = _zip_bytes({"readme.md": "hello"})
    with pytest.raises(ModuleImportError, match="__init__.py"):
        install_from_zip(data)


def test_reserved_prefix_refused(module_dir):
    manifest = MANIFEST.replace("my-module", "celerp-sneaky")
    with pytest.raises(ModuleImportError, match="reserved"):
        install_from_zip(_zip_bytes({"__init__.py": manifest}))


def test_bad_name_chars_refused(module_dir):
    manifest = MANIFEST.replace("my-module", "my module!")
    with pytest.raises(ModuleImportError, match="letters, digits"):
        install_from_zip(_zip_bytes({"__init__.py": manifest}))


def test_collision_refused(module_dir):
    data = _zip_bytes({"__init__.py": MANIFEST})
    install_from_zip(data)
    with pytest.raises(ModuleImportError, match="already exists"):
        install_from_zip(data)


def test_non_literal_manifest_refused(module_dir):
    bad = "PLUGIN_MANIFEST = {'name': open('/etc/passwd').read()}"
    with pytest.raises(ModuleImportError, match="literal"):
        install_from_zip(_zip_bytes({"__init__.py": bad}))


def test_min_celerp_version_gate(module_dir, monkeypatch):
    import celerp
    monkeypatch.setattr(celerp, "__version__", "1.2.0", raising=False)
    manifest = MANIFEST.rstrip()[:-2] + '    "min_celerp_version": "9.9.9",\n}\n'
    with pytest.raises(ModuleImportError, match="requires Celerp 9.9.9"):
        install_from_zip(_zip_bytes({"__init__.py": manifest}))


# ── folder entrypoint ──────────────────────────────────────────────────────────

def test_folder_installs(module_dir, tmp_path):
    src = tmp_path / "src-module"
    src.mkdir()
    (src / "__init__.py").write_text(MANIFEST)
    (src / "routes.py").write_text("x = 1")
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("junk")
    info = install_from_folder(src)
    assert info["name"] == "my-module"
    assert (module_dir / "my-module" / "routes.py").exists()
    assert not (module_dir / "my-module" / ".git").exists()  # ignored


def test_folder_symlink_refused(module_dir, tmp_path):
    src = tmp_path / "src-module"
    src.mkdir()
    (src / "__init__.py").write_text(MANIFEST)
    os.symlink("/etc/passwd", src / "evil")
    with pytest.raises(ModuleImportError, match="symlink"):
        install_from_folder(src)


def test_folder_not_a_dir_refused(module_dir, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ModuleImportError, match="not a folder"):
        install_from_folder(f)


def test_no_module_dir_configured(monkeypatch):
    monkeypatch.setenv("MODULE_DIR", "")
    with pytest.raises(ModuleImportError, match="no module directory"):
        install_from_zip(_zip_bytes({"__init__.py": MANIFEST}))

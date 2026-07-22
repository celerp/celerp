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


def test_repo_archive_with_module_in_subfolder_installs(module_dir):
    # A repo (like the module template) keeps the module in a subfolder beside a
    # README, lint, and tests. The importer finds the PLUGIN_MANIFEST folder and
    # installs only that, under the manifest name.
    data = _zip_bytes({
        "acme-maintenance/__init__.py": MANIFEST,
        "acme-maintenance/inner/__init__.py": "x = 1",
        "README.md": "how to use",
        "tests/test_it.py": "y = 1",
    }, root="celerp-module-template-abc123/")
    info = install_from_zip(data)
    assert info["name"] == "my-module"
    assert (module_dir / "my-module" / "__init__.py").exists()
    assert (module_dir / "my-module" / "inner" / "__init__.py").exists()
    # Only the module folder lands; the repo's README and tests do not.
    assert not (module_dir / "my-module" / "README.md").exists()


def test_repo_archive_with_two_modules_refused(module_dir):
    data = _zip_bytes({
        "mod-a/__init__.py": MANIFEST,
        "mod-b/__init__.py": MANIFEST.replace("my-module", "other-mod"),
    }, root="wrap/")
    with pytest.raises(ModuleImportError, match="more than one module"):
        install_from_zip(data)


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


# ── marketplace (official/premium) installs ───────────────────────────────────

OFFICIAL_MANIFEST = MANIFEST.replace("my-module", "celerp-warehousing")


def test_official_install_allows_celerp_prefix(module_dir):
    info = install_from_zip(
        _zip_bytes({"__init__.py": OFFICIAL_MANIFEST}), official=True)
    assert info["name"] == "celerp-warehousing"
    assert (module_dir / "celerp-warehousing" / "__init__.py").exists()


def test_official_install_requires_celerp_prefix(module_dir):
    # The relay says a module is official: a package NOT under celerp- must be
    # refused, so third-party packages can't ride the official install path.
    with pytest.raises(ModuleImportError, match="celerp-"):
        install_from_zip(_zip_bytes({"__init__.py": MANIFEST}), official=True)


def test_premium_install_writes_license_marker(module_dir):
    from celerp.modules.importer import PREMIUM_MARKER
    install_from_zip(
        _zip_bytes({"__init__.py": OFFICIAL_MANIFEST}), official=True, premium=True)
    assert (module_dir / "celerp-warehousing" / PREMIUM_MARKER).exists()


def test_free_install_writes_no_marker(module_dir):
    from celerp.modules.importer import PREMIUM_MARKER
    install_from_zip(_zip_bytes({"__init__.py": MANIFEST}))
    assert not (module_dir / "my-module" / PREMIUM_MARKER).exists()


# ── concurrency: landing dir must not collide across simultaneous installs ────

def test_concurrent_installs_of_same_slug_use_distinct_landing_dirs(module_dir):
    """os.getpid() is identical across threads in the same process, so two
    installs of the SAME slug racing through asyncio.to_thread used to share
    one landing dir - one call's cleanup/replace could clobber the other's
    in-flight copy. Capture the landing (copytree destination) each of two
    overlapping install_from_zip calls actually uses and assert they differ."""
    import shutil as _shutil
    import threading

    real_copytree = _shutil.copytree
    landings: list[Path] = []
    lock = threading.Lock()
    both_entered = threading.Barrier(2, timeout=5)

    def _capturing_copytree(src, dst, *a, **kw):
        with lock:
            landings.append(Path(dst))
        both_entered.wait()  # force real overlap between the two threads
        return real_copytree(src, dst, *a, **kw)

    manifest_a = MANIFEST.replace("my-module", "same-slug")
    manifest_b = manifest_a  # identical name -> identical target, same collision class

    results = {}
    errors = {}

    def _install(key, manifest):
        try:
            results[key] = install_from_zip(_zip_bytes({"__init__.py": manifest}))
        except Exception as exc:  # noqa: BLE001 - capture for assertion below
            errors[key] = exc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_shutil, "copytree", _capturing_copytree)
        t1 = threading.Thread(target=_install, args=("a", manifest_a))
        t2 = threading.Thread(target=_install, args=("b", manifest_b))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

    assert len(landings) == 2
    assert landings[0] != landings[1], "concurrent installs used the SAME landing dir"
    # Exactly one wins (the second hits the importer's own name collision,
    # which is correct/expected for two installs of the true same slug) - the
    # point under test is that neither corrupts the other's temp copy.
    assert len(results) + len(errors) == 2


def test_replace_onto_populated_target_reports_already_exists(module_dir, monkeypatch):
    """A concurrent install can land the target between _target_for()'s existence
    check and os.replace(). On Linux that surfaces as OSError(ENOTEMPTY), not
    FileExistsError - both must map to the same friendly 'already exists'
    message, never a raw errno string."""
    import errno as _errno
    import os as _os

    real_replace = _os.replace

    def _boom(src, dst, *a, **kw):
        # Simulate the TOCTOU race: the target got populated by another install.
        real_replace(src, dst, *a, **kw)  # let landing->target proceed once...
        raise OSError(_errno.ENOTEMPTY, "Directory not empty")

    monkeypatch.setattr(_os, "replace", _boom)
    with pytest.raises(ModuleImportError, match="already exists"):
        install_from_zip(_zip_bytes({"__init__.py": MANIFEST}))


# ── premium marker cannot be smuggled in from package contents ────────────────

def test_zip_with_premium_marker_entry_refused(module_dir):
    from celerp.modules.importer import PREMIUM_MARKER
    data = _zip_bytes({"__init__.py": MANIFEST, PREMIUM_MARKER: ""})
    with pytest.raises(ModuleImportError, match="reserved"):
        install_from_zip(data)
    assert not (module_dir / "my-module").exists()


def test_folder_with_premium_marker_file_refused(module_dir, tmp_path):
    from celerp.modules.importer import PREMIUM_MARKER
    src = tmp_path / "src-module"
    src.mkdir()
    (src / "__init__.py").write_text(MANIFEST)
    (src / PREMIUM_MARKER).write_text("")
    with pytest.raises(ModuleImportError, match="reserved"):
        install_from_folder(src)
    assert not (module_dir / "my-module").exists()


def test_premium_false_removes_any_marker_belt_and_suspenders(module_dir, tmp_path, monkeypatch):
    """Direct unit test of the _finish reconciliation, independent of the
    entrypoint-level refusals above: if a marker somehow reaches _finish with
    premium=False, it must not survive into the installed module."""
    from celerp.modules.importer import PREMIUM_MARKER, _finish

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / PREMIUM_MARKER).write_text("")
    manifest = {"name": "reconciled-mod", "version": "1.0.0"}
    _finish(staged, manifest, official=False, premium=False)
    assert not (module_dir / "reconciled-mod" / PREMIUM_MARKER).exists()

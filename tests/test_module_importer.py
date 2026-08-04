# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Unit tests for celerp.modules.importer - the single validation + install
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


# ── provenance sidecar (.celerp-meta.json) + directory removal ────────────────

def test_zip_install_writes_sidecar_with_source(module_dir):
    import json
    from celerp.modules.meta import META_FILENAME
    install_from_zip(_zip_bytes({"__init__.py": MANIFEST}), source="marketplace")
    sidecar = module_dir / "my-module" / META_FILENAME
    assert sidecar.exists()
    assert json.loads(sidecar.read_text())["source"] == "marketplace"


def test_folder_install_defaults_to_sideloaded_source(module_dir, tmp_path):
    import json
    from celerp.modules.meta import META_FILENAME
    src = tmp_path / "src-module"
    src.mkdir()
    (src / "__init__.py").write_text(MANIFEST)
    install_from_folder(src)
    sidecar = module_dir / "my-module" / META_FILENAME
    assert sidecar.exists()
    assert json.loads(sidecar.read_text())["source"] == "sideloaded"


def test_read_meta_returns_empty_on_missing_file(tmp_path):
    from celerp.modules.meta import read_meta
    assert read_meta(tmp_path) == {}


def test_read_meta_returns_empty_on_corrupt_file(tmp_path):
    from celerp.modules.meta import META_FILENAME, read_meta
    (tmp_path / META_FILENAME).write_text("{not json")
    assert read_meta(tmp_path) == {}


def test_remove_module_dir_removes_folder(module_dir):
    from celerp.modules.importer import remove_module_dir
    install_from_zip(_zip_bytes({"__init__.py": MANIFEST}))
    assert (module_dir / "my-module").exists()
    remove_module_dir("my-module")
    assert not (module_dir / "my-module").exists()


def test_remove_module_dir_raises_if_absent(module_dir):
    from celerp.modules.importer import remove_module_dir
    with pytest.raises(ModuleImportError, match="not installed"):
        remove_module_dir("never-installed")


def test_remove_module_dir_rejects_path_traversal_name(module_dir):
    from celerp.modules.importer import remove_module_dir
    sentinel = module_dir.parent / "sentinel.txt"
    sentinel.write_text("keep me")
    with pytest.raises(ModuleImportError):
        remove_module_dir("../../etc")
    # Nothing outside the module dir was touched.
    assert sentinel.exists()


def test_remove_module_dir_removes_stragglers_across_all_entries(tmp_path, monkeypatch):
    """A copy left in a later MODULE_DIR entry (e.g. one landed under an older
    folder layout) is removed along with the first copy - the scan searches all
    entries, so a surviving straggler would resurface as a sideload the moment
    the first copy is gone."""
    from celerp.modules.importer import remove_module_dir
    d1 = tmp_path / "modules"
    d2 = tmp_path / "default_modules"
    d1.mkdir()
    d2.mkdir()
    for base in (d1, d2):
        pkg = base / "my-module"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(MANIFEST)
    monkeypatch.setenv("MODULE_DIR", f"{d1},{d2}")
    remove_module_dir("my-module")
    assert not (d1 / "my-module").exists()
    assert not (d2 / "my-module").exists()


def test_remove_module_dir_never_touches_first_party_copy(tmp_path, monkeypatch):
    """Removing a name that also exists as a genuine bundled default (content
    matches the committed lock) deletes only the stale copy; the first-party
    copy is never touched."""
    import json

    from celerp.modules import loader as _loader
    from celerp.modules.importer import remove_module_dir
    d1 = tmp_path / "modules"
    d2 = tmp_path / "bundled"
    d1.mkdir()
    d2.mkdir()
    stale = d1 / "my-module"
    stale.mkdir()
    (stale / "__init__.py").write_text(MANIFEST + "\n# local edit\n")
    genuine = d2 / "my-module"
    genuine.mkdir()
    (genuine / "__init__.py").write_text(MANIFEST)
    lock = tmp_path / "first_party.lock.json"
    lock.write_text(json.dumps({"my-module": _loader.module_content_digest(genuine)}))
    monkeypatch.setattr(_loader, "_lock_path", lambda: lock)
    _loader._first_party_lock.cache_clear()
    monkeypatch.setenv("MODULE_DIR", f"{d1},{d2}")
    try:
        remove_module_dir("my-module")
    finally:
        _loader._first_party_lock.cache_clear()
    assert not stale.exists()   # stale copy removed
    assert genuine.exists()     # genuine bundled default untouched


def test_module_dir_refuses_bundled_target(monkeypatch, tmp_path):
    """An import must never land in a bundled/trusted module dir: a package
    written there would inherit first-party trust by name. Pointing MODULE_DIR at
    a bundled dir is refused with a clear error, not written into. The bundled set
    is monkeypatched to a tmp dir so the test never touches the real tree."""
    from celerp.modules import importer, loader
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(loader, "_BUNDLED_MODULES_DIRS", (bundled,))
    monkeypatch.setenv("MODULE_DIR", str(bundled))
    with pytest.raises(ModuleImportError, match="bundled"):
        importer._module_dir()


def test_install_into_bundled_dir_refused(monkeypatch, tmp_path):
    """The guard holds at the install entrypoint, before any bytes are written."""
    from celerp.modules import loader
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(loader, "_BUNDLED_MODULES_DIRS", (bundled,))
    monkeypatch.setenv("MODULE_DIR", str(bundled))
    with pytest.raises(ModuleImportError, match="bundled"):
        install_from_zip(_zip_bytes({"__init__.py": MANIFEST}))
    assert not (bundled / "my-module").exists()


def test_with_writable_module_dir_prepends_when_first_is_bundled(monkeypatch, tmp_path):
    """A dev/CLI MODULE_DIR whose first entry is the bundled default_modules/ tree
    is corrected: a writable data-dir drop-in is prepended so imports land there,
    with the bundled dir kept on the path for default discovery."""
    from celerp.modules import loader
    from celerp.modules.loader import _BUNDLED_MODULES_DIRS
    monkeypatch.setattr(loader, "writable_module_dir", lambda: tmp_path / "modules")
    bundled = str(_BUNDLED_MODULES_DIRS[0])
    parts = loader.with_writable_module_dir(bundled).split(",")
    assert parts[0] == str(tmp_path / "modules")
    assert bundled in parts


def test_with_writable_module_dir_leaves_safe_first_entry(tmp_path):
    """A MODULE_DIR whose first entry is already a writable, non-bundled dir (the
    normal test/CLI case) is returned unchanged."""
    from celerp.modules import loader
    safe = f"{tmp_path / 'mods'},/other"
    assert loader.with_writable_module_dir(safe) == safe


def test_with_writable_module_dir_empty_unchanged():
    """No module dir configured stays off - the helper never invents one."""
    from celerp.modules import loader
    assert loader.with_writable_module_dir("") == ""


# ── Copy-ignore is the shared digest-exclude set (DRY) ────────────────────────
# The importer's copytree ignore and the content digest's exclusion set are one
# source of truth: celerp.modules.loader._DIGEST_EXCLUDE_GLOBS.

def test_digest_exclude_globs_built_from_meta_and_premium_constants():
    from celerp.modules.loader import _DIGEST_EXCLUDE_GLOBS
    from celerp.modules.meta import META_FILENAME
    from celerp.modules.importer import PREMIUM_MARKER
    assert META_FILENAME in _DIGEST_EXCLUDE_GLOBS
    assert PREMIUM_MARKER in _DIGEST_EXCLUDE_GLOBS
    assert "__pycache__" in _DIGEST_EXCLUDE_GLOBS
    assert "*.pyc" in _DIGEST_EXCLUDE_GLOBS


def test_importer_copy_ignore_uses_shared_digest_globs(module_dir, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text(MANIFEST)
    (src / "helper.py").write_text("x = 1")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "helper.cpython-311.pyc").write_bytes(b"\x00")
    (src / "stale.pyc").write_bytes(b"\x00")
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("[core]")
    (src / ".github").mkdir()
    (src / ".github" / "workflow.yml").write_text("on: push")
    install_from_folder(src)
    installed = module_dir / "my-module"
    assert (installed / "helper.py").exists()
    assert not (installed / "__pycache__").exists()
    assert not (installed / "stale.pyc").exists()
    assert not (installed / ".git").exists()
    assert not (installed / ".github").exists()


# ── migrations table_prefix validation ─────────────────────────────────────────

def _migrations_manifest(name: str, *, prefix: str | None, migrations: bool = True) -> str:
    lines = [
        "PLUGIN_MANIFEST = {",
        f'    "name": "{name}",',
        '    "version": "1.0.0",',
    ]
    if migrations:
        lines.append('    "migrations": "inner.migrations",')
    if prefix is not None:
        lines.append(f'    "table_prefix": "{prefix}",')
    lines.append("}\n")
    return "\n".join(lines)


def test_missing_table_prefix_with_migrations_declared_refused(module_dir):
    data = _zip_bytes({"__init__.py": _migrations_manifest("mig-mod", prefix=None)})
    with pytest.raises(ModuleImportError, match="table_prefix"):
        install_from_zip(data)


def test_table_prefix_shorter_than_three_chars_refused(module_dir):
    data = _zip_bytes({"__init__.py": _migrations_manifest("mig-mod", prefix="a_")})
    with pytest.raises(ModuleImportError, match="3 characters"):
        install_from_zip(data)


def test_table_prefix_not_ending_in_underscore_refused(module_dir):
    data = _zip_bytes({"__init__.py": _migrations_manifest("mig-mod", prefix="acme")})
    with pytest.raises(ModuleImportError, match="underscore"):
        install_from_zip(data)


def test_table_prefix_colliding_with_core_table_refused(module_dir):
    import celerp.models  # noqa: F401  ensure core tables are registered
    data = _zip_bytes({"__init__.py": _migrations_manifest("mig-mod", prefix="connector_")})
    with pytest.raises(ModuleImportError, match="connector_configs"):
        install_from_zip(data)


def test_table_prefix_overlapping_installed_module_refused(module_dir):
    install_from_zip(_zip_bytes(
        {"__init__.py": _migrations_manifest("first-mod", prefix="acme_")}))
    data = _zip_bytes({"__init__.py": _migrations_manifest("second-mod", prefix="acme_sub_")})
    with pytest.raises(ModuleImportError, match="acme_"):
        install_from_zip(data)

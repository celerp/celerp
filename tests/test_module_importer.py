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
    with pytest.raises(ModuleImportError, match="something unsafe"):
        install_from_zip(data)
    assert not (module_dir.parent / "evil.py").exists()


def test_symlink_entry_refused(module_dir):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("__init__.py", MANIFEST)
        info = zipfile.ZipInfo("link.py")
        info.external_attr = (0o120777 << 16)  # symlink mode
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ModuleImportError, match="links to other files"):
        install_from_zip(buf.getvalue())


def test_oversize_archive_refused(module_dir):
    with pytest.raises(ModuleImportError, match="larger than 50 MB"):
        install_from_zip(b"x" * (MAX_ARCHIVE_BYTES + 1))


def test_not_a_zip_refused(module_dir):
    with pytest.raises(ModuleImportError, match="isn't a ZIP file"):
        install_from_zip(b"definitely not a zip")


def test_missing_manifest_refused(module_dir):
    data = _zip_bytes({"__init__.py": "x = 1"})
    with pytest.raises(ModuleImportError, match="isn't a Celerp module"):
        install_from_zip(data)


def test_missing_init_refused(module_dir):
    data = _zip_bytes({"readme.md": "hello"})
    with pytest.raises(ModuleImportError, match="isn't a Celerp module"):
        install_from_zip(data)


def test_reserved_prefix_refused(module_dir):
    manifest = MANIFEST.replace("my-module", "celerp-sneaky")
    with pytest.raises(ModuleImportError, match="reserved"):
        install_from_zip(_zip_bytes({"__init__.py": manifest}))


def test_bad_name_chars_refused(module_dir):
    manifest = MANIFEST.replace("my-module", "my module!")
    with pytest.raises(ModuleImportError, match="characters that aren't allowed"):
        install_from_zip(_zip_bytes({"__init__.py": manifest}))


def test_collision_refused(module_dir):
    data = _zip_bytes({"__init__.py": MANIFEST})
    install_from_zip(data)
    with pytest.raises(ModuleImportError, match="already installed"):
        install_from_zip(data)


def test_non_literal_manifest_refused(module_dir):
    bad = "PLUGIN_MANIFEST = {'name': open('/etc/passwd').read()}"
    with pytest.raises(ModuleImportError, match="description is built incorrectly"):
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
    with pytest.raises(ModuleImportError, match="links to other files"):
        install_from_folder(src)


def test_folder_not_a_dir_refused(module_dir, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ModuleImportError, match="isn't a folder"):
        install_from_folder(f)


def test_no_module_dir_configured(monkeypatch):
    monkeypatch.setenv("MODULE_DIR", "")
    with pytest.raises(ModuleImportError, match="set a module folder"):
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


@pytest.mark.parametrize("name", ["Celerp-mine", "CELERP-mine", "cElErP-mine",
                                  "celerp_mine", "CELERP_mine"])
@pytest.mark.parametrize("source", ["sideloaded", "community"])
def test_upload_refuses_the_reserved_prefix_in_any_case(module_dir, name, source):
    with pytest.raises(ModuleImportError, match="'celerp-' or 'celerp_', in any letter case, are reserved for Marketplace modules"):
        install_from_zip(_zip_bytes({"__init__.py": MANIFEST.replace("my-module", name)}),
                         source=source)
    assert not (module_dir / name).exists()


@pytest.mark.parametrize("name", ["Celerp-warehousing", "celerp_warehousing"])
def test_official_install_requires_the_exact_celerp_prefix(module_dir, name):
    with pytest.raises(ModuleImportError, match="celerp-"):
        install_from_zip(_zip_bytes({"__init__.py": MANIFEST.replace("my-module", name)}),
                         official=True)


def test_premium_install_writes_license_marker(module_dir):
    from celerp.modules.importer import PREMIUM_MARKER
    install_from_zip(
        _zip_bytes({"__init__.py": OFFICIAL_MANIFEST}), official=True, premium=True)
    assert (module_dir / "celerp-warehousing" / PREMIUM_MARKER).exists()


def test_free_install_writes_no_marker(module_dir):
    from celerp.modules.importer import PREMIUM_MARKER
    install_from_zip(_zip_bytes({"__init__.py": MANIFEST}))
    assert not (module_dir / "my-module" / PREMIUM_MARKER).exists()


# ── concurrency: two installs of one slug land exactly one module ─────────────

def test_concurrent_installs_of_same_slug_land_exactly_one(module_dir):
    """Two installs of the same slug that reach the install lock together end with
    one complete install and one canonical already-exists refusal, and leave no
    landing dir behind."""
    import contextlib
    import threading

    from celerp.modules import importer

    real_lock = importer._one_install_at_a_time
    both_arrived = threading.Barrier(2, timeout=5)

    @contextlib.contextmanager
    def _arrive_together():
        both_arrived.wait()
        with real_lock():
            yield

    manifest = MANIFEST.replace("my-module", "same-slug")
    results: dict[str, dict] = {}
    errors: dict[str, Exception] = {}

    def _install(key):
        try:
            results[key] = install_from_zip(_zip_bytes({"__init__.py": manifest, "data.txt": "payload"}))
        except Exception as exc:  # noqa: BLE001 - captured for the assertions below
            errors[key] = exc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(importer, "_one_install_at_a_time", _arrive_together)
        threads = [threading.Thread(target=_install, args=(key,)) for key in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    assert len(results) == 1 and len(errors) == 1, (results, errors)
    assert next(iter(results.values()))["name"] == "same-slug"
    error = next(iter(errors.values()))
    assert isinstance(error, ModuleImportError)
    assert str(error) == "A module named 'same-slug' already exists. Remove it first, then import."
    installed = module_dir / "same-slug"
    assert (installed / "__init__.py").read_text() == manifest
    assert (installed / "data.txt").read_text() == "payload"
    assert not list(module_dir.glob(".same-slug.incoming-*"))


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
    with pytest.raises(ModuleImportError, match="already installed"):
        install_from_zip(_zip_bytes({"__init__.py": MANIFEST}))


# ── premium marker cannot be smuggled in from package contents ────────────────

def test_zip_with_premium_marker_entry_refused(module_dir):
    from celerp.modules.importer import PREMIUM_MARKER
    data = _zip_bytes({"__init__.py": MANIFEST, PREMIUM_MARKER: ""})
    with pytest.raises(ModuleImportError, match="file name that isn't allowed"):
        install_from_zip(data)
    assert not (module_dir / "my-module").exists()


def test_folder_with_premium_marker_file_refused(module_dir, tmp_path):
    from celerp.modules.importer import PREMIUM_MARKER
    src = tmp_path / "src-module"
    src.mkdir()
    (src / "__init__.py").write_text(MANIFEST)
    (src / PREMIUM_MARKER).write_text("")
    with pytest.raises(ModuleImportError, match="file name that isn't allowed"):
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


@pytest.mark.parametrize("premium", [True, False])
def test_install_metadata_holds_only_provenance(premium, module_dir):
    """The install metadata records where a module came from and when, nothing
    else."""
    import json
    from celerp.modules.meta import META_FILENAME
    name = "celerp-provenance-paid"
    install_from_zip(_zip_bytes({"__init__.py": f"PLUGIN_MANIFEST = {{'name': {name!r}, "
                                                 "'version': '1.0.0'}\n"}, root=f"{name}/"),
                     official=True, premium=premium, source="marketplace")
    assert set(json.loads((module_dir / name / META_FILENAME).read_text())) == {"source", "installed_at"}


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


def test_remove_module_dir_waits_for_an_install_in_progress(module_dir):
    """An install checks names and prefixes against what is on disk; a removal
    landing in the middle of it would change that under it."""
    import threading

    from celerp.modules.importer import _one_install_at_a_time, remove_module_dir
    install_from_zip(_zip_bytes({"__init__.py": MANIFEST}))
    held, release = threading.Event(), threading.Event()

    def install_in_progress():
        with _one_install_at_a_time():
            held.set()
            release.wait(10)

    holder = threading.Thread(target=install_in_progress)
    holder.start()
    assert held.wait(10)
    remover = threading.Thread(target=remove_module_dir, args=("my-module",))
    remover.start()
    remover.join(0.5)
    still_there = (module_dir / "my-module").exists()
    release.set()
    holder.join(10)
    remover.join(10)

    assert still_there
    assert not (module_dir / "my-module").exists()


def test_remove_module_dir_raises_if_absent(module_dir):
    from celerp.modules.importer import remove_module_dir
    with pytest.raises(ModuleImportError, match="isn't installed"):
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
    with pytest.raises(ModuleImportError, match="can't be written to"):
        importer._module_dir()


def test_install_into_bundled_dir_refused(monkeypatch, tmp_path):
    """The guard holds at the install entrypoint, before any bytes are written."""
    from celerp.modules import loader
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(loader, "_BUNDLED_MODULES_DIRS", (bundled,))
    monkeypatch.setenv("MODULE_DIR", str(bundled))
    with pytest.raises(ModuleImportError, match="can't be written to"):
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


def test_with_writable_module_dir_defaults_an_unset_dir_to_the_bundled_trees(monkeypatch, tmp_path):
    """No module dir given (a bare `uvicorn` dev run) searches what `celerp start`
    gives a launch: the writable drop-in first, then the bundled trees that exist."""
    from celerp.modules import loader
    monkeypatch.setattr(loader, "writable_module_dir", lambda: tmp_path / "modules")
    root = loader.BUNDLED_SOURCE_DIR.parent
    bundled = [str(d) for d in loader.bundled_module_dirs(root) if d.exists()]
    assert loader.with_writable_module_dir(None).split(",") == [str(tmp_path / "modules"), *bundled]
    assert str(root / "default_modules") in bundled


def test_with_writable_module_dir_empty_unchanged():
    """A module dir set to empty means no module trees - the helper never invents one."""
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
        lines.append(f'    "company_backup": {{"{prefix}things": "include"}},')
    lines.append("}\n")
    return "\n".join(lines)


def test_missing_table_prefix_with_migrations_declared_refused(module_dir):
    data = _zip_bytes({"__init__.py": _migrations_manifest("mig-mod", prefix=None)})
    with pytest.raises(ModuleImportError, match="doesn't name the data it stores"):
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


@pytest.mark.parametrize("prefix, table", [("alembic_", "alembic_version"),
                                           ("instance_", "instance_meta")])
def test_table_prefix_claiming_a_core_table_without_a_model_refused(module_dir, tmp_path, prefix, table):
    """Celerp owns its schema stamp and upgrade markers though no model declares them."""
    data = _zip_bytes({"__init__.py": _migrations_manifest("mig-mod", prefix=prefix)})
    with pytest.raises(ModuleImportError, match=table):
        install_from_zip(data)
    src = tmp_path / "src" / "mig-mod"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(_migrations_manifest("mig-mod", prefix=prefix))
    with pytest.raises(ModuleImportError, match=table):
        install_from_folder(src)


@pytest.mark.parametrize("prefix", ["label_", "marketplace_", "bank_"])
def test_table_prefix_claiming_a_turned_off_bundled_module_table_refused(
        module_dir, tmp_path, bundled_modules_unloaded, prefix):
    """A bundled module's tables stay Celerp's while the module is turned off and its
    models are not loaded."""
    table = bundled_modules_unloaded[prefix]
    data = _zip_bytes({"__init__.py": _migrations_manifest("mig-mod", prefix=prefix)})
    with pytest.raises(ModuleImportError, match=table):
        install_from_zip(data)
    src = tmp_path / "src" / "mig-mod"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(_migrations_manifest("mig-mod", prefix=prefix))
    with pytest.raises(ModuleImportError, match=table):
        install_from_folder(src)


def test_table_prefix_overlapping_installed_module_refused(module_dir):
    install_from_zip(_zip_bytes(
        {"__init__.py": _migrations_manifest("first-mod", prefix="acme_")}))
    data = _zip_bytes({"__init__.py": _migrations_manifest("second-mod", prefix="acme_sub_")})
    with pytest.raises(ModuleImportError, match="acme_"):
        install_from_zip(data)


# A module with models but no migrations still owns its tables by prefix: the
# purge drops and the backup attributes by it, so the same checks apply.

@pytest.mark.parametrize("prefix, reason", [
    ("ab", "3 characters"),
    ("acme", "underscore"),
    ("connector_", "connector_configs"),
])
def test_model_only_table_prefix_is_validated(module_dir, prefix, reason):
    import celerp.models  # noqa: F401  ensure core tables are registered
    data = _zip_bytes({"__init__.py": _migrations_manifest("model-mod", prefix=prefix, migrations=False)})
    with pytest.raises(ModuleImportError, match=reason):
        install_from_zip(data)
    assert not (module_dir / "model-mod").exists()


def test_model_only_table_prefix_overlapping_installed_module_refused(module_dir):
    install_from_zip(_zip_bytes(
        {"__init__.py": _migrations_manifest("first-mod", prefix="acme_", migrations=False)}))
    data = _zip_bytes({"__init__.py": _migrations_manifest("second-mod", prefix="acme_sub_", migrations=False)})
    with pytest.raises(ModuleImportError, match="acme_"):
        install_from_zip(data)
    assert not (module_dir / "second-mod").exists()


@pytest.mark.parametrize("second", ["zip", "folder"])
def test_overlapping_modules_installed_at_once_only_one_lands(module_dir, tmp_path, monkeypatch, second):
    """Two installs whose prefixes overlap, started together: exactly one lands."""
    import threading
    import time

    from celerp.modules import importer

    real_write_meta = importer.write_meta

    def slow_write_meta(*args, **kwargs):
        time.sleep(0.3)
        return real_write_meta(*args, **kwargs)

    monkeypatch.setattr(importer, "write_meta", slow_write_meta)
    src = tmp_path / "src" / "second-mod"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(_migrations_manifest("second-mod", prefix="acme_sub_"))
    installs = [
        lambda: install_from_zip(_zip_bytes(
            {"__init__.py": _migrations_manifest("first-mod", prefix="acme_")})),
        (lambda: install_from_zip(_zip_bytes({"__init__.py": src.joinpath("__init__.py").read_text()})))
        if second == "zip" else (lambda: install_from_folder(src)),
    ]
    outcomes: list = []

    def run(install):
        try:
            outcomes.append(install()["name"])
        except ModuleImportError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=run, args=(install,)) for install in installs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    landed = sorted(p.name for p in module_dir.iterdir() if not p.name.startswith("."))
    assert len(landed) == 1, (landed, outcomes)
    refused = [o for o in outcomes if isinstance(o, ModuleImportError)]
    assert len(refused) == 1 and "overlaps" in str(refused[0]), outcomes


def test_model_only_module_without_table_prefix_installs(module_dir):
    install_from_zip(_zip_bytes(
        {"__init__.py": _migrations_manifest("plain-mod", prefix=None, migrations=False)}))
    assert (module_dir / "plain-mod" / "__init__.py").exists()

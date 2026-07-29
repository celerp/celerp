# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Module importer — the single validation + install path for every way a
module package arrives.

Two thin entrypoints, one core:
  - install_from_zip(data)     — Import Module upload (browser) and, later, the
                                 marketplace installer (downloaded artifact).
  - install_from_folder(path)  — desktop "Import Module…" folder picker (the
                                 backend runs locally, so a path is enough).

Both funnel through the same checks, so the security posture cannot drift
between surfaces:
  - manifest must parse (PLUGIN_MANIFEST with a valid "name")
  - the installed folder name IS the manifest name (id = folder = manifest)
  - the "celerp-" prefix is reserved for first-party modules
  - size caps, zip-slip guards, symlink rejection
  - min_celerp_version gate against the running app
  - collision refusal (existing module of the same name must be removed first)

The importer only writes into the module directory; enabling and the restart
are separate, deliberate steps (see the modules UI).
"""
from __future__ import annotations

import ast
import errno
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path

from celerp.modules.meta import write_meta

# Compressed and uncompressed caps. Generous for code, hostile to zip bombs.
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024

_RESERVED_PREFIX = "celerp-"
_NAME_MAX = 64

# Marker file the marketplace installer drops inside a PAID module's directory.
# The license gate (celerp.modules.license.is_premium_path) treats a directory
# carrying it like premium_modules/, so downloaded paid modules are license-checked
# at load without needing a second module dir.
PREMIUM_MARKER = ".celerp-premium"


class ModuleImportError(Exception):
    """User-facing import failure. Message is safe to show in the UI."""


def _validate_name_chars(name: str) -> None:
    """Length and character-set rules shared by install and delete.

    Delete resolves a folder from a caller-supplied name, so it needs the same
    charset guard as install (no separators, no traversal) without the celerp-
    prefix trust rule, which only governs where a NEW package may install.
    """
    if not name or len(name) > _NAME_MAX:
        raise ModuleImportError("Module name missing or too long.")
    ok = all(c.isascii() and (c.isalnum() or c in "-_") for c in name)
    if not ok or not name[0].isalnum():
        raise ModuleImportError(
            "Module name may only contain letters, digits, '-' and '_'."
        )


def _validate_name(name: str, *, official: bool = False) -> None:
    _validate_name_chars(name)
    # The celerp- prefix is the trust boundary: sideloads may never claim it, and
    # the marketplace-download path (official=True, relay-authenticated) may ONLY
    # install under it - so neither path can impersonate the other.
    if official != name.startswith(_RESERVED_PREFIX):
        raise ModuleImportError(
            "The 'celerp-' name prefix is reserved for official modules."
            if not official else
            "Official module packages must use the 'celerp-' name prefix."
        )


def _manifest_node(tree: ast.AST):
    """The `PLUGIN_MANIFEST = {...}` assignment node in a parsed module, or None."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PLUGIN_MANIFEST":
                return node
    return None


def _read_manifest(init_py_text: str) -> dict:
    """Extract PLUGIN_MANIFEST literals without importing anything."""
    try:
        tree = ast.parse(init_py_text)
    except Exception:
        raise ModuleImportError("__init__.py does not parse as Python.")
    node = _manifest_node(tree)
    if node is None:
        raise ModuleImportError("No PLUGIN_MANIFEST found in __init__.py.")
    try:
        manifest = ast.literal_eval(node.value)
    except Exception:
        raise ModuleImportError("PLUGIN_MANIFEST must contain only literal values.")
    if not isinstance(manifest, dict):
        raise ModuleImportError("PLUGIN_MANIFEST must be a dict.")
    return manifest


def _has_manifest(init_py: Path) -> bool:
    """True if an __init__.py declares a PLUGIN_MANIFEST, without executing it."""
    try:
        tree = ast.parse(init_py.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False
    return _manifest_node(tree) is not None


def _locate_module(tree: Path) -> tuple[Path, dict]:
    """Find the module package inside an already-extracted archive.

    The archive root is tried first (a bare module zip); otherwise the single
    subfolder whose __init__.py declares a PLUGIN_MANIFEST is the module - so a
    repo that keeps its module in a subdirectory (the module template ships the
    example module beside its README, lint, and tests) still installs. More than
    one candidate is refused so the install target is never ambiguous."""
    root_init = tree / "__init__.py"
    if root_init.exists() and _has_manifest(root_init):
        return tree, _read_manifest(root_init.read_text(encoding="utf-8", errors="replace"))
    candidates = sorted(p.parent for p in tree.rglob("__init__.py") if _has_manifest(p))
    if len(candidates) == 1:
        init = candidates[0] / "__init__.py"
        return candidates[0], _read_manifest(init.read_text(encoding="utf-8", errors="replace"))
    if not candidates:
        raise ModuleImportError(
            "No __init__.py with a PLUGIN_MANIFEST found; not a Celerp module package."
        )
    raise ModuleImportError(
        "The archive contains more than one module; import a single-module package."
    )


def _version_tuple(v: str) -> tuple:
    parts = []
    for chunk in str(v).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _check_min_version(manifest: dict) -> None:
    wanted = manifest.get("min_celerp_version")
    if not wanted:
        return
    try:
        from celerp import __version__ as current
    except Exception:
        return
    if "dev" in current or current.startswith("0.0.0"):
        return  # dev builds bypass the gate
    if _version_tuple(current) < _version_tuple(str(wanted)):
        raise ModuleImportError(
            f"This module requires Celerp {wanted} or newer (you run {current}). "
            "Update Celerp first."
        )


def _module_dir() -> Path:
    raw = os.environ.get("MODULE_DIR", "")
    first = raw.split(",")[0].strip()
    if not first:
        raise ModuleImportError("This install has no module directory configured.")
    d = Path(first)
    # A sideload must never land in a bundled/trusted dir: a package written there
    # would inherit first-party trust by name. Refuse rather than write into it.
    from celerp.modules.loader import is_bundled_dir
    if is_bundled_dir(d):
        raise ModuleImportError(
            "The module directory points at the bundled default modules, which is "
            "read-only. Configure a writable MODULE_DIR for imports."
        )
    d.mkdir(parents=True, exist_ok=True)
    return d


def _target_for(name: str) -> Path:
    target = _module_dir() / name
    if target.exists():
        raise ModuleImportError(
            f"A module named '{name}' already exists. Remove it first, then import."
        )
    return target


def remove_module_dir(name: str) -> None:
    """Delete an installed module's folder, freeing the name for re-import.

    The name is charset-validated (same guard as install, so no separators or
    traversal reach the filesystem) and the resolved target must sit directly
    under the module dir. Removal mirrors the install landing: rename to a hidden
    `.<name>.deleting-<uuid>` then rmtree, so a crash never leaves a half-deleted
    tree under the live module name.
    """
    _validate_name_chars(name)
    base = _module_dir()
    target = base / name
    if target.resolve().parent != base.resolve() or not target.is_dir():
        raise ModuleImportError(f"Module '{name}' is not installed.")
    grave = base / f".{name}.deleting-{uuid.uuid4().hex}"
    try:
        os.replace(target, grave)
    except OSError as exc:
        raise ModuleImportError(f"Could not remove the module: {exc}")
    shutil.rmtree(grave, ignore_errors=True)


def _finish(staged: Path, manifest: dict, *, official: bool = False,
            premium: bool = False, source: str = "sideloaded") -> dict:
    name = str(manifest.get("name", ""))
    _validate_name(name, official=official)
    _check_min_version(manifest)
    # Reconcile the marker in BOTH directions - belt and suspenders alongside
    # the explicit reserved-name refusals above: this is the one place every
    # entrypoint (zip, folder) funnels through, so it is the actual source of
    # truth for whether an installed module is license-gated, regardless of
    # what either entrypoint's package contents happened to carry.
    marker = staged / PREMIUM_MARKER
    if premium:
        marker.write_text("")
    else:
        marker.unlink(missing_ok=True)
    # Record provenance and install time in the staged tree so the sidecar
    # travels into the landing dir with the rest of the package (one atomic
    # replace, no second write into the live module dir).
    write_meta(staged, source=source)
    target = _target_for(name)
    # Land atomically: copy into a temp dir on the SAME filesystem as the module
    # dir (staged lives under /tmp, often a different device, where shutil.move
    # degrades to a non-atomic copy that can orphan a half-written target on
    # disk-full), then os.replace the finished tree into place. On any failure
    # the partial temp dir is removed and the error is a clean ModuleImportError,
    # not a 500.
    # os.getpid() is identical across concurrent requests in the same process
    # (installs run via asyncio.to_thread, i.e. real OS threads sharing one
    # PID) - two simultaneous installs of the same slug would then race on
    # this exact path, corrupting each other's copytree/replace. A per-call
    # random suffix makes every attempt's landing dir unique regardless of
    # concurrency.
    landing = target.parent / f".{name}.incoming-{uuid.uuid4().hex}"
    try:
        shutil.rmtree(landing, ignore_errors=True)
        shutil.copytree(staged, landing)
        os.replace(landing, target)
    except OSError as exc:
        shutil.rmtree(landing, ignore_errors=True)
        # A concurrent install of the same slug can land the target between
        # _target_for()'s check and this replace. os.replace onto a populated
        # dir raises FileExistsError (EEXIST) or, on Linux, OSError(ENOTEMPTY) -
        # both mean "already there", so surface the same friendly message.
        if isinstance(exc, FileExistsError) or exc.errno == errno.ENOTEMPTY:
            raise ModuleImportError(
                f"A module named '{name}' already exists. Remove it first, then import."
            )
        raise ModuleImportError(f"Could not write the module to disk: {exc}")
    return {
        "name": name,
        "version": str(manifest.get("version", "")),
        "display_name": str(manifest.get("display_name") or manifest.get("label") or name),
    }


# ── zip entrypoint ─────────────────────────────────────────────────────────────

def _zip_root(zf: zipfile.ZipFile) -> str:
    """Return the single top-level folder prefix, or '' for a flat archive.

    GitHub archives wrap everything in one folder; hand-made zips may be flat
    with __init__.py at the root. Both are accepted; anything else is not.
    """
    tops = set()
    for info in zf.infolist():
        name = info.filename
        if name.startswith("/") or name.startswith("\\"):
            raise ModuleImportError("Archive contains absolute paths.")
        top = name.split("/", 1)[0]
        if top:
            tops.add(top)
    if "__init__.py" in zf.namelist():
        return ""
    if len(tops) == 1:
        return tops.pop() + "/"
    raise ModuleImportError(
        "Archive must contain a single module folder (or __init__.py at its root)."
    )


def install_from_zip(data: bytes, *, official: bool = False,
                     premium: bool = False, source: str = "sideloaded") -> dict:
    """Validate and install a module from zip bytes. Returns manifest summary.

    `official` is set ONLY by the marketplace installer (relay-authenticated
    download): it flips the celerp- prefix rule from forbidden to required.
    `premium` drops the license-gate marker for paid modules.
    `source` is recorded in the provenance sidecar and drives the source shield
    and newest-first ordering on the modules page."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ModuleImportError("Archive is too large (limit 50 MB).")
    tmp_zip = None
    staging = Path(tempfile.mkdtemp(prefix="celerp-mod-import-"))
    try:
        tmp_zip = staging / "_pkg.zip"
        tmp_zip.write_bytes(data)
        try:
            zf = zipfile.ZipFile(tmp_zip)
        except zipfile.BadZipFile:
            raise ModuleImportError("That file is not a valid zip archive.")
        with zf:
            root = _zip_root(zf)
            unpacked = 0
            out = staging / "pkg"
            out.mkdir()
            for info in zf.infolist():
                rel = info.filename[len(root):] if root else info.filename
                if not rel or rel.endswith("/"):
                    continue
                # zip-slip guard: resolve inside the staging package only
                dest = (out / rel)
                try:
                    dest.resolve().relative_to(out.resolve())
                except ValueError:
                    raise ModuleImportError("Archive contains unsafe paths.")
                # symlink entries carry the link mode in external_attr
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ModuleImportError("Archive contains symlinks; refused.")
                if rel == PREMIUM_MARKER:
                    # Only the installer itself may write this file (it's how
                    # the license gate decides a module is paid) - a package
                    # that ships it would either fake premium status on a free
                    # module or collide with a genuinely paid install's marker.
                    raise ModuleImportError(
                        "Archive contains a reserved file name; refused.")
                unpacked += info.file_size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise ModuleImportError("Archive expands too large; refused.")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as f:
                    shutil.copyfileobj(src, f, length=1024 * 256)
            module_root, manifest = _locate_module(out)
            return _finish(module_root, manifest, official=official,
                           premium=premium, source=source)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ── folder entrypoint ──────────────────────────────────────────────────────────

def install_from_folder(source_path: str | Path, *,
                        source: str = "sideloaded") -> dict:
    """Validate and install a module from a local folder path (desktop picker).

    `source` is recorded in the provenance sidecar (defaults to a sideload)."""
    src = Path(source_path)
    if not src.is_dir():
        raise ModuleImportError("That path is not a folder.")
    init_py = src / "__init__.py"
    if not init_py.exists():
        raise ModuleImportError(
            "No __init__.py at the folder root; not a Celerp module package."
        )
    if (src / PREMIUM_MARKER).exists():
        # Same reserved-name refusal as the zip path - only the installer
        # itself may write this file.
        raise ModuleImportError("Folder contains a reserved file name; refused.")
    total = 0
    for p in src.rglob("*"):
        if p.is_symlink():
            raise ModuleImportError("Folder contains symlinks; refused.")
        if p.is_file():
            total += p.stat().st_size
            if total > MAX_UNPACKED_BYTES:
                raise ModuleImportError("Folder is too large; refused.")
    manifest = _read_manifest(init_py.read_text(encoding="utf-8", errors="replace"))
    # Function-level import: loader imports PREMIUM_MARKER from this module at load
    # time, so a top-level loader import here would be circular. The digest and this
    # copy share one exclusion set so the installed tree hashes to its lock entry.
    from celerp.modules.loader import _DIGEST_EXCLUDE_GLOBS
    staging = Path(tempfile.mkdtemp(prefix="celerp-mod-import-"))
    try:
        out = staging / "pkg"
        shutil.copytree(src, out, symlinks=False,
                        ignore=shutil.ignore_patterns(*_DIGEST_EXCLUDE_GLOBS))
        return _finish(out, manifest, source=source)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

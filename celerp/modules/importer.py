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
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

# Compressed and uncompressed caps. Generous for code, hostile to zip bombs.
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024

_RESERVED_PREFIX = "celerp-"
_NAME_MAX = 64


class ModuleImportError(Exception):
    """User-facing import failure. Message is safe to show in the UI."""


def _validate_name(name: str) -> None:
    if not name or len(name) > _NAME_MAX:
        raise ModuleImportError("Module name missing or too long.")
    if name.startswith(_RESERVED_PREFIX):
        raise ModuleImportError(
            "The 'celerp-' name prefix is reserved for official modules."
        )
    ok = all(c.isascii() and (c.isalnum() or c in "-_") for c in name)
    if not ok or not name[0].isalnum():
        raise ModuleImportError(
            "Module name may only contain letters, digits, '-' and '_'."
        )


def _read_manifest(init_py_text: str) -> dict:
    """Extract PLUGIN_MANIFEST literals without importing anything."""
    try:
        tree = ast.parse(init_py_text)
    except Exception:
        raise ModuleImportError("__init__.py does not parse as Python.")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PLUGIN_MANIFEST":
                try:
                    manifest = ast.literal_eval(node.value)
                except Exception:
                    raise ModuleImportError(
                        "PLUGIN_MANIFEST must contain only literal values."
                    )
                if not isinstance(manifest, dict):
                    raise ModuleImportError("PLUGIN_MANIFEST must be a dict.")
                return manifest
    raise ModuleImportError("No PLUGIN_MANIFEST found in __init__.py.")


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
    d.mkdir(parents=True, exist_ok=True)
    return d


def _target_for(name: str) -> Path:
    target = _module_dir() / name
    if target.exists():
        raise ModuleImportError(
            f"A module named '{name}' already exists. Remove it first, then import."
        )
    return target


def _finish(staged: Path, manifest: dict) -> dict:
    name = str(manifest.get("name", ""))
    _validate_name(name)
    _check_min_version(manifest)
    target = _target_for(name)
    shutil.move(str(staged), str(target))
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


def install_from_zip(data: bytes) -> dict:
    """Validate and install a module from zip bytes. Returns manifest summary."""
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
                unpacked += info.file_size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise ModuleImportError("Archive expands too large; refused.")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as f:
                    shutil.copyfileobj(src, f, length=1024 * 256)
            init_py = out / "__init__.py"
            if not init_py.exists():
                raise ModuleImportError(
                    "No __init__.py at the module root; not a Celerp module package."
                )
            manifest = _read_manifest(init_py.read_text(encoding="utf-8", errors="replace"))
            return _finish(out, manifest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ── folder entrypoint ──────────────────────────────────────────────────────────

def install_from_folder(source: str | Path) -> dict:
    """Validate and install a module from a local folder path (desktop picker)."""
    src = Path(source)
    if not src.is_dir():
        raise ModuleImportError("That path is not a folder.")
    init_py = src / "__init__.py"
    if not init_py.exists():
        raise ModuleImportError(
            "No __init__.py at the folder root; not a Celerp module package."
        )
    total = 0
    for p in src.rglob("*"):
        if p.is_symlink():
            raise ModuleImportError("Folder contains symlinks; refused.")
        if p.is_file():
            total += p.stat().st_size
            if total > MAX_UNPACKED_BYTES:
                raise ModuleImportError("Folder is too large; refused.")
    manifest = _read_manifest(init_py.read_text(encoding="utf-8", errors="replace"))
    staging = Path(tempfile.mkdtemp(prefix="celerp-mod-import-"))
    try:
        out = staging / "pkg"
        shutil.copytree(src, out, symlinks=False,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".github"))
        return _finish(out, manifest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

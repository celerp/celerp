# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Module importer - the single validation + install path for every way a
module package arrives.

Two thin entrypoints, one core:
  - install_from_zip(data)     - Import Module upload (browser) and, later, the
                                 marketplace installer (downloaded artifact).
  - install_from_folder(path)  - desktop "Import Module..." folder picker (the
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
import functools
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path

from celerp.modules.meta import write_meta
from ui.i18n import t

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
        raise ModuleImportError(t("module_import.name_invalid"))
    ok = all(c.isascii() and (c.isalnum() or c in "-_") for c in name)
    if not ok or not name[0].isalnum():
        raise ModuleImportError(t("module_import.name_chars"))


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
        raise ModuleImportError(t("module_import.broken_init"))
    node = _manifest_node(tree)
    if node is None:
        raise ModuleImportError(t("module_import.no_manifest"))
    try:
        manifest = ast.literal_eval(node.value)
    except Exception:
        raise ModuleImportError(t("module_import.manifest_bad"))
    if not isinstance(manifest, dict):
        raise ModuleImportError(t("module_import.manifest_bad"))
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
        raise ModuleImportError(t("module_import.not_a_module"))
    raise ModuleImportError(t("module_import.multiple_modules"))


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


def installed_table_prefixes(exclude: str) -> dict[str, str]:
    """Return {module_name: table_prefix} for every installed module that
    declares one, across every MODULE_DIR entry, excluding ``exclude`` so a
    reinstall of the same name never collides with its own prior copy. Manifests
    are read via AST, never imported.
    """
    from celerp.modules.loader import read_manifest

    out: dict[str, str] = {}
    for entry in os.environ.get("MODULE_DIR", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        base = Path(entry)
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.name == exclude or not (child / "__init__.py").exists():
                continue
            other = read_manifest(child).get("table_prefix")
            if isinstance(other, str) and other:
                out[child.name] = other
    return out


# A prefix is at least this long and ends in an underscore, so "acme_" scopes
# cleanly and can never be a bare word that swallows unrelated tables.
MIN_TABLE_PREFIX_LEN = 3


def reserved_tables(name: str) -> frozenset[str]:
    """Every table module *name*'s prefix may not claim, whichever modules this process
    has loaded: every table Celerp's migration history has created or changed (obsolete
    ones included), every table a bundled module declares or migrates, the tables the
    loaded models declare other than *name*'s own, and the two Celerp manages without a
    model, alembic's schema stamp and the instance's upgrade markers. The one source
    for install, migrations, purge and backup attribution, through table_prefix_problem."""
    from celerp.migrations._data_reconcile import _META_TABLE
    from celerp.models.base import Base
    import celerp.models  # noqa: F401  (registers every core table)

    loaded = frozenset(Base.metadata.tables) - _loaded_tables_of(name)
    return _historical_tables() | loaded | {"alembic_version", _META_TABLE}


@functools.lru_cache(maxsize=1)
def _historical_tables() -> frozenset[str]:
    """Tables named in Celerp's migration history and in the bundled modules (their
    migrations and model declarations), read from the source files, never imported.
    A turned-off module's models are never loaded, so its tables are known only here."""
    from celerp.migrations import _auto_stamp
    from celerp.modules.loader import BUNDLED_SOURCE_DIR, read_manifest

    migration_files = list((Path(_auto_stamp.__file__).parent / "versions").glob("*.py"))
    declared: set[str] = set()
    for module in sorted(BUNDLED_SOURCE_DIR.iterdir()) if BUNDLED_SOURCE_DIR.is_dir() else ():
        if not (module / "__init__.py").is_file():
            continue
        package = read_manifest(module).get("migrations")
        if isinstance(package, str) and package:
            migration_files += module.joinpath(*package.split(".")).glob("*.py")
        for source in module.rglob("*.py"):
            if "tests" not in source.relative_to(module).parts:
                declared |= _declared_table_names(source)
    history = {sig.table for path in migration_files for sig in _auto_stamp.extract_signatures(path)}
    return frozenset(history | declared)


def _declared_table_names(source: Path) -> set[str]:
    """The literal ``__tablename__`` values a source file assigns."""
    try:
        tree = ast.parse(source.read_text())
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    return {node.value.value for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in node.targets)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}


def _loaded_tables_of(name: str) -> frozenset[str]:
    """The loaded tables whose model class is defined in a file inside an installed
    copy of module *name* (its inner package name need not match the folder, e.g.
    acme-widgets/acme_widgets)."""
    import inspect
    import sys

    from celerp.models.base import Base
    from celerp.modules.loader import module_search_path

    roots = [os.path.realpath(Path(entry) / name) + os.sep
             for entry in module_search_path().split(",") if entry]

    def _owned(cls) -> bool:
        try:
            source = inspect.getsourcefile(sys.modules[cls.__module__]) or ""
        except (KeyError, TypeError):
            return False
        return os.path.realpath(source).startswith(tuple(roots))

    return frozenset(mapper.local_table.name for mapper in Base.registry.mappers
                     if _owned(mapper.class_))


def table_prefix_problem(name: str, prefix: object,
                         installed: dict[str, str] | None = None) -> str | None:
    """Why *prefix* cannot scope module *name*'s tables, or None when it can.

    The migration runner scopes DDL by the prefix and the purge drops every table
    carrying it, so a prefix that captures a core table or overlaps another
    module's would put foreign data in reach. Checked wherever the prefix is
    trusted (install, migrations, purge, backup attribution), because a module
    copied in by hand never passed the install check. *installed* is the other
    modules' prefixes, read from MODULE_DIR when not given.
    """
    if not isinstance(prefix, str) or not prefix:
        return ('"table_prefix" must name the tables the module owns '
                '(for example "acme_").')
    if len(prefix) < MIN_TABLE_PREFIX_LEN:
        return (f'table_prefix "{prefix}" is too short; it must be at least '
                f'{MIN_TABLE_PREFIX_LEN} characters.')
    if not prefix.endswith("_"):
        return f'table_prefix "{prefix}" must end with an underscore (for example "acme_").'
    for table_name in sorted(reserved_tables(name)):
        if table_name.startswith(prefix):
            return (f'table_prefix "{prefix}" collides with the existing table '
                    f'"{table_name}". Choose a prefix that no core or installed '
                    "table begins with.")
    if installed is None:
        installed = _wellformed_prefixes(exclude=name)
    for other_name, other_prefix in installed.items():
        if other_name != name and (prefix.startswith(other_prefix) or other_prefix.startswith(prefix)):
            return (f'table_prefix "{prefix}" overlaps the prefix "{other_prefix}" '
                    f'already claimed by installed module "{other_name}". Prefixes '
                    "must not be prefixes of one another.")
    return None


def _wellformed_prefixes(exclude: str) -> dict[str, str]:
    """The installed prefixes that pass every check except overlap. Only these can
    overlap another module's: a malformed or colliding prefix owns nothing, so it
    never disqualifies a sound one."""
    return {name: prefix for name, prefix in installed_table_prefixes(exclude=exclude).items()
            if table_prefix_problem(name, prefix, {}) is None}


def valid_table_prefixes() -> dict[str, str]:
    """{module_name: table_prefix} for the installed modules whose prefix passes
    table_prefix_problem. Modules whose sound prefixes overlap are both left out."""
    wellformed = _wellformed_prefixes(exclude="")
    return {name: prefix for name, prefix in wellformed.items()
            if table_prefix_problem(name, prefix, wellformed) is None}


def _validate_table_prefix(name: str, manifest: dict) -> None:
    """A declared ``table_prefix`` must pass table_prefix_problem, and a module
    that declares migrations must declare one. Refuse the install with a clear
    reason rather than defaulting a prefix, since a wrong default silently
    mis-scopes purge.
    """
    if "table_prefix" not in manifest:
        if manifest.get("migrations"):
            raise ModuleImportError(t("module_import.no_table_prefix"))
        return
    problem = table_prefix_problem(name, manifest["table_prefix"])
    if problem:
        raise ModuleImportError(problem)


# A Postgres table name as Celerp creates them: lower case, at most 63 characters.
TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _validate_company_backup(name: str, manifest: dict) -> None:
    """A module that owns tables must say, in ``company_backup``, how each travels with a
    company backup: ``"include"`` for the company's business data, ``"exclude"`` for this
    installation's state such as credentials or caches. Without it, a company holding the
    module's data could not be backed up, so the install is refused up front."""
    prefix = manifest.get("table_prefix")
    if not manifest.get("migrations") and not prefix:
        return
    declared = manifest.get("company_backup")
    if not isinstance(declared, dict) or not declared:
        raise ModuleImportError(t("module_import.no_backup_rule", name=name))
    for table, how in declared.items():
        if not isinstance(table, str) or not TABLE_NAME.fullmatch(table):
            raise ModuleImportError(t("module_import.backup_rule_bad"))
        if not table.startswith(prefix):
            raise ModuleImportError(t("module_import.backup_rule_bad"))
        if how not in ("include", "exclude"):
            raise ModuleImportError(t("module_import.backup_rule_bad"))


def _module_dir() -> Path:
    raw = os.environ.get("MODULE_DIR", "")
    first = raw.split(",")[0].strip()
    if not first:
        raise ModuleImportError(t("module_import.no_module_dir"))
    d = Path(first)
    # A sideload must never land in a bundled/trusted dir: a package written there
    # would inherit first-party trust by name. Refuse rather than write into it.
    from celerp.modules.loader import is_bundled_dir
    if is_bundled_dir(d):
        raise ModuleImportError(t("module_import.module_dir_read_only"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _target_for(name: str) -> Path:
    target = _module_dir() / name
    if target.exists():
        raise ModuleImportError(t("module_import.already_installed", name=name))
    return target


def remove_module_dir(name: str) -> None:
    """Delete an installed module's folder, freeing the name for re-import.

    Removes EVERY copy of the name across the MODULE_DIR search entries, not just
    the first: the scan and loader search all entries, so a straggler copy in a
    later entry (e.g. one landed under an older folder layout) would otherwise
    resurface as a sideload the moment the first copy is gone. A copy whose
    content matches the committed first-party lock is never touched - deleting a
    stale shadow of a bundled default must not take the genuine default with it.

    The name is charset-validated (same guard as install, so no separators or
    traversal reach the filesystem) and each target must sit directly under its
    search entry. Removal mirrors the install landing: rename to a hidden
    `.<name>.deleting-<uuid>` then rmtree, so a crash never leaves a half-deleted
    tree under the live module name.
    """
    from celerp.modules.loader import is_first_party

    _validate_name_chars(name)
    removed = False
    for entry in os.environ.get("MODULE_DIR", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        base = Path(entry)
        target = base / name
        if not target.is_dir() or target.resolve().parent != base.resolve():
            continue
        if is_first_party(target):
            continue
        grave = base / f".{name}.deleting-{uuid.uuid4().hex}"
        try:
            os.replace(target, grave)
        except OSError as exc:
            raise ModuleImportError(t("module_import.remove_failed", exc=exc))
        shutil.rmtree(grave, ignore_errors=True)
        removed = True
    if not removed:
        raise ModuleImportError(t("module_import.not_installed", name=name))


def _finish(staged: Path, manifest: dict, *, official: bool = False,
            premium: bool = False, source: str = "sideloaded") -> dict:
    name = str(manifest.get("name", ""))
    _validate_name(name, official=official)
    _check_min_version(manifest)
    _validate_table_prefix(name, manifest)
    _validate_company_backup(name, manifest)
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
            raise ModuleImportError(t("module_import.already_installed", name=name))
        raise ModuleImportError(t("module_import.write_failed", exc=exc))
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
            raise ModuleImportError(t("module_import.unsafe"))
        top = name.split("/", 1)[0]
        if top:
            tops.add(top)
    if "__init__.py" in zf.namelist():
        return ""
    if len(tops) == 1:
        return tops.pop() + "/"
    raise ModuleImportError(t("module_import.bad_layout"))


def install_from_zip(data: bytes, *, official: bool = False,
                     premium: bool = False, source: str = "sideloaded") -> dict:
    """Validate and install a module from zip bytes. Returns manifest summary.

    `official` is set ONLY by the marketplace installer (relay-authenticated
    download): it flips the celerp- prefix rule from forbidden to required.
    `premium` drops the license-gate marker for paid modules.
    `source` is recorded in the provenance sidecar and drives the source shield
    and newest-first ordering on the modules page."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ModuleImportError(t("module_import.too_large"))
    tmp_zip = None
    staging = Path(tempfile.mkdtemp(prefix="celerp-mod-import-"))
    try:
        tmp_zip = staging / "_pkg.zip"
        tmp_zip.write_bytes(data)
        try:
            zf = zipfile.ZipFile(tmp_zip)
        except zipfile.BadZipFile:
            raise ModuleImportError(t("module_import.not_zip"))
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
                    raise ModuleImportError(t("module_import.unsafe"))
                # symlink entries carry the link mode in external_attr
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ModuleImportError(t("module_import.symlinks"))
                if rel == PREMIUM_MARKER:
                    # Only the installer itself may write this file (it's how
                    # the license gate decides a module is paid) - a package
                    # that ships it would either fake premium status on a free
                    # module or collide with a genuinely paid install's marker.
                    raise ModuleImportError(t("module_import.reserved_name"))
                unpacked += info.file_size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise ModuleImportError(t("module_import.unpacked_too_large"))
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
        raise ModuleImportError(t("module_import.not_a_folder"))
    init_py = src / "__init__.py"
    if not init_py.exists():
        raise ModuleImportError(t("module_import.folder_not_module"))
    if (src / PREMIUM_MARKER).exists():
        # Same reserved-name refusal as the zip path - only the installer
        # itself may write this file.
        raise ModuleImportError(t("module_import.folder_reserved_name"))
    total = 0
    for p in src.rglob("*"):
        if p.is_symlink():
            raise ModuleImportError(t("module_import.folder_symlinks"))
        if p.is_file():
            total += p.stat().st_size
            if total > MAX_UNPACKED_BYTES:
                raise ModuleImportError(t("module_import.folder_too_large"))
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

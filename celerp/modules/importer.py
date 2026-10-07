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
  - names starting "celerp-" or "celerp_", in any letter case, are reserved
    for Marketplace modules
  - size caps, zip-slip guards, symlink rejection
  - min_celerp_version gate against the running app
  - collision refusal (existing module of the same name must be removed first)

The importer only writes into the module directory; enabling and the restart
are separate, deliberate steps (see the modules UI).
"""
from __future__ import annotations

import ast
import contextlib
import errno
import functools
import logging
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path

from celerp.modules.meta import write_meta

log = logging.getLogger(__name__)

# Compressed and uncompressed caps. Generous for code, hostile to zip bombs.
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024

_RESERVED_PREFIX = "celerp-"
_RESERVED_IMPORT_PREFIX = "celerp_"
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
    prefix rule, which only governs how a NEW package may install.
    """
    if not name or len(name) > _NAME_MAX:
        raise ModuleImportError("Module name missing or too long.")
    ok = all(c.isascii() and (c.isalnum() or c in "-_") for c in name)
    if not ok or not name[0].isalnum():
        raise ModuleImportError(
            "Module name may only contain letters, digits, '-' and '_'."
        )


def is_reserved_name(name: str) -> bool:
    """True for a name in the Marketplace namespace: ``celerp-`` or ``celerp_``,
    in any letter case."""
    return name.lower().startswith((_RESERVED_PREFIX, _RESERVED_IMPORT_PREFIX))


def _validate_name(name: str, *, official: bool = False) -> None:
    _validate_name_chars(name)
    # The celerp- names, in any letter case and in the celerp_ spelling, are
    # reserved for Marketplace modules: an upload or folder import may not use
    # one, and an official Marketplace install uses only the celerp- form.
    if official and not name.startswith(_RESERVED_PREFIX):
        raise ModuleImportError("Official module packages must use the 'celerp-' name prefix.")
    if not official and is_reserved_name(name):
        raise ModuleImportError(
            "Names starting with 'celerp-' or 'celerp_', in any letter case, are reserved "
            "for Marketplace modules. A module of your own needs a different name."
        )


def _bound_names(node) -> list[str]:
    """The names one AST node binds (or deletes) in its scope: definitions,
    imports, assignment targets, global and nonlocal declarations, match
    captures and except-as names, which Python deletes again when the handler
    ends."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in node.names]
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    if isinstance(node, (ast.MatchAs, ast.MatchStar, ast.ExceptHandler)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _manifest_node(tree: ast.Module):
    """The `PLUGIN_MANIFEST = {...}` assignment node in a parsed module, or None.

    Raises :class:`ModuleImportError` when the source binds, changes or reads
    the name anywhere else: Python binds the last assignment and runs every
    change, so the one literal must be the only mention for it to be what the
    module declares."""
    uses = [n for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == "PLUGIN_MANIFEST"
            or "PLUGIN_MANIFEST" in _bound_names(n)]
    node = next((n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "PLUGIN_MANIFEST"
                         for t in n.targets)), None)
    if node is not None:  # a later star import may rebind it
        uses += [n for n in tree.body if isinstance(n, ast.ImportFrom)
                 and any(a.name == "*" for a in n.names) and n.lineno > node.lineno]
    if node is None and not uses:
        return None
    if node is None or len(node.targets) != 1 or uses != [node.targets[0]]:
        raise ModuleImportError(
            "PLUGIN_MANIFEST must be bound once, as one top-level literal, "
            "and never changed or used elsewhere in __init__.py.")
    return node


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
        return _manifest_node(tree) is not None
    except ModuleImportError:
        return True  # it declares one; reading it refuses the module
    except Exception:
        return False


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
    used (install, migrations, purge, backup attribution). *installed* is the
    other modules' prefixes, read from MODULE_DIR when not given.
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
            raise ModuleImportError(
                "This module declares migrations, so its PLUGIN_MANIFEST must set a "
                '"table_prefix" naming the tables it owns (for example "acme_").'
            )
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
        raise ModuleImportError(
            f'Module "{name}" owns tables, so its PLUGIN_MANIFEST must set "company_backup" to a '
            'mapping of each table to "include" or "exclude" '
            f'(for example {{"{prefix}things": "include"}}).'
        )
    for table, how in declared.items():
        if not isinstance(table, str) or not TABLE_NAME.fullmatch(table):
            raise ModuleImportError(f'company_backup names "{table}", which is not a table name.')
        if not table.startswith(prefix):
            raise ModuleImportError(
                f'company_backup names "{table}", which does not begin with the table_prefix "{prefix}".')
        if how not in ("include", "exclude"):
            raise ModuleImportError(
                f'company_backup must say "include" or "exclude" for "{table}", not "{how}".')


def _module_dir() -> Path:
    raw = os.environ.get("MODULE_DIR", "")
    first = raw.split(",")[0].strip()
    if not first:
        raise ModuleImportError("This install has no module directory configured.")
    d = Path(first)
    # A sideload must never land in a bundled dir, which holds the default
    # modules. Refuse rather than write into it.
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
    tree under the live module name. It waits for any install in progress
    (_one_install_at_a_time), whose checks read what is on disk.
    """
    from celerp.modules.loader import is_first_party

    _validate_name_chars(name)
    with _one_install_at_a_time():
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
                raise ModuleImportError(f"Could not remove the module: {exc}")
            shutil.rmtree(grave, ignore_errors=True)
            removed = True
    if not removed:
        raise ModuleImportError(f"Module '{name}' is not installed.")


@contextlib.contextmanager
def _one_install_at_a_time():
    """Run the block while no other install, in any process, is in its own.

    What an install is checked against (the names and table prefixes already on
    disk) only stays true until the package lands if nothing else lands first."""
    path = _module_dir() / ".install.lock"
    with open(path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _finish(staged: Path, manifest: dict, *, official: bool = False,
            premium: bool = False, source: str = "sideloaded",
            expected: tuple[str, str] | None = None) -> dict:
    name = str(manifest.get("name", ""))
    if expected is not None and (name, str(manifest.get("version", ""))) != expected:
        raise ModuleImportError("The downloaded package does not match the requested module.")
    _validate_name(name, official=official)
    _check_min_version(manifest)
    with _one_install_at_a_time():
        return _land(staged, manifest, name, premium=premium, source=source)


def _land(staged: Path, manifest: dict, name: str, *, premium: bool, source: str) -> dict:
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
    # A per-call random suffix keeps a landing dir left by a crashed install
    # from ever being reused.
    landing = target.parent / f".{name}.incoming-{uuid.uuid4().hex}"
    try:
        shutil.rmtree(landing, ignore_errors=True)
        shutil.copytree(staged, landing)
        os.replace(landing, target)
    except OSError as exc:
        shutil.rmtree(landing, ignore_errors=True)
        # A folder copied in by hand can appear between _target_for()'s check
        # and this replace. os.replace onto a populated dir raises
        # FileExistsError (EEXIST) or, on Linux, OSError(ENOTEMPTY) - both mean
        # "already there", so surface the same friendly message.
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
                     premium: bool = False, source: str = "sideloaded",
                     expected: tuple[str, str] | None = None) -> dict:
    """Validate and install a module from zip bytes. Returns manifest summary.

    `official` is set ONLY by the marketplace installer (relay-authenticated
    download): it flips the celerp- prefix rule from forbidden to required.
    `premium` drops the license-gate marker for paid modules.
    `source` is recorded in the provenance sidecar and drives the source shield
    and newest-first ordering on the modules page.
    `expected` is the (name, version) the package must declare; any other
    package is refused before anything lands."""
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
                           premium=premium, source=source, expected=expected)
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

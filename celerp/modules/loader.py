# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Module loader — scans DATA_DIR/modules/, imports enabled modules,
registers slots, wires API and UI routes.

Admission
---------
Every enabled module passes one static preflight, :func:`admit_modules`, before
any code it ships runs: its manifest literal, folder name, reserved-name rules,
Celerp version, table prefix, route sources, migrations package and, for a
module that is not first-party, its protected imports and premium license. The
migration phase and the loader both consume that verdict, so a refused module
runs nothing.

Revenue protection
------------------
The loader enforces that no third-party module imports protected BSL internals
(_PROTECTED_BSL_INTERNALS). If a module imports any of these, it is rejected
with a clear error that names the violation and links to the license and the
sanctioned alternative.

Module authors who need AI should use celerp.modules.api (public, BSL) —
NOT celerp.ai.* directly.

Startup sequence
----------------
Called from celerp/main.py lifespan:
    admission = admit_modules(module_dir, enabled_modules)
    admission = await run_migration_phase(engine, admission)
    loaded = load_all(module_dir, enabled_modules, admission=admission)
    register_api_routes(app, loaded)
    then records what it runs (celerp.modules.outcome.publish).

Called from ui/app.py after core route setup, admitting only what the API runs:
    admission = admission_as_reported(module_dir, enabled_modules,
                                      reported_by_api(api_url, database_url))
    loaded = load_all(module_dir, enabled_modules, admission=admission)
    register_ui_routes(ui_app, loaded)

A module whose routes fail to register is taken out of that process, with every
module depending on it. A module that is not running has none of its tables on
the shared metadata, so table creation never builds them.
"""
from __future__ import annotations

import ast
import fnmatch
import functools
import hashlib
import importlib
import importlib.machinery
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from celerp.modules.importer import (
    _RESERVED_PREFIX, PREMIUM_MARKER, ModuleImportError, _bound_names, _check_min_version,
    _read_manifest as _read_literal_manifest, _validate_name, _validate_name_chars,
    _validate_table_prefix,
)
from celerp.modules.license import check_license, exchange_api_key_for_jwt, is_premium_path
from celerp.modules.meta import META_FILENAME, read_meta
from celerp.modules.slots import (
    SLOT_NAMES, register as register_slot, resolve_handler,
    unregister_module as unregister_module_slots,
)
from celerp.services.app_paths import is_app_local_path
from celerp.services.permissions import is_permission_key

log = logging.getLogger(__name__)

# First-party BSL internals that third-party modules are not allowed to import
# (licensing boundary). Module authors use celerp.modules.api instead.
_PROTECTED_BSL_INTERNALS: frozenset[str] = frozenset({
    "celerp.session_gate",
    "celerp.ai.service",
    "celerp.ai.quota",
    "celerp.gateway",
    "celerp.connectors",
})

_BSL_DOCS_URL = "https://celerp.com/licenses/bsl"
_MODULE_AI_API_URL = "https://celerp.com/docs/modules/ai-api"

# First-party bundled module directories — BSL import restrictions do NOT apply here.
# Third-party modules installed by users live elsewhere (DATA_DIR/modules/).
#
# Path(__file__) is celerp/modules/loader.py — go up two levels to the package root.
# This resolves correctly in both dev (repo root) and installed (site-packages/celerp/…)
# layouts because default_modules/ is installed alongside the celerp package.
#
# Electron installs seed default_modules/ into DATA_DIR/modules/ (outside APP_DIR).
# The Electron main process sets CELERP_TRUSTED_MODULE_DIRS to the original source
# directory so the loader can recognise seeded copies as first-party trusted modules.
BUNDLED_SOURCE_DIR = Path(__file__).resolve().parent.parent.parent / "default_modules"


def _resolve_bundled_dirs() -> tuple[Path, ...]:
    base = BUNDLED_SOURCE_DIR
    extra_raw = os.environ.get("CELERP_TRUSTED_MODULE_DIRS", "")
    extras = [Path(p.strip()).resolve() for p in extra_raw.split(",") if p.strip()]
    return tuple({base.resolve(), *extras})


_BUNDLED_MODULES_DIRS: tuple[Path, ...] = _resolve_bundled_dirs()


def is_bundled_dir(path: Path) -> bool:
    """True if `path` is (or sits inside) a bundled source module dir.

    This is a location predicate, not a trust decision (first-party trust is by
    content - see is_first_party). It is the sole source of truth for the rule
    that an import must never write into the shipped source tree, shared by the
    importer guard and the writable-dir helper.
    """
    try:
        rp = path.resolve()
    except OSError:
        return False
    for bundled in _BUNDLED_MODULES_DIRS:
        try:
            b = bundled.resolve()
        except OSError:
            continue
        if rp == b or b in rp.parents:
            return True
    return False


def writable_module_dir() -> Path:
    """The dedicated writable drop-in for imported modules: data_dir/modules.

    Kept distinct from the read-only bundled default_modules/ tree so a sideload
    never lands among first-party modules. Created on demand; raises OSError on a
    read-only data dir so callers can fall back honestly."""
    from celerp.config import settings
    d = settings.data_dir / "modules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundled_module_dirs(root: Path) -> list[Path]:
    """The module trees shipped under the package root *root*: the default (core)
    modules, then the premium (opt-in add-on) ones. Either may be absent."""
    return [root / "default_modules", root / "premium_modules"]


def with_writable_module_dir(module_dir_env: str | None) -> str:
    """Ensure a launch path's MODULE_DIR writes imports to a safe location.

    An unset MODULE_DIR (None: a bare `uvicorn` dev run) means the bundled trees
    that exist, as `celerp start` gives them; one set to "" means no module trees
    and is returned as is. The importer installs into
    MODULE_DIR.split(",")[0]. If that first entry is a bundled/trusted dir (the
    dev/CLI footgun: MODULE_DIR=default_modules), a writable data-dir drop-in is
    prepended so imports land there, with the bundled dir kept on the path for
    default discovery. An already-safe first entry is returned unchanged."""
    if module_dir_env is None:
        module_dir_env = ",".join(
            str(d) for d in bundled_module_dirs(BUNDLED_SOURCE_DIR.parent) if d.exists())
    entries = [e.strip() for e in module_dir_env.split(",") if e.strip()]
    if not entries or not is_bundled_dir(Path(entries[0])):
        return module_dir_env
    try:
        writable = writable_module_dir()
    except OSError:
        return module_dir_env
    rest = [e for e in entries if Path(e).resolve() != writable.resolve()]
    return ",".join([str(writable), *rest])


# Files that are never copied into a seeded module dir and never enter the
# content digest: bytecode caches, VCS metadata, test-run tooling artifacts
# (a coverage run drops .coverage next to the source, pytest drops .pytest_cache),
# and the runtime sidecars the app itself writes into a module folder after it is
# installed (the meta sidecar and the premium marker). None of these are module
# source - they are never imported by the loader - so hashing them would demote a
# first-party module purely because a developer ran its tests. One tuple feeds two
# derivations - the importer's copy-ignore and the content digest - so the copied
# set and the hashed set cannot drift (DRY). META_FILENAME / PREMIUM_MARKER are
# imported, never re-typed here, so renaming either constant cannot silently desync
# this exclusion set. The lock generator digests only git-tracked files, so these
# gitignored artifacts never enter the committed lock either - excluding them here
# keeps the runtime digest in agreement with it.
_DIGEST_EXCLUDE_GLOBS: tuple[str, ...] = (
    "__pycache__", "*.pyc", ".git", ".github",
    ".coverage", ".coverage.*", "htmlcov", ".pytest_cache",
    META_FILENAME, PREMIUM_MARKER,
)


def _is_excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in _DIGEST_EXCLUDE_GLOBS)


def module_content_digest(pkg_path: Path) -> str | None:
    """Deterministic sha256 over a module's source content, or None if it cannot
    be read.

    Excludes _DIGEST_EXCLUDE_GLOBS (bytecode, VCS metadata, runtime sidecars) so a
    seeded copy hashes identically to the shipped source it was copied from. The
    walk never follows a symlink and rejects any symlinked component outright
    (returning None) rather than dereferencing it, so a symlink escaping pkg_path
    cannot inline foreign bytes into the hash. Any OSError yields None - fail
    closed, so an unreadable tree is never mistaken for a first-party match.

    Relative paths are hashed in POSIX form and line endings are normalised to
    LF before hashing, so the same shipped source produces the same digest on
    every platform (a Windows checkout that lands CRLF, or a build there, still
    matches a lock generated on Linux). The generator digests the same way, so
    the committed lock and the running content agree regardless of platform.
    """
    entries: list[tuple[str, str]] = []
    try:
        for root, dirs, files in os.walk(pkg_path, followlinks=False):
            root_p = Path(root)
            dirs[:] = [d for d in dirs if not _is_excluded(d)]
            for d in dirs:
                if (root_p / d).is_symlink():
                    return None
            for fname in files:
                if _is_excluded(fname):
                    continue
                fpath = root_p / fname
                if fpath.is_symlink():
                    return None
                rel = fpath.relative_to(pkg_path).as_posix()
                data = fpath.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                entries.append((rel, hashlib.sha256(data).hexdigest()))
    except OSError as exc:
        log.warning("Cannot digest module at %s: %s", pkg_path, exc)
        return None
    digest = hashlib.sha256()
    for rel, file_hash in sorted(entries):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _lock_path() -> Path:
    """Path to the committed first-party lock, installed beside default_modules/.
    A separate function so tests can patch it without touching the read logic."""
    return BUNDLED_SOURCE_DIR / "first_party.lock.json"


@functools.lru_cache(maxsize=1)
def _first_party_lock() -> dict[str, str]:
    """The committed {module_name: content_digest} map, or {} if it cannot be read
    or is malformed (fail closed: no module is trusted rather than a wrong one).

    Cached so the fail-closed log line is emitted once per process. Trust flows
    only from this committed file - never from CELERP_TRUSTED_MODULE_DIRS or any
    directory listing - so no environment variable can grant first-party trust.
    """
    path = _lock_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.error("First-party lock missing at %s; no module will be trusted.", path)
        return {}
    except (OSError, ValueError) as exc:
        log.error("First-party lock unreadable at %s: %s; no module will be trusted.", path, exc)
        return {}
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        log.error("First-party lock at %s is not a name->digest map; no module will be trusted.", path)
        return {}
    if not raw:
        log.warning("First-party lock at %s is empty; no module treated as first-party.", path)
    return raw


# Names already warned about a lock mismatch this process. is_first_party is a hot
# predicate - called on every module scan (boot, each /modules render, the delete
# guard) - so without this a single demoted module spams one WARNING per call.
# One line per module per process is enough to surface the state; the bell carries
# the durable, user-facing notice (see celerp.modules.demotion).
_demotion_warned: set[str] = set()


def is_first_party(pkg_path: Path) -> bool:
    """True only if pkg_path's folder name is in the committed lock AND its live
    content digest matches the locked digest.

    Trust is by content, never by folder name or filesystem location: an impostor
    dropped into any module search dir is not trusted, and a modified default is
    demoted (with a warning) so it stops skipping the BSL import checks.
    """
    lock = _first_party_lock()
    expected = lock.get(pkg_path.name)
    if expected is None:
        return False
    digest = module_content_digest(pkg_path)
    if digest is None:
        return False
    if digest != expected:
        if pkg_path.name not in _demotion_warned:
            _demotion_warned.add(pkg_path.name)
            log.warning(
                "Module %r content does not match its first-party lock entry; "
                "treating it as not first-party.", pkg_path.name)
        return False
    return True


def first_party_names() -> frozenset[str]:
    """The module names the committed lock claims as first-party.

    A scanned module whose name is here but whose content no longer matches the
    lock is a demoted default - the scan reports that as a per-module fact so the
    UI can surface it without keeping any cross-render state."""
    return frozenset(_first_party_lock())


def demoted_first_party(enabled: set[str]) -> list[str]:
    """Sorted names of enabled modules the lock claims as first-party but whose
    live content no longer matches (demoted). Mirrors the /modules scan's
    per-module verdict via the same content check, so the boot-time bell notice
    and the page agree. Catches a genuine tamper whether or not the module still
    loads - it asks the filesystem, not the loaded set (a demoted default that
    then trips the BSL checks never reaches the loaded manifests)."""
    lock_names = first_party_names()
    out: list[str] = []
    for name in sorted(enabled & lock_names):
        path = resolve_module_path(name)
        if path is not None and not is_first_party(path):
            out.append(name)
    return out


def _module_candidates(
    name: str, module_dir: str | Path | None = None,
) -> list[Path]:
    """Installed copies of *name* in MODULE_DIR order. A name that is not a plain module
    name (a path, '.', '..') has none, so it can never resolve to a folder outside it."""
    try:
        _validate_name_chars(name)
    except ModuleImportError:
        return []
    raw = os.environ.get("MODULE_DIR", "") if module_dir is None else str(module_dir)
    out: list[Path] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        candidate = Path(entry) / name
        if candidate.is_dir() and (candidate / "__init__.py").exists():
            out.append(candidate)
    return out


def module_search_path() -> str:
    """Every directory an installed module can live in, as a MODULE_DIR-style string:
    the MODULE_DIR entries (administrative precedence) followed by the bundled and
    trusted dirs. Lets a caller locate an installed module's data even when the
    module system is off or the module is not enabled."""
    entries = [e.strip() for e in os.environ.get("MODULE_DIR", "").split(",") if e.strip()]
    return ",".join([*entries, *(str(d) for d in _BUNDLED_MODULES_DIRS)])


def resolve_module_path(
    name: str, module_dir: str | Path | None = None,
) -> Path | None:
    """The first installed copy of *name*, preserving administrative precedence."""
    candidates = _module_candidates(name, module_dir)
    return candidates[0] if candidates else None


def resolve_runtime_module_path(
    name: str, module_dir: str | Path | None = None,
) -> Path | None:
    """The copy safe to execute.

    For a current first-party name, prefer any candidate whose contents match the
    committed lock. This lets a current bundled copy self-heal a stale writable
    shadow left by an old backup restore without deleting or rewriting that shadow.
    Third-party names and installations with no verified copy retain normal
    first-entry precedence.
    """
    candidates = _module_candidates(name, module_dir)
    if name in first_party_names():
        for candidate in candidates:
            if is_first_party(candidate):
                return candidate
    return candidates[0] if candidates else None


def _purge_pycache(pkg_path: Path) -> None:
    """Remove every __pycache__ under pkg_path before the module is imported.

    The content digest excludes *.pyc, so a stale or tampered bytecode cache with a
    matching header would otherwise be executed in preference to recompiling the
    just-verified source. Purging first guarantees the bytes CPython runs are the
    bytes that were content-verified. Best effort: a purge failure is logged, not
    fatal, and Python still validates cache headers against source mtime.
    """
    try:
        for cache in pkg_path.rglob("__pycache__"):
            if cache.is_dir() and not cache.is_symlink():
                shutil.rmtree(cache, ignore_errors=True)
    except OSError as exc:
        log.warning("Could not purge bytecode cache under %s: %s", pkg_path, exc)

# Loaded manifests - populated by load_all()
_loaded: list[dict] = []
# The admission record of each loaded module, by name - populated by load_all()
_admitted: dict[str, "AdmittedModule"] = {}
# The tables each module's code added to the shared metadata, by name. Kept
# across load_all passes: a module imported once is not imported (and its tables
# not added) again.
_module_tables: dict[str, set[str]] = {}
# Tables taken off the shared metadata because the module behind them is not running
_removed_tables: set[str] = set()
# The routes each running module registered, by name, so taking it out removes them
_module_routes: dict[str, list] = {}

# Proprietary cloud components folded into core: wired directly at app construction (celerp/main.py,
# ui/app.py), never loaded as pluggable/replaceable modules.
CORE_FOLDED: frozenset[str] = frozenset({"celerp-ai", "celerp-backup", "celerp-connectors"})


class ModuleLoadError(Exception):
    """Raised (and caught) when a module fails validation."""


def read_manifest(pkg_path: Path) -> dict:
    """Return PLUGIN_MANIFEST from a module's __init__.py via AST literal_eval.

    No import side effects. Returns an empty dict if the file is missing or the
    manifest is absent or unparseable. This is the single source for reading a
    module's declared manifest without importing it.
    """
    init_file = pkg_path / "__init__.py"
    try:
        tree = ast.parse(init_file.read_text())
    except Exception:
        return {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PLUGIN_MANIFEST":
                try:
                    return dict(ast.literal_eval(node.value))
                except Exception:
                    return {}
    return {}


def _manifest_depends_on(value) -> list[str]:
    """The validated ``depends_on`` list (None means no dependencies)."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(dep, str) and dep.strip() for dep in value):
        raise ModuleLoadError(
            f"'depends_on' must be a list of module name strings, not {value!r}.")
    return list(value)


def _validated_manifest(raw) -> dict:
    """Check a module's PLUGIN_MANIFEST once and return the copy every later
    read uses, so a malformed value is refused with its reason here instead of
    raising out of the load pass at some later raw read. ``slots`` is
    normalized to a dict (None means none) and ``depends_on`` to a list; the
    contents of ``slots`` are checked by :func:`_validate_slots`.
    """
    if not isinstance(raw, dict):
        raise ModuleLoadError(
            f"PLUGIN_MANIFEST must be a dict, not {type(raw).__name__}.")
    missing = [f for f in ("name", "version") if not raw.get(f)]
    if missing:
        raise ModuleLoadError(f"Manifest missing required fields: {', '.join(missing)}.")
    for field in ("name", "version"):
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ModuleLoadError(
                f"'{field}' must be a non-empty string, not {type(raw[field]).__name__}.")
    for key in ("api_routes", "ui_routes"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise ModuleLoadError(
                f"'{key}' must be a dotted module path string, not {type(value).__name__}.")
    slots_manifest = raw.get("slots")
    if slots_manifest is None:
        slots_manifest = {}
    if not isinstance(slots_manifest, dict):
        raise ModuleLoadError(
            f"'slots' must be a dict of slot name to entries, "
            f"not {type(slots_manifest).__name__}.")
    return {**raw, "slots": slots_manifest,
            "depends_on": _manifest_depends_on(raw.get("depends_on"))}


def _dependency_order(
    deps_by_name: dict[str, list[str]], enabled: set[str], installed: set[str],
    errors: dict[str, str],
) -> list[str]:
    """The names in ``deps_by_name`` ordered so dependencies come before
    dependents, ties broken by name so the order is deterministic.

    A name whose dependency is not enabled, not installed, refused (in
    ``errors``) or part of a cycle is left out, its reason added to ``errors``.
    """
    result: list[str] = []
    done: set[str] = set()       # fully resolved and appended to result
    skipped: set[str] = set(errors)
    on_stack: set[str] = set()   # currently being visited (for cycle detection)

    def _skip(name: str, reason: str) -> None:
        log.warning("Module %r skipped: %s", name, reason)
        errors.setdefault(name, reason)
        skipped.add(name)
        on_stack.discard(name)

    def _visit(name: str) -> None:
        if name in done or name in skipped:
            return
        if name in on_stack:
            # A back-edge to a module still being visited: a dependency cycle.
            # Skip it with a clear error rather than accepting an order that
            # cannot actually satisfy the deps.
            _skip(name, "Part of a dependency cycle.")
            return
        on_stack.add(name)
        for dep in deps_by_name[name]:
            if dep not in enabled:
                return _skip(name, f"Requires {dep!r}, which is not enabled.")
            if dep not in installed:
                return _skip(name, f"Requires {dep!r}, which is not installed.")
            if dep in deps_by_name:
                _visit(dep)
            if dep in skipped:
                return _skip(name, f"Requires {dep!r}, which failed to load.")
        on_stack.discard(name)
        done.add(name)
        result.append(name)

    for name in sorted(deps_by_name):
        _visit(name)
    return result


@dataclass(frozen=True)
class AdmittedModule:
    """A module that passed admission: the copy to run, its declared manifest
    (validated) and its first-party verdict."""
    name: str
    path: Path
    manifest: dict
    first_party: bool


@dataclass(frozen=True)
class Admission:
    """The verdict on every enabled module: those admitted, in dependency order,
    and the reason each refused one was refused."""
    admitted: list[AdmittedModule]
    refused: dict[str, str]

    def without(self, failed: dict[str, str]) -> "Admission":
        """This admission with ``failed`` (name -> reason) refused, along with
        every admitted module that depends on one of them, directly or not."""
        refused = {**self.refused, **failed}
        admitted: list[AdmittedModule] = []
        for module in self.admitted:
            if module.name in refused:
                continue
            dep = next((d for d in module.manifest["depends_on"] if d in refused), None)
            if dep is not None:
                refused[module.name] = f"Requires {dep!r}, which failed to load."
                continue
            admitted.append(module)
        return Admission(admitted, refused)


def _is_official_name(name: str, pkg_path: Path) -> bool:
    """True when a module may carry the reserved ``celerp-`` name: the committed
    lock claims it, the marketplace installed it, or it ships in a license-gated
    premium tree. Anything else claiming the prefix is refused, exactly as the
    importer refuses a sideload that claims it."""
    if not name.startswith(_RESERVED_PREFIX):
        return False
    return (name in first_party_names()
            or read_meta(pkg_path).get("source") == "marketplace"
            or any(p.name == "premium_modules" for p in pkg_path.parents))


def _inside(path: Path, root: Path) -> bool:
    """True when ``path`` resolves (symlinks and '..' collapsed) inside ``root``."""
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(root))


def module_migration_files(pkg_path: Path, migrations_pkg) -> list[Path]:
    """The migration files a module's ``migrations`` package holds, in run order.

    ``migrations_pkg`` must be a dotted package path of identifiers, resolved
    under the module folder; the package directory and every file in it must
    resolve inside the module folder, so neither an absolute path nor a symlink
    can point the runner at code outside the module. Files starting with ``_``
    are skipped. An absent package is no migrations. Raises
    :class:`ModuleLoadError` on any violation.
    """
    if (not isinstance(migrations_pkg, str)
            or not all(part.isidentifier() for part in migrations_pkg.split("."))):
        raise ModuleLoadError(
            f"'migrations' must be a dotted package path inside the module, "
            f"not {migrations_pkg!r}.")
    mig_dir = pkg_path.joinpath(*migrations_pkg.split("."))
    if not _inside(mig_dir, pkg_path):
        raise ModuleLoadError(
            f"'migrations' package {migrations_pkg!r} resolves outside the module folder.")
    if not mig_dir.is_dir():
        return []
    files = sorted(p for p in mig_dir.glob("*.py")
                   if p.is_file() and not p.name.startswith("_"))
    for path in files:
        if not _inside(path, pkg_path):
            raise ModuleLoadError(
                f"Migration file {path.name!r} resolves outside the module folder.")
    return files


def _check_route_source(pkg_path: Path, manifest: dict, kind: str) -> None:
    """Prove, without importing it, that the module's ``{kind}_routes`` names a
    source file inside the module that defines ``setup_{kind}_routes`` or
    imports it from the module's own code, as a plain top-level def that is not
    async (_check_source_call_style). Registration later proves the resolved
    callable itself (:func:`_check_owned_callable`)."""
    key = f"{kind}_routes"
    dotted = manifest.get(key)
    if not dotted:
        return
    setup = f"setup_{kind}_routes"
    source = _module_source_file(pkg_path, dotted)
    if source is None or not _inside(source, pkg_path):
        raise ModuleLoadError(
            f"{key} {dotted!r} does not resolve to source inside the module.")
    tree = _parse_source(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == setup:
            break
        if isinstance(node, ast.ImportFrom) and any(
                (alias.asname or alias.name) == setup for alias in node.names):
            if _resolve_local_import(pkg_path, source, node.module, node.level) is None:
                raise ModuleLoadError(
                    f"{key} {dotted!r} takes {setup} from outside the module.")
            break
    else:
        raise ModuleLoadError(f"{key} {dotted!r} does not define {setup}.")
    _check_source_call_style(pkg_path, source, f"{key} setup", f"{dotted}:{setup}", awaited=False)


def _module_entry_files(pkg_path: Path, manifest: dict) -> list[Path]:
    """Every source file core may execute for this module: its __init__.py, its
    route modules, every callable-slot module and every migration file."""
    files = [pkg_path / "__init__.py"]
    for key in ("api_routes", "ui_routes"):
        if manifest.get(key):
            files.append(_module_source_file(pkg_path, manifest[key]))
    for slot_name, contribution in manifest["slots"].items():
        if slot_name not in _CALLABLE_SLOTS:
            continue
        key, _awaited = _CALLABLE_SLOTS[slot_name]
        for item in contribution if isinstance(contribution, list) else [contribution]:
            if isinstance(item, dict) and key in item:
                files.append(_owned_callable_source(
                    pkg_path, f"Slot {slot_name!r}", item[key]))
    if manifest.get("migrations"):
        files.extend(module_migration_files(pkg_path, manifest["migrations"]))
    return files


def _handler_names(manifest: dict) -> set[str]:
    """The names of every callable core will call in the module: its route
    setup functions and each callable slot's function."""
    names = {f"setup_{kind}_routes" for kind in ("api", "ui") if manifest.get(f"{kind}_routes")}
    for slot_name, contribution in manifest["slots"].items():
        if slot_name in _CALLABLE_SLOTS:
            key = _CALLABLE_SLOTS[slot_name][0]
            names |= {item[key].split(":")[1] for item in (
                contribution if isinstance(contribution, list) else [contribution])
                if isinstance(item, dict) and isinstance(item.get(key), str) and ":" in item[key]}
    return names


# Top-level package names Celerp itself ships, and the prefix of the packages
# inside official modules (celerp_inventory, ...): no other module answers to them.
_RESERVED_IMPORT_NAMES = frozenset({"celerp", "ui", "default_modules", "premium_modules"})
_RESERVED_IMPORT_PREFIX = "celerp_"


def _import_roots(name: str, pkg_path: Path) -> list[str]:
    """Every top-level name the module answers to once its folder and the
    folder's parent are on sys.path: its own name and each package or source
    file directly inside it."""
    shipped = {entry.stem for entry in pkg_path.iterdir()
               if (entry.is_dir() and (entry / "__init__.py").is_file())
               or (entry.suffix == ".py" and entry.name != "__init__.py")}
    return sorted({name} | shipped)


def _module_location(mod) -> str | None:
    """Where an imported module's code lives, or None (built in)."""
    return getattr(mod, "__file__", None) or next(iter(getattr(mod, "__path__", None) or []), None)


def _declares_manifest(folder: Path) -> bool:
    try:
        _read_literal_manifest((folder / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, ModuleImportError):
        return False
    return True


def _module_homes(pkg_path: Path) -> list[Path]:
    return [pkg_path.parent, *(Path(e) for e in module_search_path().split(",") if e)]


def _is_module_code(location: str | None, homes: list[Path]) -> bool:
    """True when *location* is a Celerp module's own code: inside a module
    directory, a module folder, or a package directly inside one."""
    if not location:
        return False
    path = Path(location)
    folder = path.parent if path.suffix else path
    return (any(_inside(path, home) for home in homes)
            or _declares_manifest(folder) or _declares_manifest(folder.parent))


def _check_import_names(name: str, pkg_path: Path, *, official: bool) -> None:
    """Refuse a module that would answer to a package name the standard library,
    Celerp or an installed package already uses: loading it would replace that
    package for everything else in the process. Only another Celerp module may
    already hold the name. Raises :class:`ModuleLoadError`."""
    homes = _module_homes(pkg_path)
    elsewhere = [p for p in sys.path if not any(_inside(Path(p or "."), h) for h in homes)]
    for root in _import_roots(name, pkg_path):
        if root in sys.modules:
            taken = not _is_module_code(_module_location(sys.modules[root]), homes)
        else:
            spec = importlib.machinery.PathFinder.find_spec(root, elsewhere)
            taken = bool(spec and spec.origin) and not _is_module_code(spec.origin, homes)
        if (taken or root in _RESERVED_IMPORT_NAMES or root in sys.stdlib_module_names
                or (root.startswith(_RESERVED_IMPORT_PREFIX) and not official)):
            raise ModuleLoadError(
                f"The package name {root!r} is already used by Celerp, Python or an "
                f"installed package; the module must use its own.")


def _declared_manifest(pkg_path: Path) -> dict:
    """The validated PLUGIN_MANIFEST literal in a module's __init__.py, read
    without importing anything. Raises :class:`ModuleLoadError`."""
    try:
        raw = _read_literal_manifest(
            (pkg_path / "__init__.py").read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModuleLoadError(f"Cannot read __init__.py ({type(exc).__name__}).")
    except ModuleImportError as exc:
        raise ModuleLoadError(str(exc))
    return _validated_manifest(raw)


def _admission_checks(name: str, pkg_path: Path) -> AdmittedModule:
    """Every static rule a module must pass before any of its code runs.
    Raises :class:`ModuleLoadError` (or the importer's ModuleImportError) with
    the reason."""
    manifest = _declared_manifest(pkg_path)
    if manifest["name"] != name:
        raise ModuleLoadError(
            f"Manifest name {manifest['name']!r} does not match its folder {name!r}.")
    official = _is_official_name(name, pkg_path)
    _validate_name(name, official=official)
    _check_min_version(manifest)
    _validate_table_prefix(name, manifest)
    for kind in ("api", "ui"):
        _check_route_source(pkg_path, manifest, kind)
    _check_slot_contracts(pkg_path, manifest["slots"])
    _check_import_names(name, pkg_path, official=official)
    entry_files = _module_entry_files(pkg_path, manifest)
    _check_dynamic_writes(pkg_path, entry_files, _handler_names(manifest) | {"PLUGIN_MANIFEST"})
    first_party = is_first_party(pkg_path)
    if not first_party:
        violations: set[str] = set()
        for entry in entry_files:
            violations |= _scan_protected_imports(pkg_path, entry)
        if violations:
            raise ModuleLoadError(_bsl_violation_message(name, violations))
    return AdmittedModule(name, pkg_path, manifest, first_party)


def _license_refusal(module: AdmittedModule, creds) -> str | None:
    """Why a premium module may not load on this instance, or None.

    Only checked when this instance has a relay identity (it has activated / been
    given a GATEWAY_TOKEN). It verifies even when the live token exchange failed
    (no JWT): check_license still decides from the offline lifetime JWT and the
    grace cache, so a transient startup failure falls back to cached state rather
    than skipping the check. Only a never-activated install skips it.
    """
    if not is_premium_path(module.path):
        return None
    relay_url, instance_jwt, data_dir, instance_id = creds()
    if not relay_url:
        log.debug("Premium module %r: no relay identity (never activated) - "
                  "skipping license check (dev mode)", module.name)
        return None
    if check_license(
        slug=module.name,
        relay_url=relay_url,
        instance_jwt=instance_jwt or "",
        cache_dir=Path(data_dir),
        instance_id=instance_id,
        offline_only=instance_jwt is None,
    ):
        return None
    log.warning("Premium module %r skipped: no valid license", module.name)
    return "Premium module: no valid license."


def _premium_credentials():
    """A resolver for the relay credentials the premium-license gate needs,
    computed lazily and ONCE per admission: the JWT is the same for every
    module, and there must be no network call at all when no premium module is
    present. gateway_token (GATEWAY_TOKEN / GATEWAY_URL on a hosted deploy; set
    by /auth/activate on desktop) is exchanged for a short-lived JWT via
    /auth/token, the same pattern celerp.routers.health uses."""
    cache: dict = {}

    def _resolve() -> tuple[str, str | None, str, str]:
        if not cache:
            from celerp.config import ensure_instance_id, settings as _settings
            from celerp.gateway.state import relay_http_url
            api_key = _settings.gateway_token
            relay_url = relay_http_url() if api_key else ""
            cache["creds"] = (
                relay_url,
                exchange_api_key_for_jwt(relay_url, api_key) if api_key else None,
                os.environ.get("DATA_DIR", "/tmp/celerp-data"),
                # The instance's own canonical id (offline-available): a lifetime
                # license is validated against this via its `sub` claim.
                ensure_instance_id(),
            )
        return cache["creds"]
    return _resolve


def _refuse_shared_import_names(candidates: dict[str, AdmittedModule],
                                refused: dict[str, str]) -> None:
    """Python holds one module per import name, so of two modules answering to
    the same one, the second would run the first's code. First-party modules
    claim their names first, then the rest in name order; a later module whose
    names overlap a claimed one moves from *candidates* to *refused*."""
    claimed: dict[str, str] = {}
    for name in sorted(candidates, key=lambda n: (not candidates[n].first_party, n)):
        roots = _import_roots(name, candidates[name].path)
        owner = next((claimed[r] for r in roots if r in claimed), None)
        if owner is not None:
            shared = sorted(r for r in roots if claimed.get(r) == owner)
            refused[name] = (f"Module {owner!r} also ships {', '.join(map(repr, shared))}; "
                             f"each import name may belong to one module only.")
            log.error("Module %r refused: %s", name, refused[name])
            del candidates[name]
            continue
        claimed.update(dict.fromkeys(roots, name))


def admit_modules(module_dir: str | Path, enabled: set[str]) -> Admission:
    """Decide, without executing any module code, which enabled modules may run.

    The one preflight both the migration phase and the loader consume. Per
    enabled module (core-folded ones excepted) it reads the copy
    resolve_runtime_module_path picks and checks: the manifest is a literal
    that validates; its name matches the folder; the importer's name rules
    (reserved prefix); the Celerp version it needs; the table prefix contract;
    that no package name it answers to is already taken, by Python or by
    another enabled module (_refuse_shared_import_names); that every route
    source lies inside the module and provides its setup function; that no
    code it would execute rebinds a callable core calls (_check_dynamic_writes);
    that the migrations package resolves inside the module; for a
    module that is not first-party, that nothing it would execute imports a
    protected internal; and for a premium module, a valid license. Survivors are
    then put in dependency order, a module whose dependency is missing or
    refused being refused too. A first-party module that fails a rule stops
    startup, as a default module is the product.
    """
    refused: dict[str, str] = {}
    candidates: dict[str, AdmittedModule] = {}
    installed: set[str] = set()
    creds = _premium_credentials()
    for name in sorted(enabled - CORE_FOLDED):
        pkg_path = resolve_runtime_module_path(name, module_dir)
        if pkg_path is None:
            continue
        installed.add(name)
        try:
            module = _admission_checks(name, pkg_path)
        except (ModuleLoadError, ModuleImportError) as exc:
            if name in first_party_names() and is_first_party(pkg_path):
                raise ModuleLoadError(
                    f"Default module {name!r} failed to load: {exc}") from exc
            log.error("Module %r refused: %s", name, exc)
            refused[name] = str(exc)
            continue
        reason = _license_refusal(module, creds)
        if reason:
            refused[name] = reason
            continue
        candidates[name] = module
    _refuse_shared_import_names(candidates, refused)
    order = _dependency_order(
        {n: m.manifest["depends_on"] for n, m in candidates.items()},
        enabled, installed, refused)
    return Admission([candidates[n] for n in order], refused)


# Per-module load failures from the last load_all() run: name -> message.
# The modules UI surfaces these so a broken module fails loudly, not silently.
_load_errors: dict[str, str] = {}


def load_errors() -> dict[str, str]:
    """Return {module_name: error message} for modules that failed to load."""
    return dict(_load_errors)


def loaded_modules() -> list[dict]:
    """Return manifests of all successfully loaded modules."""
    return list(_loaded)


def is_core_folded(pkg_name: str) -> bool:
    """True if a module is a proprietary cloud component folded into core (ai/backup/
    connectors). These are wired directly into the app at construction, so they never
    appear in ``loaded_modules()`` — yet they ARE running whenever the app is up."""
    return pkg_name in CORE_FOLDED


def is_running(pkg_name: str) -> bool:
    """Single source of truth for "is this module active in the process".

    A module is running if the pluggable loader loaded it OR it is a core-folded
    module (wired directly at app construction). Used by /companies/me/modules and
    the setup activating page so folded modules are never reported as failed to
    start — that bug made ai/backup spin forever on the activating screen.
    """
    return any(m["name"] == pkg_name for m in _loaded) or is_core_folded(pkg_name)


def running_version(pkg_name: str) -> str | None:
    """The version of *pkg_name* this process loaded, or None when the loader did not load it.

    The copy on disk can be newer than the running code until the next restart.
    """
    return next((m.get("version") for m in _loaded if m["name"] == pkg_name), None)


def restart_would_load(pkg_name: str) -> bool:
    """True when a server restart would load *pkg_name*.

    That needs the module system on with an installed copy in MODULE_DIR, and an
    enabled list the restart re-reads from the config file: under the supervisor
    ENABLED_MODULES is rebuilt from config on every restart, while outside it a set
    ENABLED_MODULES pins the list and only the names it holds can load."""
    if not _module_candidates(pkg_name):
        return False
    pinned = os.environ.get("ENABLED_MODULES", "")
    if not pinned or os.environ.get("CELERP_SUPERVISED") == "1":
        return True
    return pkg_name in {n.strip() for n in pinned.split(",")}


def module_label(pkg_name: str) -> str:
    """The module's display name from its manifest, or the package name."""
    path = resolve_runtime_module_path(pkg_name, module_search_path())
    meta = read_manifest_metadata(path) if path is not None else {}
    return meta.get("display_name") or meta.get("label") or pkg_name


# Fields to extract from PLUGIN_MANIFEST for display purposes.
# All must be string or list-of-strings literals in __init__.py (safe for ast.literal_eval).
_MANIFEST_DISPLAY_FIELDS: frozenset[str] = frozenset({
    "name", "display_name", "label", "version", "description", "author",
    "depends_on", "license", "min_celerp_version", "table_prefix",
})


def read_manifest_metadata(pkg_path: Path) -> dict:
    """Parse display metadata from a module's __init__.py without importing it.

    Reads PLUGIN_MANIFEST from the package's __init__.py using ast.parse so
    there are no import side effects. Only extracts the fields in
    _MANIFEST_DISPLAY_FIELDS (all plain string/list literals).

    Returns a partial manifest dict. Missing or unparseable fields are omitted.
    Returns an empty dict if the file cannot be found or parsed.
    """
    init_py = pkg_path / "__init__.py"
    if not init_py.exists():
        return {}
    try:
        tree = ast.parse(init_py.read_text())
    except Exception:
        return {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id == "PLUGIN_MANIFEST"):
                continue
            if not isinstance(node.value, ast.Dict):
                return {}
            result: dict = {}
            for key_node, val_node in zip(node.value.keys, node.value.values):
                if not isinstance(key_node, ast.Constant):
                    continue
                field = key_node.value
                if field not in _MANIFEST_DISPLAY_FIELDS:
                    continue
                try:
                    result[field] = ast.literal_eval(val_node)
                except Exception:
                    pass  # Non-literal value (e.g. function call) — skip gracefully
            return result
    return {}


def load_all(
    module_dir: str | Path, enabled: set[str], *, admission: Admission | None = None,
) -> list[dict]:
    """Import the admitted modules and register their slots and locales.

    Args:
        module_dir: Comma-separated paths or single path to module directories.
        enabled: Set of module names that should be loaded.
        admission: The verdict from :func:`admit_modules` when the caller already
            has one (the API process admits once, before its migration phase);
            otherwise the modules are admitted here. A refused module's code is
            never imported, and its reason is reported in :func:`load_errors`.

    Returns:
        List of successfully loaded PLUGIN_MANIFEST dicts.
    """
    _loaded.clear()
    _load_errors.clear()
    _admitted.clear()
    _module_routes.clear()
    # Every core table is on the metadata before any module code runs, so a table
    # a module adds is told apart from one it merely caused to be imported.
    import celerp.models  # noqa: F401
    # Module-contributed i18n catalogs are rebuilt from scratch on every pass,
    # exactly like _loaded above, so a re-scan (a module toggled off, or a
    # catalog changed) never leaves a stale or orphaned catalog behind. Lazy
    # import keeps ui.i18n the lowest leaf (it must not import this module).
    from ui.i18n import clear_registry
    clear_registry()

    if admission is None:
        admission = admit_modules(module_dir, enabled)
    _load_errors.update(admission.refused)

    for d in (Path(e.strip()) for e in str(module_dir).split(",") if e.strip()):
        d_str = str(d)
        if d.exists() and d_str not in sys.path:
            sys.path.insert(0, d_str)

    for module in admission.admitted:
        pkg_name, pkg_path = module.name, module.path
        failed_dep = next(
            (d for d in module.manifest["depends_on"] if not is_running(d)), None)
        if failed_dep is not None:
            _load_errors[pkg_name] = f"Requires {failed_dep!r}, which failed to load."
            continue
        # Each module dir (e.g. default_modules/celerp-inventory/) must be on
        # sys.path so that its inner packages (e.g. celerp_inventory) are
        # importable by importlib.import_module when routes are registered.
        p_str = str(pkg_path)
        if p_str not in sys.path:
            sys.path.insert(0, p_str)
        # Run the source just content-verified, never a stale/tampered .pyc that a
        # matching cache header would execute in preference (the digest omits *.pyc).
        _purge_pycache(pkg_path)
        try:
            with _recording_tables(pkg_name):
                manifest = _load_one(pkg_path, pkg_name, trusted=module.first_party,
                                     declared=module.manifest)
        except ModuleLoadError as exc:
            # A default module IS the product (a boot without documents is not
            # a working app): fail startup naming the module and error.
            # Third-party modules keep load-and-continue; their failure shows
            # as the failed badge in the Modules UI.
            if module.first_party:
                raise ModuleLoadError(
                    f"Default module {pkg_name!r} failed to load: {exc}") from exc
            _load_errors[pkg_name] = str(exc)
            _drop_tables({pkg_name})
            continue
        # Carry the trust decision on the manifest so route registration reads
        # it rather than recomputing (and re-hashing) per module.
        manifest["first_party"] = module.first_party
        _loaded.append(manifest)
        _admitted[pkg_name] = module

    log.info(
        "Module loader complete: %d loaded, %d skipped/rejected",
        len(_loaded),
        len(enabled) - len(_loaded),
    )
    return list(_loaded)


@contextmanager
def _recording_tables(pkg_name: str):
    """Attribute to *pkg_name* every table added to the shared metadata while the
    block runs (its import, its route setup), whether or not the block fails.

    A table the module does not own (core's, another module's) must leave the
    block as it entered: a change (extend_existing columns, constraints or
    indexes, a removal) is undone and the block raises :class:`ModuleLoadError`,
    so the module is taken out instead of reshaping a table it does not own."""
    from celerp.models.base import Base

    before = set(Base.metadata.tables)
    own = _module_tables.get(pkg_name, set())
    shapes = {key: _table_shape(table) for key, table in Base.metadata.tables.items()
              if key not in own}
    try:
        yield
    finally:
        _module_tables.setdefault(pkg_name, set()).update(set(Base.metadata.tables) - before)
        altered = sorted(key for key, shape in shapes.items()
                         if _restore_table(Base.metadata, shape))
        if _removed_tables:
            _sweep_removed_tables()
        if altered:
            raise ModuleLoadError(
                f"Changes table(s) it does not own: {', '.join(altered)}.")


# What a query reads from each column, beyond the column object itself.
_COLUMN_STATE = ("name", "key", "type", "nullable", "server_default", "primary_key",
                 "default", "onupdate", "server_onupdate")


def _table_shape(table) -> tuple:
    return (table, (table.name, table.schema, table.fullname), list(table.columns),
            [tuple(getattr(c, a) for a in _COLUMN_STATE) for c in table.columns],
            set(table.constraints), set(table.indexes))


def _restore_table(metadata, shape: tuple) -> bool:
    """Put a table back on *metadata* exactly as :func:`_table_shape` saw it.
    True when anything had changed."""
    table, identity, columns, states, constraints, indexes = shape
    changed = (table.name, table.schema, table.fullname) != identity
    table.name, table.schema, table.fullname = identity
    for key in [k for k, t in metadata.tables.items() if t is table and k != table.key]:
        changed = True
        dict.pop(metadata.tables, key)
    if metadata.tables.get(table.key) is not table:
        changed = True
        metadata._add_table(table.name, table.schema, table)
    for column, state in zip(columns, states):
        if tuple(getattr(column, a) for a in _COLUMN_STATE) != state:
            changed = True
            for attr, value in zip(_COLUMN_STATE, state):
                setattr(column, attr, value)
    by_key = {c.key: c for c in columns}
    kept = set(map(id, columns))
    for column in [c for c in table.columns if id(c) not in kept]:
        changed = True
        if column.key in by_key:
            table._columns.replace(by_key[column.key])
        else:
            table._columns.remove(column)
    for column in columns:
        if table.columns.get(column.key) is not column:
            changed = True
            table._columns.add(column)
    for current, saved in ((table.constraints, constraints), (table.indexes, indexes)):
        if current != saved:
            changed = True
            current.intersection_update(saved)
            current.update(saved)
    return changed


def _drop_tables(names: set[str]) -> None:
    """Take the tables these modules' code added off the shared metadata, so table
    creation (create_all) covers only modules that are running."""
    for name in names:
        _removed_tables.update(_module_tables.get(name, set()))
    _sweep_removed_tables()


def _sweep_removed_tables() -> None:
    """Keep every removed table off the shared metadata, together with any table
    holding a foreign key into one (it cannot be created without it), whenever
    that table was added."""
    from celerp.models.base import Base

    while True:
        referencing = {
            key for key, table in Base.metadata.tables.items()
            if key not in _removed_tables and any(
                fk.target_fullname.rsplit(".", 1)[0] in _removed_tables
                for fk in table.foreign_keys)}
        if not referencing:
            break
        log.warning("Tables %s reference tables of a module that is not running; not created",
                    ", ".join(sorted(referencing)))
        _removed_tables.update(referencing)
    for key in _removed_tables:
        # By the key it was added under: remove() recomputes the key from the
        # table's current name, which the module's code can change.
        Base.metadata._remove_table(key, None)


def _evict_module(pkg_name: str) -> None:
    """Drop a refused module and its submodules from sys.modules."""
    for key in list(sys.modules.keys()):
        if key == pkg_name or key.startswith(pkg_name + "."):
            sys.modules.pop(key, None)


def _load_one(pkg_path: Path, pkg_name: str, *, trusted: bool, declared: dict) -> dict:
    """Import a single admitted module package and register its slots.

    Args:
        trusted: If True, skip BSL import checks. Set for first-party bundled modules.
        declared: The manifest admission validated. The manifest the import
            produces must equal it, so code that rewrites PLUGIN_MANIFEST at
            import cannot widen what was admitted.

    Returns the manifest dict. Raises :class:`ModuleLoadError` on failure.
    """
    before = set(sys.modules.keys())
    existing = sys.modules.get(pkg_name)
    if existing is not None and not _is_module_code(_module_location(existing), _module_homes(pkg_path)):
        raise ModuleLoadError(f"The package name {pkg_name!r} is already in use.")

    try:
        spec = importlib.util.spec_from_file_location(
            pkg_name,
            pkg_path / "__init__.py",
            submodule_search_locations=[str(pkg_path)],
        )
        if spec is None or spec.loader is None:
            raise ModuleLoadError(f"Cannot create import spec for {pkg_path}")

        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = mod
        spec.loader.exec_module(mod)

    except ModuleLoadError:
        sys.modules.pop(pkg_name, None)
        raise
    except Exception as exc:
        log.error("Module %r failed to import (%s: %s) — skipping", pkg_name, type(exc).__name__, exc)
        sys.modules.pop(pkg_name, None)
        raise ModuleLoadError(f"Failed to import ({type(exc).__name__}: {exc})")

    # Revenue protection, second stage: admission scanned the source statically;
    # this checks what the import actually bound. Trusted (first-party bundled)
    # modules are exempt — they ARE the internals.
    if not trusted:
        violations: set[str] = set()

        for val in vars(mod).values():
            candidate = getattr(val, "__name__", None) or getattr(
                getattr(val, "__spec__", None), "name", None
            )
            if candidate and candidate in _PROTECTED_BSL_INTERNALS:
                violations.add(candidate)
            owner = getattr(val, "__module__", None)
            if owner and owner in _PROTECTED_BSL_INTERNALS:
                violations.add(owner)

        truly_new = set(sys.modules.keys()) - before
        violations |= truly_new & _PROTECTED_BSL_INTERNALS

        if violations:
            _evict_module(pkg_name)
            raise ModuleLoadError(_bsl_violation_message(pkg_name, violations))

    try:
        manifest = _validated_manifest(getattr(mod, "PLUGIN_MANIFEST", None))
    except ModuleLoadError as exc:
        log.error("Module %r rejected: invalid manifest (%s)", pkg_name, exc)
        _evict_module(pkg_name)
        raise
    if manifest != declared:
        _evict_module(pkg_name)
        raise ModuleLoadError(
            "PLUGIN_MANIFEST at import differs from the manifest declared in __init__.py.")

    slots_manifest = manifest["slots"]

    # Check every slot entry BEFORE any is registered, so a module with one bad
    # entry is refused whole (no half-registered slots) with the reason named,
    # instead of first surfacing as a broken page, a link out of Celerp, an entry
    # shown to every role, or a hook bound to code the module does not own.
    try:
        prepared_search_provider = _resolve_slot_callables(
            pkg_name, pkg_path, slots_manifest, trusted=trusted)
    except ModuleLoadError:
        log.error("Module %r rejected: invalid slots", pkg_name)
        _evict_module(pkg_name)
        raise

    # Register extension slots (search_provider is registered from its prepared
    # descriptor below, never through the generic path).
    for slot_name, contribution in slots_manifest.items():
        if slot_name == _SEARCH_PROVIDER_SLOT:
            continue
        for item in contribution if isinstance(contribution, list) else [contribution]:
            register_slot(slot_name, {**item, **_runtime_keys(pkg_name, trusted)})

    if prepared_search_provider is not None:
        register_slot(_SEARCH_PROVIDER_SLOT, prepared_search_provider)

    _register_locales(pkg_name, pkg_path, manifest)

    log.info(
        "Module %r loaded (v%s, slots: %s)",
        manifest["name"],
        manifest["version"],
        ", ".join(slots_manifest) or "none",
    )
    return manifest


def _register_locales(pkg_name: str, pkg_path: Path, manifest: dict) -> None:
    """Register module-contributed UI translation catalogs (the `locales`
    manifest key, mirroring `slots`). Each entry maps a language code to
    {"file": <path>, "rtl": <bool>}. Pushed into ui.i18n through a lazy import
    so the i18n leaf never imports the module subsystem. A malformed or
    unreadable entry is logged and skipped; the module and its other locales
    still load."""
    from ui.i18n import register_catalog

    locales = manifest.get("locales") or {}
    if not isinstance(locales, dict):
        log.error(
            "Module %r 'locales' skipped: must be a dict, got %s",
            pkg_name, type(locales).__name__,
        )
        return
    for lang, entry in locales.items():
        if not isinstance(lang, str) or not lang.strip():
            log.error(
                "Module %r locale skipped: language code must be a non-empty string (got %r)",
                pkg_name, lang,
            )
            continue
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("file"), str) or not entry["file"].strip()):
            log.error(
                "Module %r locale %r skipped: entry must be a dict with a non-empty string 'file'",
                pkg_name, lang,
            )
            continue
        rtl = entry.get("rtl", False)
        if not isinstance(rtl, bool):
            log.error(
                "Module %r locale %r: 'rtl' must be a bool, got %s - treating as false",
                pkg_name, lang, type(rtl).__name__,
            )
            rtl = False
        cat_path = pkg_path / entry["file"]
        if not _inside(cat_path, pkg_path):
            log.error("Module %r locale %r skipped: %s is outside the module folder",
                      pkg_name, lang, entry["file"])
            continue
        try:
            catalog = json.loads(cat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error(
                "Module %r locale %r skipped: cannot read %s (%s: %s)",
                pkg_name, lang, entry["file"], type(exc).__name__, exc,
            )
            continue
        register_catalog(lang, catalog, rtl=rtl)


def _route_failure(manifest: dict, kind: str, exc: Exception) -> None:
    """Route-registration failure policy: a first-party module fails boot loudly
    (a boot without it is not a working product); a third-party module is taken
    out of this process whole, with every module that depends on it, and the
    failure recorded for the Modules UI badge. The manifest carries its own
    first-party verdict (set by load_all), so the policy reads it directly rather
    than re-deriving trust here."""
    name = manifest["name"]
    if manifest.get("first_party"):
        raise ModuleLoadError(
            f"Default module {name!r} {kind} failed to register "
            f"({type(exc).__name__}: {exc})") from exc
    log.error("Module %r %s failed (%s: %s)", name, kind, type(exc).__name__, exc)
    _deactivate(name, f"{kind} failed ({type(exc).__name__}: {exc})")


def _deactivate(name: str, reason: str) -> set[str]:
    """Take a loaded module, and every loaded module depending on it directly or
    not, out of this process: off the loaded list (so is_running is false), their
    slot contributions (nav, actions, lifecycle hooks, handlers) unregistered, their
    tables off the shared metadata, and the module locale catalogs rebuilt from the
    modules still loaded. Each gets a
    load error; a dependent's names the module it needed. Returns the names
    taken out; :func:`stop_module` also removes the routes they registered."""
    from ui.i18n import clear_registry

    out = {name: reason}
    for manifest in _loaded:
        dep = next((d for d in manifest["depends_on"] if d in out), None)
        if dep is not None and manifest["name"] not in out:
            out[manifest["name"]] = f"Requires {dep!r}, which failed to load."
    _loaded[:] = [m for m in _loaded if m["name"] not in out]
    for gone, why in out.items():
        _load_errors[gone] = why
        _admitted.pop(gone, None)
        unregister_module_slots(gone)
    _drop_tables(set(out))
    clear_registry()
    for manifest in _loaded:
        module = _admitted.get(manifest["name"])
        if module is not None:
            _register_locales(module.name, module.path, manifest)
    return set(out)


def stop_module(app, name: str, reason: str) -> set[str]:
    """Take a running module, and every module depending on it, out of this process
    after startup (:func:`_deactivate`), with every route they registered on *app*.
    Returns the names taken out."""
    gone = _deactivate(name, reason)
    _remove_routes(app, gone)
    return gone


def _remove_routes(app, names: set[str]) -> None:
    stale = {id(r) for name in names for r in _module_routes.pop(name, [])}
    if not stale:
        return
    app.router.routes[:] = [r for r in app.router.routes if id(r) not in stale]
    if getattr(app, "openapi_schema", None) is not None:
        app.openapi_schema = None  # rebuilt without them on the next request


class RouteConflictError(Exception):
    """A module declared a route path already claimed by core or an earlier
    module. Starlette matches the first-registered route, so a duplicate would
    silently shadow the original - the second module is refused instead."""


def _route_keys(route) -> set:
    """(path, method) pairs a route answers, for collision comparison. A route
    with no path (Mount host rules) contributes nothing; one with no methods
    (websocket, mount) keys on its path alone."""
    path = getattr(route, "path", None)
    if path is None:
        return set()
    methods = getattr(route, "methods", None)
    if methods:
        return {(path, m) for m in methods}
    return {(path, None)}


def _register_module_routes(app, loaded: list[dict], kind: str) -> None:
    """Register `{kind}` routes (kind in {"api","ui"}) for every loaded module,
    refusing any module whose route collides with an already-registered one.

    Before core calls a module's setup function it proves the resolved callable
    is the module's own code (:func:`_check_owned_callable`). Core routes are
    registered before modules, so a module can never shadow core; between two
    modules the first loaded wins. A module whose routes fail is taken out of
    the process with its dependents (:func:`_route_failure`), and every route
    any of them added is removed, so nothing half-registers."""
    manifest_key = f"{kind}_routes"
    setup_attr = f"setup_{kind}_routes"
    for manifest in loaded:
        name = manifest["name"]
        route_mod_path = manifest.get(manifest_key)
        if not route_mod_path or not is_running(name):
            continue
        # Snapshot, then re-read app.router.routes after setup: FastHTML's
        # add_route rebinds (and may replace into) the list, so a captured list
        # or index goes stale.
        before = list(app.router.routes)
        existing = {k for r in before for k in _route_keys(r)}
        try:
            module = _admitted.get(name)
            if module is None:
                raise ModuleLoadError("module was not admitted in this process.")
            with _recording_tables(name):
                setup = _check_owned_callable(
                    name, module.path, f"{manifest_key} setup",
                    f"{route_mod_path}:{setup_attr}",
                    awaited=False, trusted=module.first_party)
                setup(app)
        except Exception as exc:
            failure: Exception = exc
        else:
            kept = {id(r) for r in before}
            added = [r for r in app.router.routes if id(r) not in kept]
            clashes = sorted({
                path for r in added
                for (path, _method) in _route_keys(r) & existing
            })
            failure = RouteConflictError(
                "route path(s) already registered: " + ", ".join(clashes)) if clashes else None
        if failure is None:
            _module_routes.setdefault(name, []).extend(added)
            log.info("Module %r: %s routes registered", name, kind.upper())
            continue
        app.router.routes[:] = before
        _route_failure(manifest, manifest_key, failure)
        _remove_routes(app, set(_module_routes) - {m["name"] for m in _loaded})


def route_module(scope) -> str | None:
    """The running module whose route serves this request, or None."""
    from starlette.routing import Match
    for name, routes in _module_routes.items():
        if any(r.matches(scope)[0] is Match.FULL for r in routes):
            return name
    return None


def register_api_routes(app, loaded: list[dict]) -> None:
    """Register API routes from all loaded modules into the FastAPI app, then
    take out any module whose code defined a table outside its table_prefix
    (:func:`_stray_table_problem`). This is the last step before the API process
    creates tables, so no such table is ever created. A first-party module's
    tables are part of Celerp's own schema (importer.reserved_tables) and keep
    their names."""
    _register_module_routes(app, loaded, "api")
    for manifest in list(_loaded):
        if manifest.get("first_party") or not is_running(manifest["name"]):
            continue
        problem = _stray_table_problem(manifest)
        if problem is not None:
            _route_failure(manifest, "tables", ModuleLoadError(problem))
    _remove_routes(app, set(_module_routes) - {m["name"] for m in _loaded})


def _stray_table_problem(manifest: dict) -> str | None:
    """Why the tables a module's code defined (its import and route setup) do
    not all carry its table_prefix, or None."""
    from celerp.models.base import Base

    prefix = manifest.get("table_prefix")
    for key in sorted(_module_tables.get(manifest["name"], set())):
        table = Base.metadata.tables.get(key)
        if table is None:
            continue
        if not prefix:
            return f"Defines table {table.name!r} but declares no table_prefix."
        if table.schema is not None or not table.name.startswith(prefix):
            return f"Defines table {table.name!r} outside its table_prefix {prefix!r}."
    return None


def register_ui_routes(app, loaded: list[dict]) -> None:
    """Register UI routes from all loaded modules into the FastHTML app."""
    _register_module_routes(app, loaded, "ui")


def _protected_hit(name: str) -> str | None:
    """The protected internal `name` names/imports from, or None."""
    for protected in _PROTECTED_BSL_INTERNALS:
        if name == protected or name.startswith(protected + "."):
            return protected
    return None


def _flag_dynamic_import(node: ast.Call, violations: set[str]) -> None:
    """Flag importlib.import_module("celerp.ai.quota") / __import__("...") /
    importlib.__import__("...") whose first arg is a string literal naming a
    protected internal - a static Import/ImportFrom walk cannot see these."""
    fn = node.func
    if isinstance(fn, ast.Name):
        fname = fn.id            # __import__(...)
    elif isinstance(fn, ast.Attribute):
        fname = fn.attr          # importlib.import_module(...) / importlib.__import__(...)
    else:
        return
    if fname not in ("import_module", "__import__") or not node.args:
        return
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        hit = _protected_hit(arg.value)
        if hit:
            violations.add(hit)


def _module_source_file(pkg_path: Path, dotted_module_path: str) -> Path | None:
    """The local ``.py`` (or package ``__init__.py``) inside ``pkg_path`` that a
    dotted module path names, or None if no such source lives in the installed
    module tree.

    Handles both the flat layout (``pkg_path`` IS the top-level package, its
    folder name == the package) and the nested GitHub-style layout (the package
    sits one level in, at ``pkg_path/<top_pkg>``). Used to prove a search
    handler's source is owned by the module before core imports it, and to seed
    the protected-BSL AST scan from that same file.
    """
    parts = dotted_module_path.split(".")
    if not parts or not parts[0]:
        return None
    top_pkg = parts[0]
    pkg_dir = pkg_path if pkg_path.name == top_pkg else pkg_path / top_pkg
    entry = pkg_dir.joinpath(*parts[1:]).with_suffix(".py")
    if entry.exists():
        return entry
    entry = pkg_dir.joinpath(*parts[1:], "__init__.py")
    if entry.exists():
        return entry
    return None


def _parse_source(path: Path) -> ast.Module:
    """Parse a module source file. A file core cannot read or parse is a file
    whose behaviour cannot be checked, so it refuses the module (fail closed).
    Raises :class:`ModuleLoadError`."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        raise ModuleLoadError(
            f"Source file {path.name!r} cannot be checked ({type(exc).__name__}).")


def _resolve_local_import(
    pkg_path: Path, current: Path, module: str | None, level: int,
) -> Path | None:
    """The source file inside the module folder that an import in ``current``
    refers to, or None when it is not the module's own code (stdlib, third-party,
    core, or a relative import that climbs out of the folder).

    The module folder is on sys.path, so an absolute import whose first part is a
    package or file in the folder is local; a flat module whose folder name is its
    package also resolves its own name. Relative imports resolve from the
    importing file's package directory.
    """
    parts = module.split(".") if module else []
    if level:
        base = current.parent
        for _ in range(level - 1):
            base = base.parent
        if not _inside(base, pkg_path):
            return None
        target = base.joinpath(*parts)
    elif not parts:
        return None
    elif parts[0] == pkg_path.name:
        target = pkg_path.joinpath(*parts[1:])
    else:
        target = pkg_path.joinpath(*parts)
    for cand in (target.with_suffix(".py"), target / "__init__.py"):
        if target != pkg_path and cand.is_file() and _inside(cand, pkg_path):
            return cand
    if target == pkg_path:
        return pkg_path / "__init__.py"
    return None


def _local_imports(pkg_path: Path, current: Path, node) -> list[Path]:
    """The module's own source files an import statement in ``current`` loads."""
    if isinstance(node, ast.Import):
        targets = [(alias.name, 0) for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        targets = [(t or None, node.level) for t in [module] + [
            f"{module}.{alias.name}" if module else alias.name for alias in node.names]]
    else:
        return []
    found = (_resolve_local_import(pkg_path, current, t, level) for t, level in targets)
    return [f for f in found if f]


def _reachable_sources(pkg_path: Path, entries: list[Path | None]) -> dict[Path, ast.Module]:
    """Every source file of the module's own code that importing ``entries``
    executes, parsed: each entry, the module's files they import, transitively,
    and the package ``__init__.py`` files on the way to each of them. Fails
    closed: a missing entry, or a reachable file that cannot be parsed, raises
    :class:`ModuleLoadError`."""
    if any(entry is None or not entry.is_file() for entry in entries):
        raise ModuleLoadError("A module entry point has no source file to check.")
    trees: dict[Path, ast.Module] = {}
    queue: list[Path] = list(entries)
    while queue:
        f = queue.pop()
        if f in trees:
            continue
        trees[f] = _parse_source(f)
        parent = f.parent if f.name != "__init__.py" else f.parent.parent
        if parent != pkg_path.parent and _inside(parent, pkg_path) and (parent / "__init__.py").is_file():
            queue.append(parent / "__init__.py")
        for node in ast.walk(trees[f]):
            queue.extend(_local_imports(pkg_path, f, node))
    return trees


def _scan_protected_imports(pkg_path: Path, entry: Path | None) -> set[str]:
    """Protected internals reachable from ``entry`` by import.

    Follows the module's own imports transitively (_reachable_sources) and flags
    static imports of a protected internal (including ``from celerp.ai import
    quota``) and dynamic importlib.import_module / __import__ calls whose literal
    argument names one. Static analysis is best-effort; the authoritative
    enforcement of paid capabilities is server-side. Fails closed like
    _reachable_sources.
    """
    violations: set[str] = set()
    for tree in _reachable_sources(pkg_path, [entry]).values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                module = node.module or ""
                targets = [module] + [f"{module}.{alias.name}" if module else alias.name
                                      for alias in node.names]
            else:
                if isinstance(node, ast.Call):
                    _flag_dynamic_import(node, violations)
                continue
            violations |= {hit for hit in map(_protected_hit, targets) if hit}
    return violations


# Names whose use writes a module's namespace in a way its source cannot show:
# the namespace mappings, code built from strings, and attribute writers reached
# through an attribute (builtins.setattr, object.__setattr__) or by name
# (getattr(builtins, 'exec')). Writes to sys.modules, which replace a whole
# module, are refused alongside them.
_NAMESPACE_WRITERS = frozenset({"globals", "vars", "exec", "eval", "__builtins__"})
_MAPPING_WRITERS = frozenset({
    "update", "setdefault", "pop", "popitem", "clear", "__setitem__", "__delitem__"})
_NAMESPACE_WRITER_ATTRS = _NAMESPACE_WRITERS | {
    "__dict__", "setattr", "delattr", "__setattr__", "__delattr__", "__getattribute__"}
# Attribute access by a name held in a value: the call, and where its name
# sits among the call's arguments (None: every argument is a name).
_ATTR_BY_NAME = {"setattr": 1, "delattr": 1, "getattr": 1,
                 "attrgetter": None, "methodcaller": 0}
# Function attributes that change what an existing def runs or is called with.
_FUNCTION_INTERNALS = frozenset({"__code__", "__defaults__", "__kwdefaults__"})


def _module_values(tree: ast.Module) -> set[str]:
    """Names in a source file that may hold a module object: every imported name
    and every name assigned from sys.modules[...] or an import call."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and _is_module_value(node.value, names)
                    and any(isinstance(t, ast.Name) and t.id not in names for t in node.targets)):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
                changed = True
    return names


def _is_module_value(node, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return _is_module_value(node.value, names)
    if isinstance(node, ast.Subscript):
        return isinstance(node.value, ast.Attribute) and node.value.attr == "modules"
    if isinstance(node, ast.Call):
        fn = node.func
        return (fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)) in (
            "import_module", "__import__", "reload")
    return False


def _dynamic_write(tree: ast.Module, handlers: set[str]) -> str | None:
    """The first construct in ``tree`` that may write one of ``handlers`` into a
    module's namespace where the source cannot show it, or None. ``handlers``
    holds PLUGIN_MANIFEST, which only its own literal in the package
    ``__init__.py`` may name."""
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    called = {id(n.func) for n in calls}
    guarded = handlers | _FUNCTION_INTERNALS
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _NAMESPACE_WRITERS:
            return node.id
        if (isinstance(node, ast.Name) and node.id in _ATTR_BY_NAME
                and id(node) not in called):
            return f"{node.id} used as a value"
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _NAMESPACE_WRITER_ATTRS | {"getattr", "PLUGIN_MANIFEST"}:
                    return f"an import of {alias.name}"
                if alias.name in _ATTR_BY_NAME and alias.asname:
                    return f"{alias.name} imported as {alias.asname}"
        if isinstance(node, ast.Attribute):
            if node.attr in ("attrgetter", "methodcaller") and id(node) not in called:
                return f"{node.attr} used as a value"
            if node.attr in _NAMESPACE_WRITER_ATTRS:
                return node.attr
            if node.attr == "PLUGIN_MANIFEST":
                return "PLUGIN_MANIFEST reached through a module"
            if isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in guarded:
                return f"an assignment to .{node.attr}"
            if (isinstance(node.value, ast.Attribute) and node.value.attr == "modules"
                    and node.attr in _MAPPING_WRITERS):
                return f"modules.{node.attr}"
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
                and isinstance(node.value, ast.Attribute) and node.value.attr == "modules"):
            return "a write to sys.modules"
    refused = guarded | _NAMESPACE_WRITER_ATTRS
    modules = None
    for node in calls:
        fn = node.func
        by_target = isinstance(fn, ast.Name) and fn.id in ("setattr", "delattr", "getattr")
        fn_name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if not by_target and fn_name not in ("attrgetter", "methodcaller"):
            continue
        if any(isinstance(a, ast.Starred) for a in node.args) or node.keywords:
            return f"{fn_name} with unpacked arguments"
        at = _ATTR_BY_NAME[fn_name]
        names = node.args if at is None else node.args[at:at + 1]
        for attr in names or [None]:
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                if set(attr.value.split(".")) & refused:
                    return f"{fn_name} of {attr.value!r}"
                continue
            if not by_target:
                return f"{fn_name} of a computed name"
            modules = _module_values(tree) if modules is None else modules
            if attr is None or _is_module_value(node.args[0], modules):
                return f"{fn_name} of a computed name on a module"
    return None


def _check_dynamic_writes(pkg_path: Path, entries: list[Path], handlers: set[str]) -> None:
    """Refuse a module whose own code may rebind a callable core will call, or
    its manifest (``handlers``), at import, through globals(), vars(), __dict__, setattr,
    exec or an attribute write: admission proves the call style from the source
    (_check_source_call_style), so a name the source does not bind for good
    would only be refused at load, after the module's migrations ran.
    Raises :class:`ModuleLoadError`."""
    for path, tree in _reachable_sources(pkg_path, entries).items():
        found = _dynamic_write(tree, handlers)
        if found:
            raise ModuleLoadError(
                f"{path.name!r} writes names dynamically ({found}), so the module's "
                "source does not show what core will call.")


def _bsl_violation_message(pkg_name: str, violations: set[str]) -> str:
    log.error("Module %r rejected: imports protected BSL internals (%s). See %s and %s",
              pkg_name, ", ".join(sorted(violations)), _BSL_DOCS_URL, _MODULE_AI_API_URL)
    return (
        f"Module {pkg_name!r} imports protected BSL internals "
        f"({', '.join(sorted(violations))}).\n\n"
        f"These modules are licensed under BSL 1.1 and cannot be imported "
        f"by third-party modules. Doing so creates a BSL derivative work.\n"
        f"  License: {_BSL_DOCS_URL}\n\n"
        f"If you need AI capabilities in your module, use the public Module "
        f"AI API instead:\n"
        f"  {_MODULE_AI_API_URL}"
    )


# The search_provider slot has a stricter contract than the generic slots: a
# single descriptor (never a list), a closed key set, and a locally-owned async
# handler resolved at load time. The template linter mirrors these exactly.
_SEARCH_PROVIDER_SLOT = "search_provider"
_SEARCH_PROVIDER_KEYS = frozenset({"handler", "result_key", "permission"})
_SEARCH_RESULT_KEYS = frozenset({"items", "entries"})


# pricing_action: a link on rows of an item's Pricing tab. Its placeholders are the
# row context core fills in; show_on lists row traits, all of which a row must carry.
# Actions open as a page: the Pricing tab has no in-page host for module content.
# requires_connector is checked by the rules every slot entry follows (_validate_slot_entry).
_PRICING_ACTION_SLOT = "pricing_action"
_PRICING_ACTION_KEYS = frozenset(
    {"label", "label_key", "href_template", "permission", "show_on", "presentation",
     "requires_connector"}
)
_PRICING_ACTION_PLACEHOLDERS = frozenset({"entity_id", "price_list", "field_name"})
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
# item_action: a button on an item's detail page, linking to the module's page for it.
_ITEM_ACTION_SLOT = "item_action"
_ITEM_ACTION_PLACEHOLDERS = frozenset({"entity_id"})
_PRICING_ROW_TRAIT_PAIRS = (("editable", "readonly"), ("sell", "cost"), ("manual", "derived"))


def _validate_href_template(slot: str, item: dict, placeholders: frozenset[str]) -> None:
    """Raise :class:`ModuleLoadError` unless ``item`` has an app-local href_template
    whose braces only wrap one of ``placeholders``.

    The app-local check runs on the template itself. Core fills each placeholder
    URL-encoded with no safe characters, so a filled value never adds a "/", a
    backslash or a control character, and a template that is app-local here gives
    an app-local link for any value."""
    href = item.get("href_template")
    if not isinstance(href, str) or not href:
        raise ModuleLoadError(f"Slot {slot!r} needs an href_template.")
    if not is_app_local_path(href):
        raise ModuleLoadError(
            f"Slot {slot!r} href_template must be a path inside Celerp: "
            f"one leading /, never //, no backslash and no control character."
        )
    unknown = set(_PLACEHOLDER_RE.findall(href)) - placeholders
    if unknown:
        raise ModuleLoadError(
            f"Slot {slot!r} href_template uses "
            f"{', '.join('{' + u + '}' for u in sorted(unknown))}; the placeholders are "
            f"{', '.join('{' + p + '}' for p in sorted(placeholders))}."
        )
    if set("{}") & set(_PLACEHOLDER_RE.sub("", href)):
        raise ModuleLoadError(
            f"Slot {slot!r} href_template has a stray brace; "
            f"braces may only wrap a placeholder."
        )


def _validate_item_action(pkg_path: Path, contribution) -> None:
    """Raise :class:`ModuleLoadError` unless every item_action item is a dict with an
    app-local href_template whose only placeholder is {entity_id}."""
    for item in contribution if isinstance(contribution, list) else [contribution]:
        if not isinstance(item, dict):
            raise ModuleLoadError(f"Slot {_ITEM_ACTION_SLOT!r} items must be dicts.")
        _validate_href_template(_ITEM_ACTION_SLOT, item, _ITEM_ACTION_PLACEHOLDERS)


def _validate_pricing_action(pkg_path: Path, contribution) -> None:
    """Raise :class:`ModuleLoadError` unless every pricing_action item has only the
    known keys, an app-local href_template whose braces only wrap known
    placeholders, a show_on list of known traits that some row can carry, and no
    presentation other than "page"."""
    traits = {trait for pair in _PRICING_ROW_TRAIT_PAIRS for trait in pair}
    for item in contribution if isinstance(contribution, list) else [contribution]:
        if not isinstance(item, dict):
            raise ModuleLoadError(f"Slot {_PRICING_ACTION_SLOT!r} items must be dicts.")
        # repr orders keys of any type: a manifest literal can mix str and int keys.
        unknown_keys = sorted(set(item) - _PRICING_ACTION_KEYS, key=repr)
        if unknown_keys:
            raise ModuleLoadError(
                f"Slot {_PRICING_ACTION_SLOT!r} has unknown key "
                f"{', '.join(repr(k) for k in unknown_keys)}; the keys are "
                f"{', '.join(sorted(_PRICING_ACTION_KEYS))}."
            )
        _validate_href_template(_PRICING_ACTION_SLOT, item, _PRICING_ACTION_PLACEHOLDERS)
        show_on = item.get("show_on", [])
        if (not isinstance(show_on, list) or not all(isinstance(t, str) for t in show_on)
                or not set(show_on) <= traits):
            raise ModuleLoadError(
                f"Slot {_PRICING_ACTION_SLOT!r} show_on must be a list of {sorted(traits)}."
            )
        for pair in _PRICING_ROW_TRAIT_PAIRS:
            if set(pair) <= set(show_on):
                raise ModuleLoadError(
                    f"Slot {_PRICING_ACTION_SLOT!r} show_on lists both {pair[0]!r} and "
                    f"{pair[1]!r}, so the action would never show."
                )
        if item.get("presentation", "page") != "page":
            raise ModuleLoadError(
                f"Slot {_PRICING_ACTION_SLOT!r} presentation must be \"page\"."
            )


# Link slots checked when a module loads, so a broken link never reaches a page.
_LINK_SLOT_VALIDATORS = {
    _ITEM_ACTION_SLOT: _validate_item_action,
    _PRICING_ACTION_SLOT: _validate_pricing_action,
}


def _enclosing_first_party_module(source_file: Path, expected_name: str) -> Path | None:
    """The nearest ancestor directory of ``source_file`` that is a content-verified
    first-party module folder named ``expected_name``, or None.

    Used to accept a first-party module whose handler resolves to the SAME
    first-party module already loaded from another location (a shipped default
    scanned from two roots - e.g. the repo copy and a reseeded copy). is_first_party
    requires the lock name AND a matching content digest, so a match here is
    content-identical to the module being loaded; a resolution to core, or to any
    other module, has no such ancestor and is rejected.
    """
    for parent in source_file.parents:
        if parent.name == expected_name and is_first_party(parent):
            return parent
    return None


def _handler_source_owned(
    proof: str | None, pkg_root: str, pkg_name: str, trusted: bool
) -> bool:
    """True if a resolved handler/module source file legitimately belongs to the
    module being loaded.

    It must live under the module's own package root. For a content-verified
    first-party module ONLY, a source under a different copy of the SAME first-party
    module (same lock name and verified digest) is also legitimate - the one case
    where importlib returns an identically-named default already loaded from another
    root. ``is_relative_to`` returns False for paths on different drives rather than
    raising, so it needs no cross-drive guard. Never accepts a resolution to core or
    to a different module, and every untrusted module is held to strict same-tree.
    """
    if not proof:
        return False
    real = Path(os.path.realpath(proof))
    if real.is_relative_to(pkg_root):
        return True
    if not trusted:
        return False
    return _enclosing_first_party_module(real, pkg_name) is not None


# Slots whose entries name code core imports and calls: the entry key holding the
# "module.path:function", and whether core awaits the call (True) or calls it
# plainly (False). fire_lifecycle awaits the hooks and the search aggregator awaits
# providers; the document page calls render(doc) and the projection engine calls
# handler(state, event_type, data) without awaiting.
_CALLABLE_SLOTS = {
    _SEARCH_PROVIDER_SLOT: ("handler", True),
    "doc_detail_actions": ("render", False),
    "doc_detail_badges": ("render", False),
    "on_company_created": ("handler", True),
    "on_modules_ready": ("handler", True),
    "doc_finalize_hook": ("handler", True),
    "on_doc_payment": ("handler", True),
    "projection_handler": ("handler", False),
    "inventory_in_production": ("handler", True),
    "item_lineage_guard": ("handler", True),
}
# Callable slots core calls with keyword arguments only, and those arguments. The
# handler takes exactly these: no other parameter, none positional-only, and no
# *args or **kwargs (lot_origin._in_production, events.engine._item_applied).
_HANDLER_KEYWORDS = {
    "inventory_in_production": ("session", "company_id"),
    "item_lineage_guard": ("session", "entry", "transition"),
}
# Entry keys naming a permission. Gating surfaces index the permission registry,
# so a value outside it must never reach them.
_PERMISSION_ENTRY_KEYS = ("permission", "write_permission")
# Entry keys naming where a click goes, and whether the entry must carry the key.
_DESTINATION_KEYS = {
    "nav": {"href": False, "settings_href": False},
    "bulk_action": {"form_action": True},
}


def _runtime_keys(pkg_name: str, trusted: bool) -> dict:
    """The keys the loader sets on every registered slot entry, applied last so a
    manifest's own _module or _first_party never survives registration."""
    return {"_module": pkg_name, "_first_party": trusted}


def _validate_bulk_action(pkg_path: Path, contribution) -> None:
    """Raise :class:`ModuleLoadError` unless every bulk_action names an
    action_type the inventory toolbar knows, when it names one."""
    for item in contribution if isinstance(contribution, list) else [contribution]:
        if item.get("action_type", "htmx") not in _BULK_ACTION_TYPES:
            raise ModuleLoadError(
                f"Slot 'bulk_action' action_type must be one of "
                f"{sorted(_BULK_ACTION_TYPES)}, not {item['action_type']!r}.")


def _validate_category_schema(pkg_path: Path, contribution) -> None:
    """Raise :class:`ModuleLoadError` unless every category_schema entry's fields
    are field definitions: dicts with a key, and text label and type and a list
    of options where given."""
    for item in contribution if isinstance(contribution, list) else [contribution]:
        for field in item["fields"]:
            if (not isinstance(field, dict) or not isinstance(field.get("key"), str)
                    or not field["key"]
                    or any(not _is_type(field[k], types)
                           for k, types in _CATEGORY_FIELD_KEYS.items() if k in field)):
                raise ModuleLoadError(
                    f"Slot 'category_schema' fields must be field definitions: a dict "
                    f"with a text key, and text label and type and a list of options "
                    f"where given, not {field!r}.")


def _takes_exactly(params: list[tuple], keywords: tuple[str, ...]) -> bool:
    """Whether a callable with these (name, kind) parameters can be called with
    exactly ``keywords`` as keyword arguments and nothing else."""
    return (sorted(name for name, _ in params) == sorted(keywords)
            and all(kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                    for _, kind in params))


def _source_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple]:
    """A def's parameters as (name, kind), the way inspect.signature reports them."""
    a, kind = node.args, inspect.Parameter
    return ([(p.arg, kind.POSITIONAL_ONLY) for p in a.posonlyargs]
            + [(p.arg, kind.POSITIONAL_OR_KEYWORD) for p in a.args]
            + ([(a.vararg.arg, kind.VAR_POSITIONAL)] if a.vararg else [])
            + [(p.arg, kind.KEYWORD_ONLY) for p in a.kwonlyargs]
            + ([(a.kwarg.arg, kind.VAR_KEYWORD)] if a.kwarg else []))


def _check_keywords(slot: str, dotted: str, params) -> None:
    """Refuse a handler that cannot be called with exactly the slot's keywords."""
    keywords = _HANDLER_KEYWORDS[slot]
    if not _takes_exactly(params, keywords):
        raise ModuleLoadError(
            f"Slot {slot!r} callable {dotted!r} must take exactly the keyword arguments "
            f"{', '.join(keywords)}; core calls it with those and nothing else.")


def _keyword_validator(slot: str):
    """The admission check for a slot in _HANDLER_KEYWORDS: each handler's
    parameters, read from the module's source. A handler whose source does not
    show them is already refused (_check_source_call_style); a class or lambda
    handler is checked at load."""
    def validate(pkg_path: Path, contribution) -> None:
        for item in contribution if isinstance(contribution, list) else [contribution]:
            dotted = item["handler"]
            source = _owned_callable_source(pkg_path, f"Slot {slot!r}", dotted)
            node = _source_callable(pkg_path, source, dotted.split(":")[1])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _check_keywords(slot, dotted, _source_params(node))
    return validate


# Per-slot checks beyond the entry keys every slot declares (_SLOT_ENTRY_KEYS),
# each called as validate(pkg_path, contribution) at admission.
_SLOT_VALIDATORS = {
    **_LINK_SLOT_VALIDATORS,
    "bulk_action": _validate_bulk_action,
    "category_schema": _validate_category_schema,
    **{slot: _keyword_validator(slot) for slot in _HANDLER_KEYWORDS},
}

_BULK_ACTION_TYPES = frozenset({"htmx", "navigate"})
_CATEGORY_FIELD_KEYS = {"label": (str,), "type": (str,), "options": (list,)}
_TEXT, _NUMBER = (str,), (int, float)
_LABEL_KEYS = {"label": (_TEXT, False), "label_key": (_TEXT, False)}
# What the code reading each slot takes from an entry: per key, the types it
# reads the value as and whether the entry must carry it (a required text value
# must not be empty). Callable keys are checked as callables (_CALLABLE_SLOTS),
# destinations as paths (_DESTINATION_KEYS) and permissions as permission keys.
_SLOT_ENTRY_KEYS: dict[str, dict[str, tuple[tuple[type, ...], bool]]] = {
    "nav": {**_LABEL_KEYS, "key": (_TEXT, False), "group": ((str, type(None)), False),
            "order": (_NUMBER, False)},
    "bulk_action": {**_LABEL_KEYS, "action_type": (_TEXT, False)},
    "send_to_targets": {**_LABEL_KEYS, "doc_type": (_TEXT, True)},
    "catalog_channel": {**_LABEL_KEYS, "id": (_TEXT, True), "marker": (_TEXT, False),
                        "can_create": ((bool,), False)},
    "item_action": _LABEL_KEYS,
    "pricing_action": _LABEL_KEYS,
    "category_schema": {"category": (_TEXT, True), "fields": ((list,), True)},
    "projection_handler": {"prefix": (_TEXT, True)},
    "search_provider": {"result_key": (_TEXT, True)},
    "doc_detail_actions": {},
    "doc_detail_badges": {},
    "on_company_created": {},
    "on_modules_ready": {},
    "doc_finalize_hook": {},
    "on_doc_payment": {},
    "inventory_in_production": {},
    "item_lineage_guard": {},
}
_TYPE_NAMES = {str: "text", int: "a number", float: "a number", bool: "true or false",
               list: "a list", type(None): "None"}


def _is_type(value, types: tuple[type, ...]) -> bool:
    """isinstance, except that True and False are not numbers here."""
    return isinstance(value, types) and (bool in types or not isinstance(value, bool))


def _validate_slot_entry(slot: str, item) -> None:
    """The rules every slot entry follows, whatever its slot.

    Raise :class:`ModuleLoadError` unless ``item`` is a dict; carries every key
    its slot reads (_SLOT_ENTRY_KEYS) in the type it is read as, with the
    required ones present; has a "permission" / "write_permission", when
    present, that is a key from the permission registry (a falsy or malformed
    value is refused, never read as "ungated"); has a "requires_connector" that
    is None or a string, a connector id (None or "" means no connector is
    needed); and has every destination its slot reads (_DESTINATION_KEYS) as an
    app-local path, with the required ones present.
    """
    if not isinstance(item, dict):
        raise ModuleLoadError(
            f"Slot {slot!r} entries must be dicts, not {type(item).__name__}."
        )
    for key, (types, required) in _SLOT_ENTRY_KEYS.get(slot, {}).items():
        if key not in item:
            if required:
                raise ModuleLoadError(f"Slot {slot!r} needs a {key}.")
            continue
        if not _is_type(item[key], types):
            names = " or ".join(dict.fromkeys(_TYPE_NAMES[t] for t in types))
            raise ModuleLoadError(f"Slot {slot!r} {key} must be {names}, not {item[key]!r}.")
        if required and types == _TEXT and not item[key]:
            raise ModuleLoadError(f"Slot {slot!r} {key} must not be empty.")
    for key in _PERMISSION_ENTRY_KEYS:
        if key in item and not is_permission_key(item[key]):
            raise ModuleLoadError(
                f"Slot {slot!r} {key} names unknown permission key {item[key]!r}. "
                f"Permission keys come from Celerp's own registry; pick the "
                f"closest existing key, or leave {key} out."
            )
    connector = item.get("requires_connector")
    if connector is not None and not isinstance(connector, str):
        raise ModuleLoadError(
            f"Slot {slot!r} requires_connector must be a connector id, not {connector!r}."
        )
    for key, required in _DESTINATION_KEYS.get(slot, {}).items():
        if key not in item:
            if required:
                raise ModuleLoadError(f"Slot {slot!r} needs a {key}.")
            continue
        if not is_app_local_path(item[key]):
            raise ModuleLoadError(
                f"Slot {slot!r} {key} {item[key]!r} must be a path inside Celerp: "
                f"one leading /, never //, no backslash and no control character."
            )


def _check_search_provider_descriptor(contribution) -> None:
    """The search_provider slot takes exactly one dict: one module, one
    provider, one results bucket, so a module can never overwrite its own search
    bucket. The descriptor must carry exactly ``{handler, result_key,
    permission}`` (extra keys are refused, never ignored, so a misspelling fails
    loudly), and ``result_key`` is one of ``{items, entries}``."""
    if not isinstance(contribution, dict):
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} takes exactly one descriptor dict, "
            f"not a {type(contribution).__name__}."
        )
    keys = set(contribution)
    if keys != set(_SEARCH_PROVIDER_KEYS):
        missing = sorted(_SEARCH_PROVIDER_KEYS - keys)
        extra = sorted(keys - _SEARCH_PROVIDER_KEYS, key=repr)
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} descriptor keys must be exactly "
            f"{sorted(_SEARCH_PROVIDER_KEYS)} (missing={missing}, extra={extra})."
        )
    result_key = contribution["result_key"]
    if not isinstance(result_key, str) or result_key not in _SEARCH_RESULT_KEYS:
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} result_key {result_key!r} must be one "
            f"of {sorted(_SEARCH_RESULT_KEYS)}."
        )


def _check_slot_contracts(pkg_path: Path, slots_manifest: dict) -> None:
    """Every slot rule the manifest and the module's source decide, checked
    before any of the module's code runs: the slot is one Celerp reads
    (SLOT_NAMES), the search_provider descriptor, the entry rules (_validate_slot_entry), each slot's own validator, and for a
    callable slot an in-module "module.path:function" whose source shows it
    async exactly where core awaits it (_check_source_call_style). Load proves
    the object importing actually returns (_resolve_slot_callables). Raises :class:`ModuleLoadError`.
    ``slots_manifest`` is already a dict (:func:`_validated_manifest`).
    """
    for slot_name, contribution in slots_manifest.items():
        if slot_name not in SLOT_NAMES:
            raise ModuleLoadError(
                f"The manifest fills unknown slot {slot_name!r}; Celerp reads only "
                f"{', '.join(sorted(SLOT_NAMES))}.")
        if slot_name == _SEARCH_PROVIDER_SLOT:
            _check_search_provider_descriptor(contribution)
        for item in contribution if isinstance(contribution, list) else [contribution]:
            _validate_slot_entry(slot_name, item)
            if slot_name in _CALLABLE_SLOTS:
                key, awaited = _CALLABLE_SLOTS[slot_name]
                subject = f"Slot {slot_name!r}"
                source = _owned_callable_source(pkg_path, subject, item.get(key))
                _check_source_call_style(pkg_path, source, subject, item[key], awaited=awaited)
        validate = _SLOT_VALIDATORS.get(slot_name)
        if validate is not None:
            validate(pkg_path, contribution)


def _resolve_slot_callables(
    pkg_name: str, pkg_path: Path, slots_manifest: dict, *, trusted: bool
) -> dict | None:
    """Prove every callable a module's slots name (_check_owned_callable) before
    anything is registered. The rest of each entry passed admission
    (_check_slot_contracts) and the manifest at import equals the one admitted.
    Returns the search_provider descriptor to register, or None. Raises
    :class:`ModuleLoadError` on any violation.
    """
    prepared = None
    for slot_name, contribution in slots_manifest.items():
        if slot_name not in _CALLABLE_SLOTS:
            continue
        key, awaited = _CALLABLE_SLOTS[slot_name]
        for item in contribution if isinstance(contribution, list) else [contribution]:
            func = _check_owned_callable(
                pkg_name, pkg_path, f"Slot {slot_name!r}", item[key],
                awaited=awaited, trusted=trusted)
            if slot_name in _HANDLER_KEYWORDS:
                try:
                    params = [(p.name, p.kind) for p in inspect.signature(func).parameters.values()]
                except (TypeError, ValueError):
                    raise ModuleLoadError(
                        f"Slot {slot_name!r} callable {item[key]!r} has no readable signature."
                    ) from None
                _check_keywords(slot_name, item[key], params)
        if slot_name == _SEARCH_PROVIDER_SLOT:
            # Runtime-owned trust metadata goes AFTER the manifest contribution,
            # and the descriptor's closed key set already refuses a manifest that
            # supplies _module / _first_party itself, so neither can be spoofed.
            prepared = {**contribution, **_runtime_keys(pkg_name, trusted)}
    return prepared


def _owned_callable_source(pkg_path: Path, subject: str, dotted) -> Path:
    """The source file of a "module.path:function" entry, which must sit inside
    this module's own tree, so a manifest can never point core at
    'celerp.some_internal:fn' and have it imported around the protected-import
    gate. Raises :class:`ModuleLoadError`."""
    if (not isinstance(dotted, str) or dotted.count(":") != 1
            or not all(dotted.split(":"))):
        raise ModuleLoadError(
            f"{subject} callable {dotted!r} must be 'module.path:function'."
        )
    source = _module_source_file(pkg_path, dotted.split(":")[0])
    if source is None:
        raise ModuleLoadError(
            f"{subject} callable {dotted!r} does not resolve to source inside "
            f"the module."
        )
    return source


def _check_call_style(subject: str, dotted: str, is_async: bool, *, awaited: bool) -> None:
    """Refuse a callable that is async where core calls it plainly, or plain
    where core awaits it."""
    if is_async == awaited:
        return
    if awaited:
        raise ModuleLoadError(f"{subject} callable {dotted!r} must be async; core awaits it.")
    raise ModuleLoadError(
        f"{subject} callable {dotted!r} must not be async; core calls it without awaiting.")


def _top_level_binding(tree: ast.Module, name: str):
    """The one statement that binds ``name`` in a module, when that is the only
    place the source binds (or deletes) it at all, it sits at the top level and
    no later star import can rebind it; else None."""
    bindings = [node for node in ast.walk(tree) if name in _bound_names(node)]
    if len(bindings) != 1:
        return None
    (binding,) = bindings
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
           and n.lineno > binding.lineno for n in tree.body):
        return None  # a later star import may rebind it
    if binding in tree.body:
        return binding
    owner = next((n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                  and binding in ast.walk(n)), None)
    return owner


def _source_callable(pkg_path: Path, source: Path, name: str, seen: set | None = None):
    """The undecorated def, async def, class or lambda that ``name`` in ``source``
    is, read from the module's own source without running it: followed through
    plain aliases and imports of the module's own files. None when the source
    alone cannot tell; loading then decides."""
    seen = set() if seen is None else seen
    if (source, name) in seen:
        return None
    seen.add((source, name))
    try:
        tree = _parse_source(source)
    except ModuleLoadError:
        return None
    binding = _top_level_binding(tree, name)
    if isinstance(binding, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return None if binding.decorator_list else binding
    if isinstance(binding, (ast.Assign, ast.AnnAssign)):
        targets = binding.targets if isinstance(binding, ast.Assign) else [binding.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return None
        if isinstance(binding.value, ast.Lambda):
            return binding.value
        if isinstance(binding.value, ast.Name):
            return _source_callable(pkg_path, source, binding.value.id, seen)
        return None
    if isinstance(binding, ast.ImportFrom):
        alias = next(a for a in binding.names if (a.asname or a.name) == name)
        target = _resolve_local_import(pkg_path, source, binding.module, binding.level)
        if target is None:
            return None
        return _source_callable(pkg_path, target, alias.name, seen)
    return None


def _check_source_call_style(pkg_path: Path, source: Path, subject: str, dotted: str, *,
                             awaited: bool) -> None:
    """Refuse, before any of the module's code runs, a callable whose source does
    not show how core may call it: ``dotted`` must name an undecorated def (or a
    class or lambda) in the module's own code (_source_callable), async exactly
    where core awaits it. A decorated or call-built callable could be either, and
    refusing it only at load would come after its migrations ran."""
    node = _source_callable(pkg_path, source, dotted.split(":")[1])
    if node is None:
        raise ModuleLoadError(
            f"{subject} callable {dotted!r} must be a plain top-level def in the module's "
            "own code, not decorated, rebound or built by a call.")
    _check_call_style(subject, dotted, isinstance(node, ast.AsyncFunctionDef), awaited=awaited)


def _check_owned_callable(
    pkg_name: str, pkg_path: Path, subject: str, dotted, *, awaited: bool, trusted: bool
):
    """Prove, before core first calls it, that ``dotted`` names a callable this
    module owns, and return it. ``subject`` names the manifest entry for errors
    (a slot, or a route module's setup function).

    ``dotted`` must be "module.path:function" naming source inside this module's
    own tree, must import no protected BSL internal (third-party only), and must
    resolve to a callable that is async exactly when core awaits it. Provenance
    is then proven on what importlib actually returned, not on the file that
    matched the dotted path (_handler_source_owned). An entry that cannot pass
    fails its module rather than first surfacing when core calls it. Raises
    :class:`ModuleLoadError` on any violation.
    """
    source = _owned_callable_source(pkg_path, subject, dotted)
    # The callable is a lazily-imported entry point, so it goes through the same
    # transitive protected-BSL scan as the route entry modules (third-party only).
    if not trusted:
        violations = _scan_protected_imports(pkg_path, source)
        if violations:
            raise ModuleLoadError(
                f"{subject} callable {dotted!r} imports protected BSL "
                f"internals ({', '.join(sorted(violations))})."
            )
    try:
        func = resolve_handler(dotted)
    except Exception as exc:
        raise ModuleLoadError(
            f"{subject} callable {dotted!r} failed to resolve ({type(exc).__name__})."
        )
    if not callable(func):
        raise ModuleLoadError(f"{subject} callable {dotted!r} is not callable.")
    _check_call_style(subject, dotted, inspect.iscoroutinefunction(func), awaited=awaited)
    # Provenance: an on-disk file matching the dotted path is not proof of what
    # importlib actually resolved. A decoy source shipped inside the module's own
    # tree (e.g. a celerp/ai/service.py) satisfies the existence and AST checks
    # above, yet importlib returns the already-loaded REAL core module of the same
    # dotted name, binding the slot to arbitrary code. Require that BOTH the
    # resolved module's file AND the callable's own source file be owned by this
    # module (_handler_source_owned): under its own package root, or - for a
    # content-verified first-party module only - inside the same first-party module
    # (same lock name and digest) loaded from another root. realpath collapses
    # symlinks and '..' so neither can point a proof outside the tree. This runs for
    # every module: a first-party manifest whose callable resolves to core, or to a
    # different module, is rejected exactly as an untrusted decoy is.
    pkg_root = os.path.realpath(pkg_path)
    try:
        module_file = getattr(
            importlib.import_module(dotted.split(":")[0]), "__file__", None)
        func_file = inspect.getsourcefile(inspect.unwrap(func))
    except Exception as exc:
        raise ModuleLoadError(
            f"{subject} callable {dotted!r} source could not be located "
            f"({type(exc).__name__})."
        )
    for proof in (module_file, func_file):
        if not _handler_source_owned(proof, pkg_root, pkg_name, trusted):
            raise ModuleLoadError(
                f"{subject} callable {dotted!r} resolves to source outside "
                f"module {pkg_name!r}'s own package tree."
            )
    return func

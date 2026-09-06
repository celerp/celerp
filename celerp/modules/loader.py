# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Module loader — scans DATA_DIR/modules/, imports enabled modules,
registers slots, wires API and UI routes.

Revenue protection
------------------
The loader enforces that no module imports protected BSL internals:
  - celerp.session_gate
  - celerp.ai.service
  - celerp.ai.quota

If a module imports any of these, it is rejected with a clear error that
names the violation and links to the license and the sanctioned alternative.

Module authors who need AI should use celerp.modules.api (public, BSL) —
NOT celerp.ai.* directly.

Startup sequence
----------------
Called from celerp/main.py lifespan:
    from celerp.modules.loader import load_all, register_api_routes
    loaded = load_all(module_dir, enabled_modules)
    register_api_routes(app, loaded)

Called from ui/app.py after core route setup:
    from celerp.modules.loader import register_ui_routes
    register_ui_routes(ui_app, loaded)
"""
from __future__ import annotations

import ast
import fnmatch
import functools
import hashlib
import importlib
import importlib.util
import inspect
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from celerp.modules.importer import PREMIUM_MARKER
from celerp.modules.license import check_license, exchange_api_key_for_jwt, is_premium_path
from celerp.modules.meta import META_FILENAME
from celerp.modules.slots import register as register_slot, resolve_handler
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
def _resolve_bundled_dirs() -> tuple[Path, ...]:
    base = Path(__file__).resolve().parent.parent.parent / "default_modules"
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


def with_writable_module_dir(module_dir_env: str) -> str:
    """Ensure a launch path's MODULE_DIR writes imports to a safe location.

    The importer installs into MODULE_DIR.split(",")[0]. If that first entry is a
    bundled/trusted dir (the dev/CLI footgun: MODULE_DIR=default_modules), a
    writable data-dir drop-in is prepended so imports land there, with the bundled
    dir kept on the path for default discovery. An already-safe first entry, or an
    unset MODULE_DIR (module system off), is returned unchanged."""
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
    return Path(__file__).resolve().parent.parent.parent / "default_modules" / "first_party.lock.json"


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


def resolve_module_path(name: str) -> Path | None:
    """The first <entry>/<name> package (a dir with __init__.py) across the
    MODULE_DIR search entries, or None if no entry holds it.

    Mirrors the scan's directory walk so a module living in any configured search
    entry - not only the first install target - is found. The delete guard needs a
    real Path to ask is_first_party the same content question the scan asks.
    """
    for entry in os.environ.get("MODULE_DIR", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        candidate = Path(entry) / name
        if candidate.is_dir() and (candidate / "__init__.py").exists():
            return candidate
    return None


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


def _read_depends_on(pkg_path: Path) -> list[str]:
    """Extract PLUGIN_MANIFEST['depends_on'] from a module's __init__.py via AST.

    Returns empty list if the manifest or key is absent or unparseable.
    """
    return list(read_manifest(pkg_path).get("depends_on") or [])


def _topo_sort(pkg_paths: list[Path], enabled: set[str], errors: dict[str, str] | None = None) -> list[Path]:
    """Return pkg_paths sorted so dependencies come before dependents.

    Modules with unresolvable hard deps are excluded with a warning.
    """
    path_by_name = {p.name: p for p in pkg_paths}
    deps_by_name: dict[str, list[str]] = {}
    for p in pkg_paths:
        deps_by_name[p.name] = _read_depends_on(p)

    result: list[Path] = []
    done: set[str] = set()       # fully resolved and appended to result
    on_stack: set[str] = set()   # currently being visited (for cycle detection)
    skipped: set[str] = set()

    def _visit(name: str) -> None:
        if name in done or name in skipped:
            return
        if name in on_stack:
            # A back-edge to a module still being visited: a dependency cycle.
            # Skip it with a clear error rather than accepting an order that
            # cannot actually satisfy the deps.
            log.warning("Module %r is part of a dependency cycle - skipping", name)
            if errors is not None:
                errors.setdefault(name, "Part of a dependency cycle.")
            skipped.add(name)
            return
        on_stack.add(name)
        for dep in deps_by_name.get(name, []):
            if dep not in enabled:
                log.warning(
                    "Module %r requires %r which is not enabled — skipping %r",
                    name, dep, name,
                )
                if errors is not None:
                    errors.setdefault(name, f"Requires {dep!r}, which is not enabled.")
                skipped.add(name)
                on_stack.discard(name)
                return
            if dep not in path_by_name:
                log.warning(
                    "Module %r requires %r which is not installed — skipping %r",
                    name, dep, name,
                )
                if errors is not None:
                    errors.setdefault(name, f"Requires {dep!r}, which is not installed.")
                skipped.add(name)
                on_stack.discard(name)
                return
            _visit(dep)
            if dep in skipped:
                log.warning(
                    "Module %r requires %r which was skipped — skipping %r",
                    name, dep, name,
                )
                if errors is not None:
                    errors.setdefault(name, f"Requires {dep!r}, which failed to load.")
                skipped.add(name)
                on_stack.discard(name)
                return
        on_stack.discard(name)
        done.add(name)
        result.append(path_by_name[name])

    for p in pkg_paths:
        _visit(p.name)

    return result


# Per-module load failures from the last load_all() run: name -> message.
# The modules UI surfaces these so a broken module fails loudly, not silently.
_load_errors: dict[str, str] = {}


def load_errors() -> dict[str, str]:
    """Return {module_name: error message} for modules that failed to load."""
    return dict(_load_errors)


def record_load_error(name: str, msg: str) -> None:
    """Record a load failure for a module the UI should surface, masking any
    database credentials in the message first so a connection string never
    reaches the modules page. Used by the startup migration phase to hold a
    third-party module's failure across load_all (which clears the map on entry).
    """
    from celerp.db import mask_db_credentials

    _load_errors[name] = mask_db_credentials(msg)


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


def load_all(module_dir: str | Path, enabled: set[str]) -> list[dict]:
    """Scan module directories for enabled modules, import them, register slots.

    Args:
        module_dir: Comma-separated paths or single path to module directories.
        enabled: Set of module names that should be loaded.

    Returns:
        List of successfully loaded PLUGIN_MANIFEST dicts.
    """
    _loaded.clear()
    _load_errors.clear()
    # Module-contributed i18n catalogs are rebuilt from scratch on every pass,
    # exactly like _loaded above, so a re-scan (a module toggled off, or a
    # catalog changed) never leaves a stale or orphaned catalog behind. Lazy
    # import keeps ui.i18n the lowest leaf (it must not import this module).
    from ui.i18n import clear_registry
    clear_registry()

    # Support comma-separated module directories
    raw = str(module_dir)
    dirs = [Path(d.strip()) for d in raw.split(",") if d.strip()]

    # Collect enabled packages from all directories
    candidate_paths = []
    for d in dirs:
        if not d.exists():
            log.info("Module directory %s does not exist — skipping", d)
            continue
        d_str = str(d)
        if d_str not in sys.path:
            sys.path.insert(0, d_str)
        for p in d.iterdir():
            if not p.is_dir() or not (p / "__init__.py").exists():
                continue
            if p.name not in enabled:
                continue
            if p.name in CORE_FOLDED:
                # Proprietary cloud core (ai/backup/connectors) is wired directly at app construction
                # (see celerp/main.py and ui/app.py), never as a pluggable module. Skip it here so it is
                # not double-registered and cannot be replaced by a user-supplied module of the same name.
                continue
            # Each module dir (e.g. default_modules/celerp-inventory/) must be on
            # sys.path so that its inner packages (e.g. celerp_inventory) are
            # importable by importlib.import_module when routes are registered.
            p_str = str(p)
            if p_str not in sys.path:
                sys.path.insert(0, p_str)
            candidate_paths.append(p)

    # Sort by dependency order (skip reasons land in _load_errors). Discovery is
    # name-sorted first so load order - and therefore every first-registered-wins
    # tie-break (slots and module i18n catalogs alike) - is deterministic, not
    # dependent on the filesystem's iterdir() order.
    ordered = _topo_sort(
        sorted(candidate_paths, key=lambda p: p.name), enabled, errors=_load_errors)

    # Relay credentials for the premium-license gate, resolved lazily and ONCE
    # per load pass: the JWT is the same for every module, and there must be no
    # network call at all when no premium module is present. gateway_token is
    # the real, documented credential (GATEWAY_TOKEN / GATEWAY_URL in
    # .env.production.example on a hosted deploy; set by /auth/activate on
    # desktop), exchanged for a short-lived JWT via /auth/token - the same
    # pattern celerp.routers.health uses. A prior version read
    # CELERP_RELAY_URL / CELERP_INSTANCE_JWT, which are set in no environment
    # (and an instance JWT expires hourly, so it could never be a static env
    # var), so as written this gate could not verify a license once premium
    # modules shipped.
    _premium_creds: dict = {}

    def _resolve_premium_creds() -> tuple[str, str | None, str, str]:
        if not _premium_creds:
            from celerp.config import ensure_instance_id, settings as _settings
            from celerp.gateway.state import relay_http_url
            api_key = _settings.gateway_token
            relay_url = relay_http_url() if api_key else ""
            _premium_creds["relay_url"] = relay_url
            _premium_creds["jwt"] = (
                exchange_api_key_for_jwt(relay_url, api_key) if api_key else None)
            _premium_creds["data_dir"] = os.environ.get("DATA_DIR", "/tmp/celerp-data")
            # The instance's own canonical id (offline-available): a lifetime
            # license is validated against this via its `sub` claim.
            _premium_creds["instance_id"] = ensure_instance_id()
        return (_premium_creds["relay_url"], _premium_creds["jwt"],
                _premium_creds["data_dir"], _premium_creds["instance_id"])

    for pkg_path in ordered:
        pkg_name = pkg_path.name
        # Trust is by content identity, decided ONCE here: the folder name is in
        # the committed lock AND the live content digest matches. Location and name
        # alone grant nothing, so a forged folder in a trusted dir is not trusted.
        first_party = is_first_party(pkg_path)
        trusted = first_party
        # License gate for premium modules (only when this instance has a relay
        # identity - i.e. it has activated / been given a GATEWAY_TOKEN).
        if is_premium_path(pkg_path):
            relay_url, instance_jwt, data_dir, instance_id = _resolve_premium_creds()
            if relay_url:
                # This instance has a relay identity, so a premium module is
                # verified. Verify even when the live token exchange failed
                # (instance_jwt is None): check_license still decides from the
                # offline lifetime JWT and the grace cache, so a transient
                # startup failure falls back to cached state rather than skipping
                # the check. Only a genuinely never-activated install (no
                # gateway_token -> relay_url == "") skips it.
                if not check_license(
                    slug=pkg_name,
                    relay_url=relay_url,
                    instance_jwt=instance_jwt or "",
                    cache_dir=Path(data_dir),
                    instance_id=instance_id,
                    offline_only=instance_jwt is None,
                ):
                    log.warning(
                        "Premium module %r skipped: no valid license", pkg_name
                    )
                    _load_errors[pkg_name] = "Premium module: no valid license."
                    continue
            else:
                log.debug(
                    "Premium module %r: no relay identity (never activated) - "
                    "skipping license check (dev mode)",
                    pkg_name,
                )
        # Run the source just content-verified, never a stale/tampered .pyc that a
        # matching cache header would execute in preference (the digest omits *.pyc).
        _purge_pycache(pkg_path)
        try:
            manifest = _load_one(pkg_path, pkg_name, trusted=trusted)
        except ModuleLoadError as exc:
            # A default module IS the product (a boot without documents is not
            # a working app): fail startup naming the module and error.
            # Third-party modules keep load-and-continue; their failure shows
            # as the failed badge in the Modules UI.
            if first_party:
                raise ModuleLoadError(
                    f"Default module {pkg_name!r} failed to load: {exc}") from exc
            _load_errors[pkg_name] = str(exc)
            manifest = None
        if manifest is not None:
            # Carry the trust decision on the manifest so route registration reads
            # it rather than recomputing (and re-hashing) per module.
            manifest["first_party"] = first_party
            _loaded.append(manifest)

    log.info(
        "Module loader complete: %d loaded, %d skipped/rejected",
        len(_loaded),
        len(enabled) - len(_loaded),
    )
    return list(_loaded)


def _load_one(pkg_path: Path, pkg_name: str, *, trusted: bool = False) -> dict | None:
    """Import a single module package and register its slots.

    Args:
        trusted: If True, skip BSL import checks. Set for first-party bundled modules.

    Returns the manifest dict on success, None on failure.
    """
    before = set(sys.modules.keys())

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

    # Revenue protection: reject modules that reference protected BSL internals.
    # Trusted (first-party bundled) modules are exempt — they ARE the internals.
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
            violation_list = ", ".join(sorted(violations))
            log.error(
                "Module %r rejected: imports protected BSL internals (%s). "
                "See %s and %s",
                pkg_name, violation_list, _BSL_DOCS_URL, _MODULE_AI_API_URL,
            )
            for key in list(sys.modules.keys()):
                if key == pkg_name or key.startswith(pkg_name + "."):
                    sys.modules.pop(key, None)
            raise ModuleLoadError(
                f"Module {pkg_name!r} imports protected BSL internals "
                f"({violation_list}).\n\n"
                f"These modules are licensed under BSL 1.1 and cannot be imported "
                f"by third-party modules. Doing so creates a BSL derivative work.\n"
                f"  License: {_BSL_DOCS_URL}\n\n"
                f"If you need AI capabilities in your module, use the public Module "
                f"AI API instead:\n"
                f"  {_MODULE_AI_API_URL}"
            )

    manifest = getattr(mod, "PLUGIN_MANIFEST", None)
    if not manifest:
        log.warning("Module %r has no PLUGIN_MANIFEST — skipping", pkg_name)
        sys.modules.pop(pkg_name, None)
        raise ModuleLoadError("No PLUGIN_MANIFEST in __init__.py.")

    # Validate required manifest fields
    missing = [f for f in ("name", "version") if not manifest.get(f)]
    if missing:
        log.error("Module %r manifest missing required fields: %s — skipping", pkg_name, missing)
        sys.modules.pop(pkg_name, None)
        raise ModuleLoadError(f"Manifest missing required fields: {', '.join(missing)}.")

    # Validate hard deps are loaded
    for dep in (manifest.get("depends_on") or []):
        if not any(m["name"] == dep for m in _loaded):
            raise ModuleLoadError(
                f"Module {pkg_name!r} requires {dep!r} which is not loaded"
            )

    # AST scan api_routes and ui_routes for lazy forbidden imports (third-party only)
    if not trusted:
        ast_violations: set[str] = set()
        for route_key in ("api_routes", "ui_routes"):
            route_mod = manifest.get(route_key)
            if route_mod and isinstance(route_mod, str):
                ast_violations |= _ast_scan_module_file(pkg_path, route_mod)
        if ast_violations:
            violation_list = ", ".join(sorted(ast_violations))
            log.error(
                "Module %r rejected (AST scan): lazy imports of protected BSL internals (%s). "
                "See %s and %s",
                pkg_name, violation_list, _BSL_DOCS_URL, _MODULE_AI_API_URL,
            )
            for key in list(sys.modules.keys()):
                if key == pkg_name or key.startswith(pkg_name + "."):
                    sys.modules.pop(key, None)
            raise ModuleLoadError(
                f"Module {pkg_name!r} has lazy imports of protected BSL internals "
                f"({violation_list}) in route files.\n\n"
                f"These modules are licensed under BSL 1.1 and cannot be imported "
                f"by third-party modules. Doing so creates a BSL derivative work.\n"
                f"  License: {_BSL_DOCS_URL}\n\n"
                f"If you need AI capabilities in your module, use the public Module "
                f"AI API instead:\n"
                f"  {_MODULE_AI_API_URL}"
            )

    slots_manifest = manifest.get("slots") or {}

    # A slot item naming a permission key outside the registry would KeyError in
    # the sidebar builder on every page render, taking the whole UI down for
    # every user. Refuse the module here, with the bad key named, instead. The
    # search_provider slot has its own stricter validation below (which also
    # checks its permission), so it is skipped here.
    for slot_name, contribution in slots_manifest.items():
        if slot_name == _SEARCH_PROVIDER_SLOT:
            continue
        items = contribution if isinstance(contribution, list) else [contribution]
        for item in items:
            perm = item.get("permission") if isinstance(item, dict) else None
            if perm and not is_permission_key(perm):
                log.error(
                    "Module %r rejected: slot %r names unknown permission key %r",
                    pkg_name, slot_name, perm,
                )
                for key in list(sys.modules.keys()):
                    if key == pkg_name or key.startswith(pkg_name + "."):
                        sys.modules.pop(key, None)
                raise ModuleLoadError(
                    f"Slot {slot_name!r} names unknown permission key {perm!r}. "
                    f"Permission keys come from Celerp's own registry; pick the "
                    f"closest existing key."
                )

    # Validate the search_provider descriptor and resolve its handler BEFORE any
    # slot is registered, so a broken provider rejects the whole module cleanly
    # (no half-registered slots) rather than first surfacing as a degraded source
    # when a user types into search.
    prepared_search_provider = None
    if _SEARCH_PROVIDER_SLOT in slots_manifest:
        try:
            prepared_search_provider = _prepare_search_provider(
                pkg_name, pkg_path, slots_manifest[_SEARCH_PROVIDER_SLOT],
                trusted=trusted,
            )
        except ModuleLoadError:
            log.error("Module %r rejected: invalid search_provider slot", pkg_name)
            for key in list(sys.modules.keys()):
                if key == pkg_name or key.startswith(pkg_name + "."):
                    sys.modules.pop(key, None)
            raise

    # Register extension slots (search_provider is registered from its prepared
    # descriptor below, never through the generic path).
    for slot_name, contribution in slots_manifest.items():
        if slot_name == _SEARCH_PROVIDER_SLOT:
            continue
        if isinstance(contribution, dict):
            register_slot(slot_name, {**contribution, "_module": pkg_name})
        elif isinstance(contribution, list):
            for item in contribution:
                register_slot(slot_name, {**item, "_module": pkg_name})

    if prepared_search_provider is not None:
        register_slot(_SEARCH_PROVIDER_SLOT, prepared_search_provider)

    # Register module-contributed UI translation catalogs (the `locales`
    # manifest key, mirroring `slots` above). Each entry maps a language code to
    # {"file": <path>, "rtl": <bool>}. Pushed into ui.i18n through a lazy import
    # so the i18n leaf never imports the module subsystem. A malformed or
    # unreadable entry is logged and skipped; the module and its other locales
    # still load.
    locales = manifest.get("locales") or {}
    if not isinstance(locales, dict):
        log.error(
            "Module %r 'locales' skipped: must be a dict, got %s",
            pkg_name, type(locales).__name__,
        )
        locales = {}
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
        try:
            catalog = json.loads(cat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error(
                "Module %r locale %r skipped: cannot read %s (%s: %s)",
                pkg_name, lang, entry["file"], type(exc).__name__, exc,
            )
            continue
        from ui.i18n import register_catalog
        register_catalog(lang, catalog, rtl=rtl)

    log.info(
        "Module %r loaded (v%s, slots: %s)",
        manifest["name"],
        manifest["version"],
        ", ".join(manifest.get("slots", {}).keys()) or "none",
    )
    return manifest


def _route_failure(manifest: dict, kind: str, exc: Exception) -> None:
    """Route-registration failure policy: a first-party module fails boot loudly
    (a boot without it is not a working product); a third-party module keeps
    load-and-continue, with the failure recorded for the Modules UI badge. The
    manifest carries its own first-party verdict (set by load_all), so the policy
    reads it directly rather than re-deriving trust here."""
    name = manifest["name"]
    if manifest.get("first_party"):
        raise ModuleLoadError(
            f"Default module {name!r} {kind} failed to register "
            f"({type(exc).__name__}: {exc})") from exc
    log.error("Module %r %s failed (%s: %s)", name, kind, type(exc).__name__, exc)
    _load_errors[name] = f"{kind} failed ({type(exc).__name__}: {exc})"


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

    Core routes are registered before modules, so a module can never shadow core;
    between two modules the first loaded wins and the second is recorded as a load
    error (failed badge). The refused module's just-added routes are rolled back so
    nothing half-registers."""
    manifest_key = f"{kind}_routes"
    setup_attr = f"setup_{kind}_routes"
    for manifest in loaded:
        route_mod_path = manifest.get(manifest_key)
        if not route_mod_path:
            continue
        name = manifest["name"]
        routes = None
        start = 0
        try:
            mod = importlib.import_module(route_mod_path)
            routes = app.router.routes
            existing = {k for r in routes for k in _route_keys(r)}
            start = len(routes)
            getattr(mod, setup_attr)(app)
        except Exception as exc:
            if routes is not None:
                del routes[start:]
            _route_failure(manifest, manifest_key, exc)
            continue
        clashes = sorted({
            path for r in routes[start:]
            for (path, _method) in _route_keys(r) & existing
        })
        if clashes:
            del routes[start:]
            _route_failure(manifest, manifest_key, RouteConflictError(
                "route path(s) already registered: " + ", ".join(clashes)))
            continue
        log.info("Module %r: %s routes registered", name, kind.upper())


def register_api_routes(app, loaded: list[dict]) -> None:
    """Register API routes from all loaded modules into the FastAPI app."""
    _register_module_routes(app, loaded, "api")


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


def _ast_scan_module_file(pkg_path: Path, dotted_module_path: str) -> set[str]:
    """Protected-BSL-internal imports reachable from the given entry module.

    Follows local (in-package) imports transitively and flags dynamic
    importlib.import_module / __import__ calls whose literal argument names a
    protected internal. Static analysis is best-effort; the authoritative
    enforcement of paid capabilities is server-side. Returns empty set if the
    entry file cannot be found or parsed.
    """
    parts = dotted_module_path.split(".")
    top_pkg = parts[0]
    # Directory of the importable top-level package. Flat layout: pkg_path IS
    # that package (its folder name == the package). Nested (GitHub-style)
    # layout: the package lives one level in, at pkg_path/<top_pkg>.
    pkg_dir = pkg_path if pkg_path.name == top_pkg else pkg_path / top_pkg

    def _resolve_local(module: str | None, level: int) -> Path | None:
        """The .py file inside the package that this import refers to, or None
        if it is not in-package (stdlib / third-party / escapes the package)."""
        if level > 1:
            return None  # from ..x import y - escapes the package
        if level == 1:
            rel = (module or "").split(".") if module else []
        else:
            p = (module or "").split(".")
            if not p or p[0] != top_pkg:
                return None  # absolute import of something outside the package
            rel = p[1:]
        base = pkg_dir.joinpath(*rel) if rel else pkg_dir
        for cand in (base.with_suffix(".py"), base / "__init__.py"):
            if cand.exists():
                return cand
        return None

    entry = _module_source_file(pkg_path, dotted_module_path)
    if entry is None:
        return set()

    violations: set[str] = set()
    seen: set[Path] = set()
    queue: list[Path] = [entry]
    while queue:
        f = queue.pop()
        if f in seen:
            continue
        seen.add(f)
        try:
            tree = ast.parse(f.read_text())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    hit = _protected_hit(alias.name)
                    if hit:
                        violations.add(hit)
                    local = _resolve_local(alias.name, 0)
                    if local:
                        queue.append(local)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                hit = _protected_hit(module)
                if hit:
                    violations.add(hit)
                local = _resolve_local(node.module, node.level)
                if local:
                    queue.append(local)
                # `from . import impl` / `from pkg import impl`: each name may be
                # a local submodule to follow.
                if node.level or (module.split(".")[:1] == [top_pkg]):
                    for alias in node.names:
                        target = (module + "." + alias.name) if module else alias.name
                        sub = _resolve_local(target, node.level)
                        if sub:
                            queue.append(sub)
            elif isinstance(node, ast.Call):
                _flag_dynamic_import(node, violations)
    return violations


# The search_provider slot has a stricter contract than the generic slots: a
# single descriptor (never a list), a closed key set, and a locally-owned async
# handler resolved at load time. The template linter mirrors these exactly.
_SEARCH_PROVIDER_SLOT = "search_provider"
_SEARCH_PROVIDER_KEYS = frozenset({"handler", "result_key", "permission"})
_SEARCH_RESULT_KEYS = frozenset({"items", "entries"})


def _prepare_search_provider(
    pkg_name: str, pkg_path: Path, contribution, *, trusted: bool
) -> dict:
    """Validate a ``search_provider`` descriptor and resolve its handler at load
    time, returning the runtime descriptor to register.

    The slot takes exactly one dict: one module, one provider, one results
    bucket, so a module can never overwrite its own search bucket. The descriptor
    must carry exactly ``{handler, result_key, permission}`` (extra keys are
    rejected, never ignored, so a misspelling fails loudly); ``result_key`` is one
    of ``{items, entries}``; ``permission`` is a known key. The handler
    ``module:function`` must name source that lives inside this module's own tree,
    must import no protected BSL internal (third-party only), and must resolve at
    load to an async callable. A provider that cannot pass all of this fails
    module load rather than first surfacing as a degraded source when a user types
    into search. Raises :class:`ModuleLoadError` on any violation.
    """
    if not isinstance(contribution, dict):
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} takes exactly one descriptor dict, "
            f"not a {type(contribution).__name__}."
        )
    keys = set(contribution)
    if keys != set(_SEARCH_PROVIDER_KEYS):
        missing = sorted(_SEARCH_PROVIDER_KEYS - keys)
        extra = sorted(keys - _SEARCH_PROVIDER_KEYS)
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} descriptor keys must be exactly "
            f"{sorted(_SEARCH_PROVIDER_KEYS)} (missing={missing}, extra={extra})."
        )
    result_key = contribution["result_key"]
    if result_key not in _SEARCH_RESULT_KEYS:
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} result_key {result_key!r} must be one "
            f"of {sorted(_SEARCH_RESULT_KEYS)}."
        )
    perm = contribution["permission"]
    if not is_permission_key(perm):
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} names unknown permission key {perm!r}."
        )
    handler_path = contribution["handler"]
    if not isinstance(handler_path, str) or handler_path.count(":") != 1:
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} handler {handler_path!r} must be "
            f"'module.path:function'."
        )
    module_path, func_name = handler_path.split(":", 1)
    if not module_path or not func_name:
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} handler {handler_path!r} must name a "
            f"non-empty module path and function."
        )
    # Local ownership: the handler must resolve to source inside this module's own
    # tree, so a manifest can never point core at 'celerp.some_internal:fn' and
    # have it imported on demand around the protected-import gate.
    if _module_source_file(pkg_path, module_path) is None:
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} handler {handler_path!r} does not "
            f"resolve to source inside module {pkg_name!r}."
        )
    # The handler is a lazily-imported entry point, so it goes through the same
    # transitive protected-BSL scan as the route entry modules (third-party only).
    if not trusted:
        violations = _ast_scan_module_file(pkg_path, module_path)
        if violations:
            raise ModuleLoadError(
                f"Slot {_SEARCH_PROVIDER_SLOT!r} handler imports protected BSL "
                f"internals ({', '.join(sorted(violations))})."
            )
    try:
        handler = resolve_handler(handler_path)
    except Exception as exc:
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} handler {handler_path!r} failed to "
            f"resolve ({type(exc).__name__})."
        )
    if not callable(handler):
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} handler {handler_path!r} is not callable."
        )
    if not inspect.iscoroutinefunction(handler):
        raise ModuleLoadError(
            f"Slot {_SEARCH_PROVIDER_SLOT!r} handler {handler_path!r} must be async; "
            f"the aggregator awaits it."
        )
    # Runtime-owned trust metadata is injected AFTER the manifest contribution, and
    # the closed key set above already rejects a manifest that tries to supply
    # _module / _first_party itself, so neither can be spoofed.
    return {**contribution, "_module": pkg_name, "_first_party": trusted}

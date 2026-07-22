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
import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path

from celerp.modules.license import check_license, exchange_api_key_for_jwt, is_premium_path
from celerp.modules.slots import register as register_slot

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


def _trusted_default_names() -> frozenset[str]:
    """Names of first-party modules, taken from the bundled SOURCE directories.

    Electron seeds default modules into DATA_DIR/modules (the user-writable
    module dir). Trust for those seeded copies is granted BY NAME against the
    bundled source listing - never by directory. Blanket-trusting MODULE_DIR
    would exempt user-imported third-party modules from the BSL import checks,
    which is exactly what those checks exist to prevent. The importer refuses
    the celerp- prefix for imports, so a third-party package cannot take a
    first-party name through the app.
    """
    names: set[str] = set()
    for d in _BUNDLED_MODULES_DIRS:
        if d.exists():
            for p in d.iterdir():
                if p.is_dir() and (p / "__init__.py").exists():
                    names.add(p.name)
    return frozenset(names)

# Loaded manifests — populated by load_all()
_loaded: list[dict] = []

# Proprietary cloud components folded into core: wired directly at app construction (celerp/main.py,
# ui/app.py), never loaded as pluggable/replaceable modules.
_CORE_FOLDED: frozenset[str] = frozenset({"celerp-ai", "celerp-backup", "celerp-connectors"})


class ModuleLoadError(Exception):
    """Raised (and caught) when a module fails validation."""


def _read_depends_on(pkg_path: Path) -> list[str]:
    """Extract PLUGIN_MANIFEST['depends_on'] from a module's __init__.py via AST.

    Returns empty list if the manifest or key is absent or unparseable.
    """
    init_file = pkg_path / "__init__.py"
    try:
        tree = ast.parse(init_file.read_text())
    except Exception:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PLUGIN_MANIFEST":
                try:
                    manifest = ast.literal_eval(node.value)
                    return list(manifest.get("depends_on") or [])
                except Exception:
                    return []
    return []


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
            log.warning("Module %r is part of a dependency cycle — skipping", name)
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


def default_module_names() -> frozenset[str]:
    """Public: names of first-party bundled modules (in-tree or seeded copies)."""
    return _trusted_default_names()


def loaded_modules() -> list[dict]:
    """Return manifests of all successfully loaded modules."""
    return list(_loaded)


def is_core_folded(pkg_name: str) -> bool:
    """True if a module is a proprietary cloud component folded into core (ai/backup/
    connectors). These are wired directly into the app at construction, so they never
    appear in ``loaded_modules()`` — yet they ARE running whenever the app is up."""
    return pkg_name in _CORE_FOLDED


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
    "depends_on", "license", "min_celerp_version",
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

    # Support comma-separated module directories
    raw = str(module_dir)
    dirs = [Path(d.strip()) for d in raw.split(",") if d.strip()]

    resolved_bundled = {d.resolve() for d in _BUNDLED_MODULES_DIRS if d.exists()}

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
            if p.name in _CORE_FOLDED:
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

    # Sort by dependency order (skip reasons land in _load_errors)
    ordered = _topo_sort(candidate_paths, enabled, errors=_load_errors)

    trusted_names = _trusted_default_names()

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
        trusted = (
            pkg_path.parent.resolve() in resolved_bundled
            or pkg_name in trusted_names
        )
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
                    "Premium module %r: no relay identity (never activated) — "
                    "skipping license check (dev mode)",
                    pkg_name,
                )
        try:
            manifest = _load_one(pkg_path, pkg_name, trusted=trusted)
        except ModuleLoadError as exc:
            # A default module IS the product (a boot without documents is not
            # a working app): fail startup naming the module and error.
            # Third-party modules keep load-and-continue; their failure shows
            # as the failed badge in the Modules UI.
            if pkg_name in trusted_names:
                raise ModuleLoadError(
                    f"Default module {pkg_name!r} failed to load: {exc}") from exc
            _load_errors[pkg_name] = str(exc)
            manifest = None
        if manifest is not None:
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

    # Register extension slots
    for slot_name, contribution in (manifest.get("slots") or {}).items():
        if isinstance(contribution, dict):
            register_slot(slot_name, {**contribution, "_module": pkg_name})
        elif isinstance(contribution, list):
            for item in contribution:
                register_slot(slot_name, {**item, "_module": pkg_name})

    log.info(
        "Module %r loaded (v%s, slots: %s)",
        manifest["name"],
        manifest["version"],
        ", ".join(manifest.get("slots", {}).keys()) or "none",
    )
    return manifest


def _route_failure(name: str, kind: str, exc: Exception) -> None:
    """Route-registration failure policy: a default module fails boot loudly
    (a boot without it is not a working product); a third-party module keeps
    load-and-continue, with the failure recorded for the Modules UI badge."""
    if name in _trusted_default_names():
        raise ModuleLoadError(
            f"Default module {name!r} {kind} failed to register "
            f"({type(exc).__name__}: {exc})") from exc
    log.error("Module %r %s failed (%s: %s)", name, kind, type(exc).__name__, exc)
    _load_errors[name] = f"{kind} failed ({type(exc).__name__}: {exc})"


def register_api_routes(app, loaded: list[dict]) -> None:
    """Register API routes from all loaded modules into the FastAPI app."""
    for manifest in loaded:
        route_mod_path = manifest.get("api_routes")
        if not route_mod_path:
            continue
        try:
            mod = importlib.import_module(route_mod_path)
            mod.setup_api_routes(app)
            log.info("Module %r: API routes registered", manifest["name"])
        except Exception as exc:
            _route_failure(manifest["name"], "api_routes", exc)


def register_ui_routes(app, loaded: list[dict]) -> None:
    """Register UI routes from all loaded modules into the FastHTML app."""
    for manifest in loaded:
        route_mod_path = manifest.get("ui_routes")
        if not route_mod_path:
            continue
        try:
            mod = importlib.import_module(route_mod_path)
            mod.setup_ui_routes(app)
            log.info("Module %r: UI routes registered", manifest["name"])
        except Exception as exc:
            _route_failure(manifest["name"], "ui_routes", exc)


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

    entry = pkg_dir.joinpath(*parts[1:]).with_suffix(".py")
    if not entry.exists():
        entry = pkg_dir.joinpath(*parts[1:], "__init__.py")
    if not entry.exists():
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

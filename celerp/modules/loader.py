# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
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

from celerp.modules.license import check_license, is_premium_path
from celerp.modules.slots import register as register_slot

log = logging.getLogger(__name__)

# BSL internals that modules are NOT allowed to import.
# Importing these bypasses revenue gates (session token, AI quota).
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
_BUNDLED_MODULES_DIRS: tuple[Path, ...] = (
    Path(__file__).resolve().parent.parent.parent / "default_modules",
)

# Loaded manifests — populated by load_all()
_loaded: list[dict] = []


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


def _topo_sort(pkg_paths: list[Path], enabled: set[str]) -> list[Path]:
    """Return pkg_paths sorted so dependencies come before dependents.

    Modules with unresolvable hard deps are excluded with a warning.
    """
    path_by_name = {p.name: p for p in pkg_paths}
    deps_by_name: dict[str, list[str]] = {}
    for p in pkg_paths:
        deps_by_name[p.name] = _read_depends_on(p)

    result: list[Path] = []
    visited: set[str] = set()
    skipped: set[str] = set()

    def _visit(name: str) -> None:
        if name in visited or name in skipped:
            return
        visited.add(name)
        for dep in deps_by_name.get(name, []):
            if dep not in enabled:
                log.warning(
                    "Module %r requires %r which is not enabled — skipping %r",
                    name, dep, name,
                )
                skipped.add(name)
                visited.discard(name)
                return
            if dep not in path_by_name:
                log.warning(
                    "Module %r requires %r which is not installed — skipping %r",
                    name, dep, name,
                )
                skipped.add(name)
                visited.discard(name)
                return
            _visit(dep)
            if dep in skipped:
                log.warning(
                    "Module %r requires %r which was skipped — skipping %r",
                    name, dep, name,
                )
                skipped.add(name)
                visited.discard(name)
                return
        result.append(path_by_name[name])

    for p in pkg_paths:
        _visit(p.name)

    return result


def loaded_modules() -> list[dict]:
    """Return manifests of all successfully loaded modules."""
    return list(_loaded)


def load_all(module_dir: str | Path, enabled: set[str]) -> list[dict]:
    """Scan module directories for enabled modules, import them, register slots.

    Args:
        module_dir: Comma-separated paths or single path to module directories.
        enabled: Set of module names that should be loaded.

    Returns:
        List of successfully loaded PLUGIN_MANIFEST dicts.
    """
    _loaded.clear()

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
            # Each module dir (e.g. default_modules/celerp-inventory/) must be on
            # sys.path so that its inner packages (e.g. celerp_inventory) are
            # importable by importlib.import_module when routes are registered.
            p_str = str(p)
            if p_str not in sys.path:
                sys.path.insert(0, p_str)
            candidate_paths.append(p)

    # Sort by dependency order
    ordered = _topo_sort(candidate_paths, enabled)

    for pkg_path in ordered:
        pkg_name = pkg_path.name
        trusted = pkg_path.parent.resolve() in resolved_bundled
        # License gate for premium modules (only when relay credentials configured)
        if is_premium_path(pkg_path):
            relay_url = os.environ.get("CELERP_RELAY_URL", "")
            instance_jwt = os.environ.get("CELERP_INSTANCE_JWT", "")
            data_dir = os.environ.get("DATA_DIR", "/tmp/celerp-data")
            if relay_url and instance_jwt:
                if not check_license(
                    slug=pkg_name,
                    relay_url=relay_url,
                    instance_jwt=instance_jwt,
                    cache_dir=Path(data_dir),
                ):
                    log.warning(
                        "Premium module %r skipped: no valid license", pkg_name
                    )
                    continue
            else:
                log.debug(
                    "Premium module %r: CELERP_RELAY_URL/CELERP_INSTANCE_JWT not set"
                    " — skipping license check (dev mode)", pkg_name,
                )
        try:
            manifest = _load_one(pkg_path, pkg_name, trusted=trusted)
        except ModuleLoadError:
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

    except ModuleLoadError as exc:
        log.error("Module %r rejected: %s", pkg_name, exc)
        sys.modules.pop(pkg_name, None)
        return None
    except Exception as exc:
        log.error("Module %r failed to import (%s: %s) — skipping", pkg_name, type(exc).__name__, exc)
        sys.modules.pop(pkg_name, None)
        return None

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
        return None

    # Validate required manifest fields
    missing = [f for f in ("name", "version") if not manifest.get(f)]
    if missing:
        log.error("Module %r manifest missing required fields: %s — skipping", pkg_name, missing)
        sys.modules.pop(pkg_name, None)
        return None

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
            log.error("Module %r api_routes failed (%s: %s)", manifest["name"], type(exc).__name__, exc)


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
            log.error("Module %r ui_routes failed (%s: %s)", manifest["name"], type(exc).__name__, exc)


def _ast_scan_module_file(pkg_path: Path, dotted_module_path: str) -> set[str]:
    """Return set of protected BSL internal imports found anywhere in the given module file.

    Resolves dotted_module_path (e.g. 'celerp_foo.routes') to a .py file relative
    to pkg_path, then walks all AST Import/ImportFrom nodes at any depth.
    Returns empty set if the file cannot be found or parsed.
    """
    # Resolve dotted path to a file path relative to pkg_path's parent (module_dir)
    parts = dotted_module_path.split(".")
    # The first part is the package name (same as pkg_path.name)
    if parts[0] == pkg_path.name:
        rel_parts = parts[1:]
    else:
        rel_parts = parts

    candidate = pkg_path.joinpath(*rel_parts).with_suffix(".py")
    if not candidate.exists():
        candidate = pkg_path.joinpath(*rel_parts, "__init__.py")
    if not candidate.exists():
        return set()

    try:
        tree = ast.parse(candidate.read_text())
    except Exception:
        return set()

    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for protected in _PROTECTED_BSL_INTERNALS:
                    if alias.name == protected or alias.name.startswith(protected + "."):
                        violations.add(protected)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for protected in _PROTECTED_BSL_INTERNALS:
                if module == protected or module.startswith(protected + "."):
                    violations.add(protected)
    return violations

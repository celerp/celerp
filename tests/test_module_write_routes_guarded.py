# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every write route a module serves names who may call it: a require_permission key,
or require_install_owner for installation-wide operations. The walk covers the routes
mounted on the app and every router a module defines, mounted or not, so a router
switched on later is held to the same rule."""
from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import APIRouter

from celerp.ai.tools import _api_routes, _route_permissions
from celerp.main import app
from celerp.services.auth import require_install_owner

_MODULES = Path(__file__).resolve().parent.parent / "default_modules"
_WRITES = {"POST", "PUT", "PATCH", "DELETE"}

# Write routes that name no permission, each with the reason it needs none.
_ALLOWED = {
    ("POST", "celerp_backup.routes:import_backup_bootstrap"):
        "restores a backup into an installation that has no users yet; the setup code authorizes it",
    ("POST", "celerp_inventory.routes:set_item_price"):
        "decides per request: set_inventory_prices, or edit_inventory for the cost of a draft being entered",
}


def _install_owner_only(route) -> bool:
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is require_install_owner:
            return True
        stack.extend(dep.dependencies)
    return False


def _module_routers() -> list[APIRouter]:
    routers = []
    for path in sorted(_MODULES.glob("*/celerp_*/**/*.py")):
        if "tests" in path.parts:
            continue
        rel = path.relative_to(_MODULES / path.relative_to(_MODULES).parts[0]).with_suffix("")
        mod = importlib.import_module(".".join(rel.parts).removesuffix(".__init__"))
        routers += [v for v in vars(mod).values() if isinstance(v, APIRouter)]
    return routers


def _write_routes():
    routes = {id(r): r for _, _, r in _api_routes(app) if r.endpoint.__module__.split(".")[0] != "celerp"}
    for router in _module_routers():
        routes.update({id(r): r for r in router.routes if hasattr(r, "dependant")})
    for route in routes.values():
        for method in route.methods & _WRITES:
            yield method, f"{route.endpoint.__module__}:{route.endpoint.__name__}", route


def test_every_module_write_route_names_who_may_call_it():
    unguarded = {(method, name) for method, name, route in _write_routes()
                 if not _route_permissions(route) and not _install_owner_only(route)}
    assert unguarded == set(_ALLOWED)

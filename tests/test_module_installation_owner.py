# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Installing, removing and restarting are installation-wide: only the
installation owner may do them. Company admins choose which installed modules
their own company uses."""
from __future__ import annotations

import io

import pytest
from fastapi.routing import APIRoute

from test_helpers import invite_user, register_admin

# (method, path, request kwargs) for every action on installed module code or
# the running installation.
_INSTALLATION_ACTIONS = [
    ("post", "/companies/me/modules/import",
     {"files": {"file": ("m.zip", io.BytesIO(b"not a zip"), "application/zip")}}),
    ("post", "/companies/me/modules/import-path", {"json": {"path": "/nonexistent/module"}}),
    ("post", "/companies/me/modules/buy", {"json": {"slug": "some-module"}}),
    ("post", "/companies/me/modules/marketplace-download", {"json": {"slug": "some-module"}}),
    ("post", "/companies/me/modules/marketplace-install", {"json": {"path": "/nonexistent.zip"}}),
    ("post", "/companies/me/modules/no-such-module/delete", {}),
    ("post", "/companies/me/modules/no-such-module/purge-data", {}),
    ("post", "/system/restart", {}),
]
_IDS = [path for _m, path, _k in _INSTALLATION_ACTIONS]

# Company-scoped module routes: a company admin's own company only.
_COMPANY_SCOPED = {
    ("GET", "/companies/me/modules"),
    ("GET", "/companies/me/modules/licenses"),
    ("POST", "/companies/me/modules/{module_name}/enable"),
    ("POST", "/companies/me/modules/{module_name}/disable"),
}


@pytest.fixture(autouse=True)
def _no_real_restart(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr("celerp.routers.system._send_sigterm", lambda: calls.append(1))
    return calls


async def _company_admin_headers(client, session) -> tuple[dict, dict]:
    owner_h = {"Authorization": f"Bearer {await register_admin(client)}"}
    admin_token = await invite_user(client, session, owner_h, "co-admin@example.test", "admin")
    return owner_h, {"Authorization": f"Bearer {admin_token}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,kwargs", _INSTALLATION_ACTIONS, ids=_IDS)
async def test_company_admin_cannot_change_installed_modules(client, session, method, path, kwargs):
    _owner_h, admin_h = await _company_admin_headers(client, session)
    r = await getattr(client, method)(path, headers=admin_h, **kwargs)
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,kwargs", _INSTALLATION_ACTIONS, ids=_IDS)
async def test_installation_owner_passes_the_owner_check(client, session, method, path, kwargs):
    owner_h, _admin_h = await _company_admin_headers(client, session)
    r = await getattr(client, method)(path, headers=owner_h, **kwargs)
    assert r.status_code != 403, r.text


@pytest.mark.asyncio
async def test_company_admin_restart_does_not_restart(client, session, _no_real_restart):
    _owner_h, admin_h = await _company_admin_headers(client, session)
    await client.post("/system/restart", headers=admin_h)
    assert _no_real_restart == []


def _depends_on(dependant, target) -> bool:
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is target:
            return True
        stack.extend(dep.dependencies)
    return False


def _module_and_restart_routes():
    """Walk the owning routers, mounted at /companies and /system in celerp.main:
    newer Starlette keeps an included router as one opaque entry in app.routes."""
    from celerp.routers import companies, system
    for prefix, router in (("/companies", companies.router), ("/system", system.router)):
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            path = prefix + route.path
            if path.startswith("/companies/me/modules") or path == "/system/restart":
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    yield method, path, route


def test_every_module_installation_route_requires_the_installation_owner():
    """A new route under the modules API is installation-wide unless it is
    listed as company-scoped here, so it cannot land without the owner check."""
    from celerp.services.auth import require_install_owner

    seen = set()
    for method, path, route in _module_and_restart_routes():
        seen.add((method, path))
        if (method, path) in _COMPANY_SCOPED:
            assert not _depends_on(route.dependant, require_install_owner), path
            continue
        assert _depends_on(route.dependant, require_install_owner), (
            f"{method} {path} changes the installation but does not require the installation owner")
    assert {(m.upper(), p.replace("no-such-module", "{module_name}")) for m, p, _k in _INSTALLATION_ACTIONS} <= seen
    assert _COMPANY_SCOPED <= seen

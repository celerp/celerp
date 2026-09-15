# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Cluster 3 - health/settings router split and readiness hardening (Phase C).

Target (post-fix) behavior:
- Every /settings/* route requires an authenticated user. At merge-base ea480c48
  the 15 /settings/* routes sit on the anonymous health router, so an
  unauthenticated probe returns something other than 401 (200, 402, 502, or a
  relay error body). RED at base.
- /health/system is authenticated (it returns host RAM/CPU/disk). Anonymous at
  base. RED at base.
- The readiness probes return a generic 503 body and never interpolate the raw
  database exception (which can carry the DSN). At base the exception is
  formatted into the body. RED at base.
- backup-status carries manage_company_settings specifically, not the weaker
  run_backups - it returns the backup encryption key.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from celerp.db import get_session
from celerp.main import app  # noqa: F401 - ensures the app (and its routers) import
from celerp.routers.health import settings_router


def _settings_routes() -> list[tuple[str, str]]:
    """(method, concrete-path) for every /settings/* route, path params filled.

    Enumerated from the settings_router itself - the app registers routes through
    a lazy include wrapper, so its top-level .routes are not APIRoute instances.
    """
    out: list[tuple[str, str]] = []
    for route in settings_router.routes:
        if not isinstance(route, APIRoute):
            continue
        assert route.path.startswith("/settings/"), route.path
        concrete = route.path.replace("{platform}", "shopify")
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, concrete))
    return out


@pytest.mark.asyncio
async def test_every_settings_route_requires_auth(client):
    """Anonymous request to any /settings/* route is rejected 401, never served."""
    routes = _settings_routes()
    assert len(routes) >= 15, f"expected the full settings surface, found {len(routes)}"
    for method, path in routes:
        r = await client.request(method, path)
        assert r.status_code == 401, f"{method} {path} must require auth, got {r.status_code}"


@pytest.mark.asyncio
async def test_settings_route_served_when_authenticated(client):
    """A benign authenticated /settings read is served (proves the dependency
    gates on identity, not that the route is simply broken)."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": "SplitCo", "email": "split@example.com", "name": "Admin", "password": "pw"},
    )
    assert reg.status_code == 200
    h = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    r = await client.get("/settings/cloud-instance-id", headers=h)
    assert r.status_code == 200, r.text
    assert r.json().get("instance_id")


@pytest.mark.asyncio
async def test_health_system_requires_auth(client):
    """/health/system exposes host RAM/CPU/disk and must reject an anonymous probe."""
    r = await client.get("/health/system")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_backup_status_requires_manage_company_settings(client):
    """backup-status returns the backup encryption key, so a viewer holding the
    weaker run_backups permission is still refused; it is gated on
    manage_company_settings (admin floor)."""
    from test_helpers import make_authed_token

    reg = await client.post(
        "/auth/register",
        json={"company_name": "BackupCo", "email": "backupowner@example.com", "name": "Owner", "password": "pw"},
    )
    owner_h = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    # A viewer member cannot read the key even though run_backups floors at viewer.
    r_new = await client.post(
        "/companies/me/users",
        json={"email": "viewer-backup@example.com", "name": "Viewer", "role": "viewer", "password": "pw123"},
        headers=owner_h,
    )
    assert r_new.status_code == 200, r_new.text
    r_login = await client.post("/auth/login", json={"email": "viewer-backup@example.com", "password": "pw123"})
    viewer_h = {"Authorization": f"Bearer {r_login.json()['access_token']}"}

    r = await client.get("/settings/backup-status", headers=viewer_h)
    assert r.status_code == 403, r.text

    # The owner (admin+) can read it.
    r_ok = await client.get("/settings/backup-status", headers=owner_h)
    assert r_ok.status_code == 200, r_ok.text


@pytest.mark.asyncio
async def test_readiness_body_is_generic_on_db_failure(client, session):
    """/health/ready returns a generic 503 body and never leaks the raw database
    exception (which can carry the connection DSN)."""

    class _BoomSession:
        async def execute(self, *a, **k):
            raise RuntimeError("could not connect: postgresql://celerp:secret@db/leak")

    async def _boom():
        yield _BoomSession()

    app.dependency_overrides[get_session] = _boom
    try:
        r = await client.get("/health/ready")
    finally:
        # Restore the shared-session override the client fixture installed.
        app.dependency_overrides[get_session] = lambda: session
    assert r.status_code == 503, r.text
    body = r.text
    assert "secret" not in body and "postgresql://" not in body, f"leaked DSN: {body}"
    assert r.json()["detail"] == "Service not ready."

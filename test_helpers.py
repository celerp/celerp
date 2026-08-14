# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared test utilities importable from any test location.

This file lives at the repo root. It is on the pytest pythonpath (pyproject.toml:
  pythonpath = [".", ".."]) so any test file can import it directly:

    from test_helpers import make_test_token, authed_cookies, REPO_ROOT

tests/helpers.py is a shim that re-exports from here for backward compatibility.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

# Repo root: this file lives at repo root, so parent == repo root.
REPO_ROOT = Path(__file__).resolve().parent

# The root conftest always sets DATABASE_URL to the test Postgres before this is
# imported; the default is just a sane placeholder for the Postgres-only app.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp")

_crm_src = os.path.join(os.path.dirname(__file__), "..", "premium_modules", "celerp-sales-funnel")
_crm_available = os.path.isfile(os.path.join(_crm_src, "celerp_sales_funnel", "__init__.py"))


def make_test_token(
    role: str = "owner",
    user_id: str = "00000000-0000-0000-0000-000000000001",
    company_id: str = "00000000-0000-0000-0000-000000000002",
    modules: list[str] | None = None,
) -> str:
    """Create a minimal JWT cookie value with a decodable payload for UI role checks.

    NOT cryptographically signed - only used so that get_role() can decode
    the role claim from the base64 payload. Signature verification never runs in tests.
    """
    payload_dict: dict = {"sub": user_id, "company_id": company_id, "role": role, "exp": 9999999999}
    if modules is not None:
        payload_dict["modules"] = modules
    payload = json.dumps(payload_dict)
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    return f"header.{payload_b64}.sig"


def authed_cookies(role: str = "owner") -> dict:
    """Return cookies dict with a properly-formed test token for the given role."""
    return {"celerp_token": make_test_token(role=role)}


async def ensure_user(session, user_id) -> None:
    """Insert a minimal users row if absent.

    Postgres enforces foreign keys (e.g. session_registry.user_id → users) that
    SQLite silently ignored, so tests that reference a synthetic user id must
    materialize it first.
    """
    import uuid as _uuid
    from celerp.models.company import User
    uid = _uuid.UUID(str(user_id))
    if await session.get(User, uid) is None:
        session.add(User(id=uid, email=f"u-{uid}@test.local", name="Test User"))
        await session.flush()


async def default_location_id(client, headers: dict) -> str:
    """Return the company's real default (or first) location id.

    Postgres enforces ledger.location_id → locations, so tests must use a real
    location rather than a random UUID. Registration seeds a 'Head Office'.
    """
    r = await client.get("/companies/me/locations", headers=headers)
    items = r.json().get("items", [])
    for it in items:
        if it.get("is_default"):
            return it["id"]
    return items[0]["id"]


async def create_location(client, headers: dict, name: str = "Warehouse 2") -> str:
    """Create a real location and return its id (for transfer-target tests)."""
    r = await client.post("/companies/me/locations", headers=headers,
                          json={"name": name, "type": "warehouse"})
    return r.json()["id"]


# ── Role/permission bootstrap ─────────────────────────────────────────────────

async def register_admin(client) -> str:
    """Register first-admin (creates owner role) and return access token."""
    r = await client.post(
        "/auth/register",
        json={"company_name": "Perm Co", "email": "admin@perm.com", "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def invite_user(client, session, admin_headers: dict, email: str, role: str) -> str:
    """Create a user with the given role and return their access token."""
    from celerp.services.session_tracker import clear as _clear_tracker
    r = await client.post(
        "/companies/me/users",
        json={"email": email, "name": role.title(), "role": role, "password": "pw123"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    await _clear_tracker(session)  # clear so the new user can log in
    r2 = await client.post("/auth/login", json={"email": email, "password": "pw123"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


async def create_item(client, headers: dict, location_id: str, sku: str = "SKU-PERM") -> str:
    """Create an item as admin and return its entity_id."""
    r = await client.post(
        "/items",
        json={"sku": sku, "name": "Perm Item", "quantity": 5, "location_id": location_id, "cost_price": 100.0, "sell_by": "piece"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def perm_setup(client, session):
    """Bootstrap: admin token, manager token, operator token, location_id, item_id."""
    admin_tok = await register_admin(client)
    admin_h = {"Authorization": f"Bearer {admin_tok}"}

    loc_r = await client.post(
        "/companies/me/locations",
        json={"name": "Main", "type": "warehouse", "address": None, "is_default": True},
        headers=admin_h,
    )
    location_id = loc_r.json()["id"]
    item_id = await create_item(client, admin_h, location_id)

    manager_tok = await invite_user(client, session, admin_h, "mgr@perm.com", "manager")
    operator_tok = await invite_user(client, session, admin_h, "operator@perm.com", "operator")

    return {
        "admin_h":    {"Authorization": f"Bearer {admin_tok}"},
        "manager_h":  {"Authorization": f"Bearer {manager_tok}"},
        "staff_h":    {"Authorization": f"Bearer {operator_tok}"},   # alias for legacy tests
        "operator_h": {"Authorization": f"Bearer {operator_tok}"},
        "location_id": location_id,
        "item_id": item_id,
    }


async def grant_permission(client, admin_headers: dict, permission: str, role: str) -> None:
    """Set *permission* so exactly *role* and the roles above it hold it, through the
    owner-gated permissions matrix endpoint.

    Role grants are per-role now, so this helper reproduces the single threshold its
    callers rely on: it sets each role's cell to granted when the role is at or above
    *role* (clamped up to the permission's floor) and to revoked when it is below.
    That is the exact effect the old threshold override produced - every role above
    the line holds the permission and every role below it does not - so callers that
    "raise a key above operator" keep working unchanged. admin_headers must be an
    owner token (register_admin creates the owner); grants can only be written
    through this endpoint.
    """
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import PERMISSIONS

    perm = next(p for p in PERMISSIONS if p.key == permission)
    target = max(ROLE_LEVELS[role], ROLE_LEVELS[perm.floor_role])
    for role_key, level in ROLE_LEVELS.items():
        r = await client.patch(
            "/companies/me/role-permissions",
            json={"perm_key": permission, "role_key": role_key, "granted": level >= target},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text

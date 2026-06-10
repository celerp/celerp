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

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

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

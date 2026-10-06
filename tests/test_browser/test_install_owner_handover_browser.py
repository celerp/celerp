# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The installation owner hands installation ownership to another user from the
Users tab: confirmed first, redrawn in place without a reload, and the control
then belongs to the new owner, who can hand it back."""
from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import clear_session_registry

pytestmark = pytest.mark.browser

_WAIT_MS = 15_000


def _user_id(client: httpx.Client, email: str) -> str:
    return next(u["id"] for u in client.get("/companies/me/users").json()["items"] if u["email"] == email)


def test_installation_owner_hands_over_on_the_users_page(page, api, api_server, seeded_user):
    email, password = f"heir-{uuid.uuid4().hex[:8]}@example.com", "heir-pass-12345"
    r = api.post("/companies/me/users",
                 json={"email": email, "name": "Heir User", "role": "admin", "password": password})
    assert r.status_code == 200, r.text
    heir_id, my_id = _user_id(api, email), _user_id(api, seeded_user["email"])
    clear_session_registry()
    heir_token = httpx.post(f"{api_server}/auth/login",
                            json={"email": email, "password": password}).json()["access_token"]
    heir = httpx.Client(base_url=api_server, headers={"Authorization": f"Bearer {heir_token}"})
    try:
        page.goto("/settings/general?tab=users", wait_until="domcontentloaded")
        page.evaluate("window.__notReloaded = true")
        button = page.locator(f'[hx-post="/settings/users/{heir_id}/installation-owner"]')
        button.wait_for(timeout=_WAIT_MS)
        assert page.locator(f'[hx-post="/settings/users/{my_id}/installation-owner"]').count() == 0

        dialogs: list[str] = []
        page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        button.click()
        page.wait_for_timeout(500)
        assert dialogs and "Heir User" in dialogs[0]
        assert api.get("/system/installation-owner").json()["installation_owner"] is True

        page.once("dialog", lambda d: d.accept())
        button.click()
        page.wait_for_selector('#users-card .flash--success:has-text("Heir User is now the installation owner")',
                               timeout=_WAIT_MS)
        assert page.evaluate("window.__notReloaded") is True
        assert page.locator('#users-card [hx-post$="/installation-owner"]').count() == 0
        assert heir.get("/system/installation-owner").json()["installation_owner"] is True
        assert api.get("/system/installation-owner").json()["installation_owner"] is False
    finally:
        # The new owner hands it back, so the rest of the session keeps its owner.
        heir.post(f"/companies/me/users/{my_id}/installation-owner")
        heir.close()
    assert api.get("/system/installation-owner").json()["installation_owner"] is True

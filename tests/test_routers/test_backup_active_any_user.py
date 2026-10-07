# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""While a backup runs, writes pause for every login, so every signed-in page shows the
backup banner: any signed-in login can read whether a backup is running, and only that."""

from __future__ import annotations

import pytest
from unittest.mock import patch


async def _second_owner(client) -> dict:
    """A company owner who is not the installation owner."""
    reg = await client.post("/auth/register", json={
        "company_name": "BannerCo", "email": "banner-root@example.com", "name": "Root", "password": "pwvalid1"})
    root = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    r = await client.post("/companies/me/users", headers=root, json={
        "email": "banner-owner@example.com", "name": "Owner", "role": "owner", "password": "pw123val"})
    assert r.status_code == 200, r.text
    login = await client.post("/auth/login", json={"email": "banner-owner@example.com", "password": "pw123val"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.asyncio
async def test_any_signed_in_login_reads_whether_a_backup_is_running(client):
    headers = await _second_owner(client)

    idle = await client.get("/settings/backup-active", headers=headers)
    with patch("celerp.services.backup_state.is_active", return_value=True):
        running = await client.get("/settings/backup-active", headers=headers)

    assert idle.status_code == 200 and idle.json() == {"active": False}, idle.text
    assert running.status_code == 200 and running.json() == {"active": True}, running.text
    assert (await client.get("/settings/backup-active")).status_code == 401


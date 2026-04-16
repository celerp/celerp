# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import pytest


async def _register(client):
    r = await client.post("/auth/register", json={"company_name": "Scan Extra", "email": f"x-{uuid.uuid4().hex[:8]}@scan.test", "name": "Admin", "password": "pw"})
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.skip(reason="Scanning module disabled until complete")
@pytest.mark.asyncio
async def test_scan_batch_endpoint_and_uuid_location_branch(client):
    token = await _register(client)
    loc = await client.post("/companies/me/locations", headers=_h(token), json={"name": "S", "type": "warehouse", "is_default": True})
    loc_id = loc.json()["id"]
    r = await client.post("/scanning/scan/batch", headers=_h(token), json={"scans": [
        {"code": "A", "location_id": "not-a-uuid", "raw": {}},
        {"code": "B", "location_id": loc_id, "raw": {}},
    ]})
    assert r.status_code == 200 and r.json()["created"] == 2

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A company whose settings hold no enabled_modules (every company upgraded from v2.5.3)
runs every loaded module. Setting its business type or applying a preset adds the
preset's modules to the ones it runs; it never switches a running module off."""
from __future__ import annotations

import uuid

import pytest


async def _company_without_module_settings(client, session) -> tuple[dict, str]:
    from celerp.models.company import Company

    r = await client.post("/auth/register", json={"company_name": "Upgraded Co",
                                                  "email": f"preset-{uuid.uuid4().hex[:8]}@test.test",
                                                  "name": "Admin", "password": "pw123val"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    company_id = (await client.get("/companies/me", headers=headers)).json()["id"]
    company = await session.get(Company, uuid.UUID(company_id))
    company.settings = {k: v for k, v in (company.settings or {}).items() if k != "enabled_modules"}
    await session.commit()
    return headers, company_id


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["business-type", "apply-preset"])
async def test_preset_keeps_every_running_module(client, session, monkeypatch, transport):
    from celerp.models.company import Company
    from celerp.modules import loader
    from celerp.modules.registry import get_enabled

    running = set(loader.first_party_names())
    assert "celerp-manufacturing" in running
    monkeypatch.setattr(loader, "_loaded", [{"name": n} for n in sorted(running)])
    headers, company_id = await _company_without_module_settings(client, session)

    if transport == "business-type":
        r = await client.post("/companies/me/business-type", headers=headers, json={"vertical": "artwork"})
    else:
        r = await client.post("/companies/me/apply-preset", headers=headers, params={"vertical": "artwork"})
    assert r.status_code == 200, r.text

    session.expire_all()
    enabled = get_enabled((await session.get(Company, uuid.UUID(company_id))).settings)
    # The artwork preset does not list manufacturing; the company was running it.
    assert running <= enabled, f"switched off: {sorted(running - enabled)}"

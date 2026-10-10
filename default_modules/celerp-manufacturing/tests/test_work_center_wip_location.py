# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A work center's WIP location is master data, like the location it names.

It is stored on the work_centers row, not derived from the ledger, so a ledger rebuild
(which replaces projections only) leaves it as the user set it. A WIP location must be
one of the company's own locations: a malformed id or another company's location is
refused with a message the reader can act on, never stored as "no location"."""
from __future__ import annotations

import uuid

import pytest

from celerp.models.company import Company, Location
from test_helpers import in_language

pytestmark = pytest.mark.asyncio


async def _register(client, name: str) -> dict:
    addr = f"admin-{uuid.uuid4().hex[:8]}@wcloc.test"
    r = await client.post("/auth/register", json={
        "company_name": name, "email": addr, "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _foreign_location(session) -> str:
    """A location of another company, made directly: one install bootstraps one owner."""
    other = Company(name="WCLoc Other", slug=f"wcloc-other-{uuid.uuid4().hex[:8]}", settings={})
    session.add(other)
    await session.flush()
    loc = Location(company_id=other.id, name="Elsewhere", type="warehouse")
    session.add(loc)
    await session.flush()
    return str(loc.id)


async def _location(client, h) -> str:
    r = await client.get("/companies/me/locations", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"][0]["id"]


async def _centers(client, h) -> dict:
    r = await client.get("/manufacturing/work-centers", headers=h)
    assert r.status_code == 200, r.text
    return {c["name"]: c for c in r.json()["items"]}


def _refused(r, key: str) -> None:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail["message_key"] == key, detail
    assert in_language("de", detail) != detail["message"], detail


async def test_wip_location_survives_a_ledger_rebuild(client):
    h = await _register(client, "WCLoc Rebuild")
    loc = await _location(client, h)
    r = await client.post("/manufacturing/work-centers", headers=h, json={"name": "Bench", "wip_location_id": loc})
    assert r.status_code == 200, r.text
    assert (await client.post("/ledger/rebuild", headers=h)).status_code == 200
    assert (await _centers(client, h))["Bench"]["wip_location_id"] == loc


async def test_malformed_wip_location_is_refused_on_create(client):
    h = await _register(client, "WCLoc Malformed")
    r = await client.post("/manufacturing/work-centers", headers=h, json={"name": "Bench", "wip_location_id": "not-a-uuid"})
    _refused(r, "mfg.wip_location_unknown")
    assert "Bench" not in await _centers(client, h)


async def test_another_companys_location_is_refused_on_create_and_patch(client, session):
    foreign = await _foreign_location(session)
    h = await _register(client, "WCLoc Own")
    r = await client.post("/manufacturing/work-centers", headers=h, json={"name": "Bench", "wip_location_id": foreign})
    _refused(r, "mfg.wip_location_unknown")

    own = await _location(client, h)
    r = await client.post("/manufacturing/work-centers", headers=h, json={"name": "Oven", "wip_location_id": own})
    assert r.status_code == 200, r.text
    wc = r.json()["id"]
    r = await client.patch(f"/manufacturing/work-centers/{wc}", headers=h, json={"wip_location_id": foreign})
    _refused(r, "mfg.wip_location_unknown")
    r = await client.patch(f"/manufacturing/work-centers/{wc}", headers=h, json={"wip_location_id": "garbage"})
    _refused(r, "mfg.wip_location_unknown")
    assert (await _centers(client, h))["Oven"]["wip_location_id"] == own


async def test_wip_location_can_be_cleared_and_set_to_an_own_location(client):
    h = await _register(client, "WCLoc Clear")
    own = await _location(client, h)
    r = await client.post("/manufacturing/work-centers", headers=h, json={"name": "Polish"})
    assert r.status_code == 200, r.text
    wc = r.json()["id"]
    assert (await client.patch(f"/manufacturing/work-centers/{wc}", headers=h, json={"wip_location_id": own})).status_code == 200
    assert (await _centers(client, h))["Polish"]["wip_location_id"] == own
    for empty in (None, ""):
        assert (await client.patch(f"/manufacturing/work-centers/{wc}", headers=h, json={"wip_location_id": empty})).status_code == 200
        assert (await _centers(client, h))["Polish"]["wip_location_id"] is None

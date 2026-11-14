# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company has exactly one default location, however a location is written.

Creating, editing, importing and restoring a location all make a location the default through
one helper, which clears the previous default in the same transaction; a partial unique index
refuses a second default from anywhere else. The default's country is read from that one row.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Location
from race_support import _app_client, _http, _ok, _race, _seed

pytestmark = pytest.mark.asyncio


def _factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _defaults(factory, company_id) -> list[str]:
    async with factory() as s:
        return list((await s.execute(select(Location.name).where(
            Location.company_id == company_id, Location.is_default.is_(True)))).scalars())


async def test_create_and_edit_move_the_one_default(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    h = c["h"]["owner"]
    async with _app_client(factory) as (client, _):
        r = await client.post("/companies/me/locations", headers=h, json={
            "name": "Second", "type": "warehouse", "address": None, "is_default": True})
        assert r.status_code == 200, r.text
        assert await _defaults(factory, c["company_id"]) == ["Second"]
        r = await client.post("/companies/me/locations", headers=h, json={
            "name": "Third", "type": "warehouse", "address": None, "is_default": False})
        third = r.json()["id"]
        r = await client.patch(f"/companies/me/locations/{third}", headers=h, json={"is_default": True})
        assert r.status_code == 200 and r.json()["is_default"] is True, r.text
        assert await _defaults(factory, c["company_id"]) == ["Third"]


async def test_import_keeps_the_existing_default_or_sets_the_first(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    h = c["h"]["owner"]
    async with _app_client(factory) as (client, _):
        r = await client.post("/companies/me/locations/import/batch", headers=h,
                              json={"records": [{"name": "North"}, {"name": "South"}]})
        assert r.json()["created"] == 2, r.text
        assert await _defaults(factory, c["company_id"]) == ["Main"]
        async with factory() as s:
            await s.execute(text("UPDATE locations SET is_default = false WHERE company_id = :c"),
                            {"c": c["company_id"]})
            await s.commit()
        r = await client.post("/companies/me/locations/import/batch", headers=h,
                              json={"records": [{"name": "East"}, {"name": "West"}]})
        assert r.json()["created"] == 2, r.text
        assert await _defaults(factory, c["company_id"]) == ["East"]


async def test_two_default_creates_at_once_leave_one_default(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    h = c["h"]["owner"]

    def _create(client, pending, name):
        body = {"name": name, "type": "warehouse", "address": None, "is_default": True}
        return lambda held: _http(pending, held, lambda: client.post("/companies/me/locations", json=body, headers=h))
    async with _app_client(factory) as (client, pending):
        one, two = await _race(committed_engine, _create(client, pending, "Left"), _create(client, pending, "Right"))
    _ok(one)
    _ok(two)
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    assert await _defaults(factory, c["company_id"]) == ["Right"]


async def test_the_database_refuses_a_second_default(committed_engine):
    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with factory() as s:
        s.add(Location(id=uuid.uuid4(), company_id=c["company_id"], name="Rogue", type="warehouse", is_default=True))
        with pytest.raises(IntegrityError, match="uq_location_one_default"):
            await s.flush()


async def test_default_location_country_reads_the_one_default(committed_engine):
    from celerp.routers.companies import _default_location_country
    from celerp.services.locations import make_default_location

    factory = _factory(committed_engine)
    c = await _seed(factory)
    async with factory() as s:
        loc = Location(id=uuid.uuid4(), company_id=c["company_id"], name="Bangkok", type="warehouse",
                       address={"country": "TH"}, is_default=False)
        s.add(loc)
        await make_default_location(s, c["company_id"], loc)
        assert await _default_location_country(s, c["company_id"]) == "TH"
        main = (await s.execute(select(Location).where(
            Location.company_id == c["company_id"], Location.name == "Main"))).scalar_one()
        await make_default_location(s, c["company_id"], main)
        assert await _default_location_country(s, c["company_id"]) == ""
        await s.rollback()

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A location name already used in the company is refused with a message naming it,
whether a location is added or another one is renamed to it, and nothing is written."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from celerp.models.company import Location
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def _add(client, auth, name):
    return await client.post("/companies/me/locations", headers=auth["headers"],
                             json={"name": name, "type": "warehouse"})


async def _count(session, name):
    return (await session.execute(select(func.count()).select_from(Location)
                                  .where(Location.name == name))).scalar_one()


async def test_adding_a_location_with_a_name_in_use_is_refused_by_name(client, session, auth):
    assert (await _add(client, auth, "Back room")).status_code == 200
    r = await _add(client, auth, "Back room")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == t("company.err_location_name_taken", name="Back room")
    assert await _count(session, "Back room") == 1


async def test_renaming_a_location_to_a_name_in_use_is_refused_by_name(client, session, auth):
    assert (await _add(client, auth, "Front")).status_code == 200
    other = (await _add(client, auth, "Side")).json()["id"]
    r = await client.patch(f"/companies/me/locations/{other}", headers=auth["headers"], json={"name": "Front"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == t("company.err_location_name_taken", name="Front")
    assert await _count(session, "Side") == 1


# Every other location writer: each reads more rows (other defaults, the tax regime)
# between taking the name and saving it, and refuses a taken name the same way.
@pytest.mark.parametrize("write", ["add_default", "rename_default", "rename_make_default"])
async def test_every_location_writer_refuses_a_name_in_use_by_name(client, session, auth, write):
    assert (await _add(client, auth, "Front")).status_code == 200
    address = {"country": "TH"}
    if write == "add_default":
        r = await client.post("/companies/me/locations", headers=auth["headers"],
                              json={"name": "Front", "type": "warehouse", "is_default": True, "address": address})
    elif write == "rename_default":
        main = (await client.post("/companies/me/locations", headers=auth["headers"],
                                  json={"name": "Main", "type": "warehouse", "is_default": True,
                                        "address": address})).json()["id"]
        r = await client.patch(f"/companies/me/locations/{main}", headers=auth["headers"], json={"name": "Front"})
    else:
        other = (await _add(client, auth, "Side")).json()["id"]
        r = await client.patch(f"/companies/me/locations/{other}", headers=auth["headers"],
                               json={"name": "Front", "is_default": True})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == t("company.err_location_name_taken", name="Front")
    assert await _count(session, "Front") == 1

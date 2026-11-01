"""Every writer of a company's locations takes the company's lock before the location row:
the order an import's commit and patch_location take them in, so two such writers wait on
each other instead of deadlocking, and the lock checker's census finds no location taken
outside the company lock."""
from __future__ import annotations

import sys


def _ungated_locations() -> set[str]:
    return sys.modules["lock_rank"].RECORDER.ungated_sites({"row:locations"})


async def test_creating_importing_and_deleting_a_location_take_the_company_lock_first(client, auth):
    before = _ungated_locations()
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": "Back room", "type": "warehouse"})
    assert r.status_code == 200, r.text
    created = r.json()["id"]
    r = await client.post("/companies/me/locations/import/batch", headers=auth["headers"],
                          json={"records": [{"name": "Annex", "type": "warehouse"}]})
    assert r.status_code == 200, r.text
    r = await client.delete(f"/companies/me/locations/{created}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert _ungated_locations() - before == set()


async def test_settling_the_default_location_takes_the_company_lock_first(session, auth):
    from celerp.services.provisioning import ensure_default_location

    await session.commit()
    before = _ungated_locations()
    await ensure_default_location(session, auth["company_id"])
    await session.commit()
    assert _ungated_locations() - before == set()

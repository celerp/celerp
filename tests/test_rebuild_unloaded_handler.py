"""A rebuild never replays events whose module handler is not running: it would fold them
into records as raw data and report success. It is refused before anything changes."""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from test_helpers import register_admin


async def _rows(session, company_id) -> dict:
    from celerp.models.projections import Projection
    session.expire_all()
    rows = (await session.execute(select(Projection).where(Projection.company_id == company_id))).scalars().all()
    return {r.entity_id: (json.dumps(r.state, sort_keys=True, default=str), r.version) for r in rows}


@pytest.mark.asyncio
async def test_rebuild_with_a_module_handler_not_running_is_refused_and_changes_nothing(client, session):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    cid = uuid.UUID(str((await client.get("/companies/me", headers=h)).json()["id"]))
    r = await client.post("/crm/contacts", json={"name": "Alice"}, headers=h)
    assert r.status_code == 200, r.text
    contact = r.json()["id"]
    r = await client.post(f"/crm/contacts/{contact}/tags", json={"tags": ["vip"]}, headers=h)
    assert r.status_code == 200, r.text
    before = await _rows(session, cid)
    from celerp.modules import slots
    saved = slots.all_slots()
    slots.unregister_module("celerp-contacts")
    try:
        r = await client.post("/ledger/rebuild", headers=h)
    finally:
        slots._slots.clear()
        slots._slots.update(saved)
    assert r.status_code == 409, r.text
    assert "crm.contact.created" in r.json()["detail"] and "crm.contact.tagged" in r.json()["detail"]
    assert await _rows(session, cid) == before
    r = await client.post("/ledger/rebuild", headers=h)  # handler running again: rebuilds as before
    assert r.status_code == 200, r.text
    assert await _rows(session, cid) == before


@pytest.mark.asyncio
async def test_full_rebuild_with_a_module_handler_not_running_is_refused(client, session):
    """The installation-wide rebuild (upgrade guard, doctor) is refused the same way."""
    from celerp.modules import slots
    from celerp.projections.engine import ProjectionEngine, UnhandledEventsError
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    r = await client.post("/crm/contacts", json={"name": "Bob"}, headers=h)
    assert r.status_code == 200, r.text
    saved = slots.all_slots()
    slots.unregister_module("celerp-contacts")
    try:
        with pytest.raises(UnhandledEventsError) as raised:
            await ProjectionEngine.rebuild(session)
    finally:
        slots._slots.clear()
        slots._slots.update(saved)
    assert "crm.contact.created" in raised.value.event_types


@pytest.mark.asyncio
async def test_ledger_with_retired_bom_events_still_rebuilds(client, session):
    """bom.* events written by celerp-manufacturing v1.0 to v1.1.x have no module
    that writes them anymore; they replay through the default merge, so the
    company rebuild and the installation-wide rebuild both go ahead."""
    from celerp.models.ledger import LedgerEntry
    from celerp.projections.engine import ProjectionEngine
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    cid = uuid.UUID(str((await client.get("/companies/me", headers=h)).json()["id"]))
    r = await client.post("/crm/contacts", json={"name": "Alice"}, headers=h)
    assert r.status_code == 200, r.text
    bom = f"bom:{uuid.uuid4()}"
    for event_type, data in (("bom.created", {"name": "Old recipe", "components": []}),
                             ("bom.updated", {"name": "Older recipe"}),
                             ("bom.deleted", {})):
        session.add(LedgerEntry(
            company_id=cid, entity_id=bom, entity_type="bom", event_type=event_type, data=data,
            source="api", idempotency_key=str(uuid.uuid4()), metadata_={}))
    await session.commit()

    r = await client.post("/ledger/rebuild", headers=h)
    assert r.status_code == 200, r.text
    await ProjectionEngine.rebuild(session)
    assert bom in await _rows(session, cid)

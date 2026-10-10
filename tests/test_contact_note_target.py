# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A contact note is edited or removed only through the contact it belongs to.

The note in the URL must be a live note of that contact in the caller's company. Any
other id (a note of another contact or company, a removed note, another kind of record,
or no record at all) is refused with a translated message and nothing is written.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

_KEY = "contacts.note_not_found"


async def _reg(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "NoteCo", "email": f"cnt-{uuid.uuid4().hex[:8]}@notes.test",
        "name": "Admin", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _other_company(client, h) -> dict:
    r = await client.post("/companies", json={"name": f"Other {uuid.uuid4().hex[:6]}"}, headers=h)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _contact(client, h, name: str) -> str:
    r = await client.post("/crm/contacts", headers=h, json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _note(client, h, cid: str, text: str) -> str:
    r = await client.post(f"/crm/contacts/{cid}/notes", headers=h, json={"note": text})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _notes(client, h, cid: str) -> list[tuple[str, str]]:
    r = await client.get(f"/crm/contacts/{cid}/notes", headers=h)
    assert r.status_code == 200, r.text
    return [(n["id"], n["note"]) for n in r.json()]


async def _events(session, entity_id: str) -> int:
    from celerp.models.ledger import LedgerEntry
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.entity_id == entity_id))


def _refused(r) -> None:
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["message_key"] == _KEY


@pytest.mark.asyncio
async def test_a_note_of_another_contact_is_refused(client, session):
    h = await _reg(client)
    alice, bob = await _contact(client, h, "Alice"), await _contact(client, h, "Bob")
    note = await _note(client, h, alice, "Alice's note")
    before = await _events(session, note)

    _refused(await client.patch(f"/crm/contacts/{bob}/notes/{note}", headers=h, json={"note": "through Bob"}))
    _refused(await client.delete(f"/crm/contacts/{bob}/notes/{note}", headers=h))
    assert await _notes(client, h, alice) == [(note, "Alice's note")]
    assert await _events(session, note) == before


@pytest.mark.asyncio
async def test_an_id_that_is_not_a_note_of_the_contact_is_refused_and_nothing_is_written(client, session):
    h = await _reg(client)
    alice, carol = await _contact(client, h, "Alice"), await _contact(client, h, "Carol")
    ghost = f"note:{uuid.uuid4()}"
    carol_events = await _events(session, carol)

    _refused(await client.patch(f"/crm/contacts/{alice}/notes/{ghost}", headers=h, json={"note": "no such note"}))
    _refused(await client.delete(f"/crm/contacts/{alice}/notes/{ghost}", headers=h))
    _refused(await client.patch(f"/crm/contacts/{alice}/notes/{carol}", headers=h, json={"note": "onto a contact"}))
    _refused(await client.delete(f"/crm/contacts/{alice}/notes/{carol}", headers=h))
    assert await _events(session, ghost) == 0
    assert await _events(session, carol) == carol_events
    r = await client.get(f"/crm/contacts/{carol}", headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Carol"


@pytest.mark.asyncio
async def test_a_note_of_another_company_is_refused(client, session):
    h = await _reg(client)
    alice = await _contact(client, h, "Alice")
    note = await _note(client, h, alice, "Alice's note")
    before = await _events(session, note)

    other = await _other_company(client, h)
    dave = await _contact(client, other, "Dave")
    _refused(await client.patch(f"/crm/contacts/{dave}/notes/{note}", headers=other, json={"note": "from elsewhere"}))
    _refused(await client.delete(f"/crm/contacts/{dave}/notes/{note}", headers=other))
    for r in (await client.patch(f"/crm/contacts/{alice}/notes/{note}", headers=other, json={"note": "from elsewhere"}),
              await client.delete(f"/crm/contacts/{alice}/notes/{note}", headers=other)):
        assert r.status_code == 404, r.text
    assert await _notes(client, h, alice) == [(note, "Alice's note")]
    assert await _events(session, note) == before


# Neighbour guards.

@pytest.mark.asyncio
async def test_a_note_is_edited_and_removed_through_its_own_contact(client):
    h = await _reg(client)
    alice = await _contact(client, h, "Alice")
    note = await _note(client, h, alice, "first call")

    r = await client.patch(f"/crm/contacts/{alice}/notes/{note}", headers=h, json={"note": "second call"})
    assert r.status_code == 200, r.text
    assert await _notes(client, h, alice) == [(note, "second call")]
    r = await client.delete(f"/crm/contacts/{alice}/notes/{note}", headers=h)
    assert r.status_code == 200, r.text
    assert await _notes(client, h, alice) == []


@pytest.mark.asyncio
async def test_a_removed_note_cannot_be_removed_or_edited_again(client, session):
    h = await _reg(client)
    alice = await _contact(client, h, "Alice")
    note = await _note(client, h, alice, "first call")
    assert (await client.delete(f"/crm/contacts/{alice}/notes/{note}", headers=h)).status_code == 200
    before = await _events(session, note)

    _refused(await client.delete(f"/crm/contacts/{alice}/notes/{note}", headers=h))
    _refused(await client.patch(f"/crm/contacts/{alice}/notes/{note}", headers=h, json={"note": "revived"}))
    assert await _notes(client, h, alice) == []
    assert await _events(session, note) == before

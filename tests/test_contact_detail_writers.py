# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Contact notes, people and addresses are written through the event ledger.

Each route emits a crm.contact.* event and the projection shows the result, so the
detail a user adds, edits or removes is what the contact page reads back.
"""
from __future__ import annotations

import uuid

import pytest


async def _reg(client) -> dict:
    addr = f"cdw-{uuid.uuid4().hex[:8]}@writers.test"
    r = await client.post("/auth/register", json={
        "company_name": "DetailCo", "email": addr, "name": "Admin", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _contact(client, h, name="Alice") -> str:
    r = await client.post("/crm/contacts", headers=h, json={
        "name": name, "email": f"{name.lower()}@test.example", "phone": "+1234567890",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _notes(client, h, cid) -> list[dict]:
    r = await client.get(f"/crm/contacts/{cid}/notes", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _state(client, h, cid) -> dict:
    r = await client.get(f"/crm/contacts/{cid}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_a_note_is_added_edited_and_removed(client):
    h = await _reg(client)
    alice = await _contact(client, h, "Alice")
    bob = await _contact(client, h, "Bob")

    r = await client.post(f"/crm/contacts/{alice}/notes", headers=h, json={"note": "first call"})
    assert r.status_code == 200, r.text
    note_id = r.json()["id"]
    notes = await _notes(client, h, alice)
    assert [(n["id"], n["note"]) for n in notes] == [(note_id, "first call")]
    assert await _notes(client, h, bob) == []

    r = await client.patch(f"/crm/contacts/{alice}/notes/{note_id}", headers=h, json={"note": "second call"})
    assert r.status_code == 200, r.text
    assert [n["note"] for n in await _notes(client, h, alice)] == ["second call"]

    r = await client.delete(f"/crm/contacts/{alice}/notes/{note_id}", headers=h)
    assert r.status_code == 200, r.text
    assert await _notes(client, h, alice) == []


@pytest.mark.asyncio
async def test_a_person_is_added_and_removed(client):
    h = await _reg(client)
    cid = await _contact(client, h)

    r = await client.post(f"/crm/contacts/{cid}/people", headers=h, json={"name": "Dana", "role": "Buyer"})
    assert r.status_code == 200, r.text
    person_id = r.json()["person_id"]
    people = (await _state(client, h, cid)).get("people") or []
    assert [(p["person_id"], p["name"], p.get("role")) for p in people] == [(person_id, "Dana", "Buyer")]

    r = await client.delete(f"/crm/contacts/{cid}/people/{person_id}", headers=h)
    assert r.status_code == 200, r.text
    assert ((await _state(client, h, cid)).get("people") or []) == []


@pytest.mark.asyncio
async def test_an_address_is_added_edited_and_removed(client):
    h = await _reg(client)
    cid = await _contact(client, h)

    r = await client.post(f"/crm/contacts/{cid}/addresses", headers=h,
                          json={"address_type": "shipping", "line1": "1 Main St", "city": "Bangkok"})
    assert r.status_code == 200, r.text
    address_id = r.json()["address_id"]
    addresses = (await _state(client, h, cid)).get("addresses") or []
    assert [(a["address_id"], a["line1"], a["city"]) for a in addresses] == [(address_id, "1 Main St", "Bangkok")]

    r = await client.patch(f"/crm/contacts/{cid}/addresses/{address_id}", headers=h, json={"city": "Chiang Mai"})
    assert r.status_code == 200, r.text
    addresses = (await _state(client, h, cid)).get("addresses") or []
    assert [(a["line1"], a["city"]) for a in addresses] == [("1 Main St", "Chiang Mai")]

    r = await client.delete(f"/crm/contacts/{cid}/addresses/{address_id}", headers=h)
    assert r.status_code == 200, r.text
    assert ((await _state(client, h, cid)).get("addresses") or []) == []

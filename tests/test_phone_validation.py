# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A phone saved on a contact, a contact person or the company has at least 4 digits and at
most 40 characters, not counting surrounding spaces, or is empty.

Phones already stored are never rechecked: a contact keeping an old phone can still be
edited, and imports are unchanged.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.asyncio

_REFUSAL = "Enter a phone number with at least 4 digits"


async def _reg(client) -> dict:
    r = await client.post("/auth/register", json={"company_name": "PhoneCo", "email": f"ph-{uuid.uuid4().hex[:8]}@t.test",
                                                  "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _contact(client, h, **data) -> str:
    r = await client.post("/crm/contacts", headers=h, json={"name": "Phone Contact", **data})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _patch_phone(client, h, cid: str, new: str):
    return await client.patch(f"/crm/contacts/{cid}", headers=h,
                              json={"fields_changed": {"phone": {"old": None, "new": new}}})


async def test_a_contact_cannot_be_created_with_a_phone_without_digits(client):
    h = await _reg(client)
    r = await client.post("/crm/contacts", headers=h, json={"name": "Bad Phone", "phone": "abc"})
    assert r.status_code == 422 and _REFUSAL in r.text, r.text


async def test_a_contact_phone_cannot_be_changed_to_a_partial_number(client):
    h = await _reg(client)
    cid = await _contact(client, h, phone="+66812345678")
    r = await _patch_phone(client, h, cid, "12")
    assert r.status_code == 422 and _REFUSAL in r.text, r.text
    assert (await client.get(f"/crm/contacts/{cid}", headers=h)).json()["phone"] == "+66812345678"


async def test_a_company_phone_cannot_be_set_to_a_partial_number(client):
    h = await _reg(client)
    r = await client.patch("/companies/me", headers=h, json={"settings": {"phone": "12"}})
    assert r.status_code == 422 and _REFUSAL in r.text, r.text


async def test_phones_with_spaces_extensions_or_nothing_are_accepted(client):
    h = await _reg(client)
    cid = await _contact(client, h, phone="081 234 5678 ext 12")
    assert (await _patch_phone(client, h, cid, "")).status_code == 200
    assert (await client.patch("/companies/me", headers=h, json={"settings": {"phone": "+1 (555) 010-0000"}})).status_code == 200
    assert (await client.patch("/companies/me", headers=h, json={"settings": {"phone": ""}})).status_code == 200


async def test_a_contact_keeping_an_old_phone_can_still_be_edited(client):
    h = await _reg(client)
    r = await client.post("/crm/contacts/import/batch", headers=h, json={"records": [{
        "entity_id": f"contact:{uuid.uuid4()}", "event_type": "crm.contact.created", "source": "csv_import",
        "data": {"name": "Old Phone", "phone": "n/a"}, "idempotency_key": f"k-{uuid.uuid4().hex}"}]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    cid = (await client.get("/crm/contacts", headers=h)).json()["items"][0]["id"]
    r = await client.patch(f"/crm/contacts/{cid}", headers=h,
                           json={"fields_changed": {"name": {"old": "Old Phone", "new": "Renamed"}}})
    assert r.status_code == 200, r.text


async def test_a_phone_over_40_characters_is_refused(client):
    h = await _reg(client)
    long_phone = "081 234 5678" + "9" * 29
    r = await client.post("/crm/contacts", headers=h, json={"name": "Long Phone", "phone": long_phone})
    assert r.status_code == 422 and "at most 40 characters" in r.text, r.text
    r = await client.patch("/companies/me", headers=h, json={"settings": {"phone": long_phone}})
    assert r.status_code == 422 and "at most 40 characters" in r.text, r.text
    assert (await client.post("/crm/contacts", headers=h, json={"name": "Max Phone", "phone": long_phone[:40]})).status_code == 200


async def test_a_padded_phone_is_measured_without_its_surrounding_spaces(client):
    h = await _reg(client)
    padded = "+66812345678" + " " * 30
    cid = await _contact(client, h, phone=padded)
    assert (await _patch_phone(client, h, cid, padded)).status_code == 200
    assert (await client.patch("/companies/me", headers=h, json={"settings": {"phone": padded}})).status_code == 200


async def test_a_contact_person_phone_is_checked_when_given_or_changed(client):
    h = await _reg(client)
    cid = await _contact(client, h)
    people = f"/crm/contacts/{cid}/people"
    for bad, refusal in (("12", _REFUSAL), ("081 234 5678" + "9" * 29, "at most 40 characters")):
        r = await client.post(people, headers=h, json={"name": "Person", "phone": bad})
        assert r.status_code == 422 and refusal in r.text, r.text
    r = await client.post(people, headers=h, json={"name": "Person", "phone": "081 234 5678"})
    assert r.status_code == 200, r.text
    pid = r.json()["person_id"]
    r = await client.patch(f"{people}/{pid}", headers=h, json={"phone": "12"})
    assert r.status_code == 422 and _REFUSAL in r.text, r.text
    assert (await client.patch(f"{people}/{pid}", headers=h, json={"phone": ""})).status_code == 200
    assert (await client.post(people, headers=h, json={"name": "No Phone"})).status_code == 200


async def test_a_contact_person_keeping_an_old_phone_can_still_be_edited(client, session):
    from celerp.models.projections import Projection
    h = await _reg(client)
    cid = await _contact(client, h)
    r = await client.post(f"/crm/contacts/{cid}/people", headers=h, json={"name": "Old", "phone": "081 234 5678"})
    pid = r.json()["person_id"]
    row = (await session.execute(select(Projection).where(Projection.entity_id == cid))).scalar_one()
    row.state = {**row.state, "people": [{**p, "phone": "12"} for p in row.state["people"]]}
    await session.commit()
    r = await client.patch(f"/crm/contacts/{cid}/people/{pid}", headers=h,
                           json={"name": "Renamed", "phone": "12", "is_primary": False})
    assert r.status_code == 200, r.text

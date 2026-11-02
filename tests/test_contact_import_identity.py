# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Importing a contact that already exists updates it instead of adding a second one.

A row is the existing contact with the same email, else phone, else name (any case),
whatever key it is sent under. A row matching two contacts is refused and writes
nothing; a row with nothing new writes nothing.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def _reg(client) -> dict:
    r = await client.post("/auth/register", json={"company_name": "ImportCo", "email": f"ci-{uuid.uuid4().hex[:8]}@t.test",
                                                  "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _row(data: dict, key: str | None = None, entity_id: str | None = None) -> dict:
    return {"entity_id": entity_id or f"contact:{uuid.uuid4()}", "event_type": "crm.contact.created",
            "data": data, "source": "csv_import", "idempotency_key": key or f"k-{uuid.uuid4().hex}"}


async def _batch(client, h, *rows: dict) -> dict:
    r = await client.post("/crm/contacts/import/batch", headers=h, json={"records": list(rows)})
    assert r.status_code == 200, r.text
    return r.json()


async def _contacts(session, h_client, h) -> list[dict]:
    r = await h_client.get("/crm/contacts", headers=h, params={"limit": 500})
    assert r.status_code == 200, r.text
    return [c for c in r.json()["items"] if not c.get("is_self")]


async def _contact_events(session) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.entity_type == "contact"))).scalar_one()


async def test_reimport_under_a_new_key_updates_the_contact_with_that_email(client):
    h = await _reg(client)
    await _batch(client, h, _row({"name": "Acme", "email": "Buyer@Acme.test", "phone": "+100"}))
    result = await _batch(client, h, _row({"name": "Acme Trading", "email": "buyer@acme.test"}))
    assert (result["created"], result["updated"], result["skipped"]) == (0, 1, 0), result
    [contact] = await _contacts(None, client, h)
    assert contact["name"] == "Acme Trading"
    assert contact["phone"] == "+100"  # a blank cell leaves the field as it was


@pytest.mark.parametrize("first, again", [
    ({"name": "Phone Only", "phone": "+66 81 000 0000"}, {"name": "Phone Only Ltd", "phone": "+66 81 000 0000"}),
    ({"name": "Name Only"}, {"name": "NAME ONLY", "tax_id": "TX-1"}),
])
async def test_without_an_email_the_phone_then_the_name_is_the_contact(client, first, again):
    h = await _reg(client)
    await _batch(client, h, _row(first))
    assert (await _batch(client, h, _row(again)))["updated"] == 1
    [contact] = await _contacts(None, client, h)
    assert all(contact[k] == v for k, v in again.items())


async def test_exact_replay_is_skipped_and_writes_nothing(client, session):
    h = await _reg(client)
    row = _row({"name": "Replay", "email": "replay@t.test"})
    assert (await _batch(client, h, row))["created"] == 1
    before = await _contact_events(session)
    result = await _batch(client, h, row)
    assert (result["created"], result["updated"], result["skipped"]) == (0, 0, 1), result
    assert await _contact_events(session) == before


async def test_same_key_with_changed_content_updates(client):
    h = await _reg(client)
    row = _row({"name": "Edited", "email": "edited@t.test"})
    await _batch(client, h, row)
    result = await _batch(client, h, {**row, "data": {"name": "Edited", "email": "edited@t.test", "website": "e.test"}})
    assert result["updated"] == 1, result
    [contact] = await _contacts(None, client, h)
    assert contact["website"] == "e.test"


async def test_a_row_matching_two_contacts_is_refused_and_writes_nothing(client, session):
    h = await _reg(client)
    for name in ("Twin A", "Twin B"):
        r = await client.post("/crm/contacts", headers=h, json={"name": name, "email": "twin@t.test"})
        assert r.status_code == 200, r.text
    before = await _contact_events(session)
    result = await _batch(client, h, _row({"name": "Twin", "email": "TWIN@t.test", "phone": "+1"}),
                          _row({"name": "Other", "email": "other@t.test"}))
    assert (result["created"], result["updated"], result["skipped"]) == (1, 0, 1), result
    [error] = result["errors"]
    assert error.startswith("Twin: matches 2 existing contacts") and "unique email, phone or name" in error
    assert await _contact_events(session) == before + 1
    assert {c["name"] for c in await _contacts(None, client, h)} == {"Twin A", "Twin B", "Other"}


async def test_two_rows_for_one_new_contact_in_one_file_make_one_contact(client):
    h = await _reg(client)
    result = await _batch(client, h, _row({"name": "Dup", "email": "dup@t.test"}),
                          _row({"name": "Dup", "email": "dup@t.test", "phone": "+2"}))
    assert (result["created"], result["updated"]) == (1, 1), result
    [contact] = await _contacts(None, client, h)
    assert contact["phone"] == "+2"


async def test_a_blank_contact_type_keeps_a_vendor_a_vendor_and_a_new_contact_is_a_customer(client):
    h = await _reg(client)
    await _batch(client, h, _row({"name": "Supplier", "email": "s@t.test", "contact_type": "vendor"}),
                 _row({"name": "Fresh", "email": "f@t.test"}))
    await _batch(client, h, _row({"name": "Supplier Co", "email": "s@t.test"}))
    types = {c["name"]: c["contact_type"] for c in await _contacts(None, client, h)}
    assert types == {"Supplier Co": "vendor", "Fresh": "customer"}


async def test_a_customer_imported_again_as_a_vendor_becomes_both(client):
    h = await _reg(client)
    await _batch(client, h, _row({"name": "Two Way", "email": "tw@t.test", "contact_type": "customer"}))
    assert (await _batch(client, h, _row({"name": "Two Way", "email": "tw@t.test", "contact_type": "vendor"})))["updated"] == 1
    [contact] = await _contacts(None, client, h)
    assert contact["contact_type"] == "both"
    # Importing either role again changes nothing.
    assert (await _batch(client, h, _row({"name": "Two Way", "email": "tw@t.test", "contact_type": "customer"})))["skipped"] == 1


async def test_an_unknown_currency_refuses_the_row(client, session):
    h = await _reg(client)
    result = await _batch(client, h, _row({"name": "Bad Money", "currency": "XXQ"}))
    assert result["created"] == 0 and result["errors"] == [f"Bad Money: {t('error.invalid_currency', value='XXQ')}"], result
    assert await _contacts(None, client, h) == []


async def test_single_record_import_of_an_existing_contact_updates_it(client):
    h = await _reg(client)
    first = _row({"name": "Single", "email": "single@t.test"})
    r = await client.post("/crm/contacts/import", headers=h, json=first)
    assert r.status_code == 200 and r.json()["status"] == "created", r.text
    again = _row({"name": "Single Ltd", "email": "single@t.test"}, entity_id=first["entity_id"])
    r = await client.post("/crm/contacts/import", headers=h, json=again)
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["status"]) == (first["entity_id"], "updated")
    [contact] = await _contacts(None, client, h)
    assert contact["name"] == "Single Ltd"


async def test_the_company_own_contact_is_not_matched(client):
    h = await _reg(client)
    me = next(c for c in (await client.get("/crm/contacts", headers=h)).json()["items"] if c.get("is_self"))
    assert me["email"]
    result = await _batch(client, h, _row({"name": "Imported Me", "email": me["email"]}))
    assert result["created"] == 1, result
    assert (await client.get(f"/crm/contacts/{me['id']}", headers=h)).json()["name"] == me["name"]


async def test_a_merged_away_contact_is_not_matched(client):
    h = await _reg(client)
    ids = []
    for name, email in (("Keep", "keep@t.test"), ("Gone", "gone@t.test")):
        r = await client.post("/crm/contacts", headers=h, json={"name": name, "email": email})
        ids.append(r.json()["id"])
    r = await client.post("/crm/contacts/merge", headers=h, json={"target_contact_id": ids[0], "source_contact_ids": [ids[1]]})
    assert r.status_code == 200, r.text
    result = await _batch(client, h, _row({"name": "Gone Again", "email": "gone@t.test"}))
    assert result["created"] == 1, result


async def _me(client, h) -> dict:
    return next(c for c in (await client.get("/crm/contacts", headers=h)).json()["items"] if c.get("is_self"))


async def test_no_import_path_writes_the_company_own_contact(client, session):
    """The company's identity is changed only in Company details: an import row naming the
    company's contact by its id, sent again under the key that created it, or claiming to be
    the company's own is refused, through the batch and the single record import alike, and
    nothing is written."""
    h = await _reg(client)
    me = await _me(client, h)
    created_key = (await session.execute(select(LedgerEntry.idempotency_key).where(
        LedgerEntry.entity_id == me["id"], LedgerEntry.event_type == "crm.contact.created"))).scalar_one()
    before = await _contact_events(session)
    rows = [_row({"name": "Taken Over"}, entity_id=me["id"]),
            _row({"name": "Taken Over"}, key=created_key),
            _row({"name": "Second Me", "is_self": True})]
    result = await _batch(client, h, *rows)
    assert result["created"] == result["updated"] == 0, result
    assert all(t("contacts.err_import_own_contact") in e for e in result["errors"]), result
    assert len(result["errors"]) == 3, result
    for row in rows:
        single = await client.post("/crm/contacts/import", headers=h, json=row)
        assert single.status_code == 409, single.text
    assert await _contact_events(session) == before
    assert (await client.get(f"/crm/contacts/{me['id']}", headers=h)).json()["name"] == me["name"]
    assert [c["id"] for c in (await client.get("/crm/contacts", headers=h, params={"limit": 500})).json()["items"]
            if c.get("is_self")] == [me["id"]]


async def test_the_merge_service_never_merges_the_company_own_contact_away(client, session):
    """The merge service itself, whichever endpoint calls it, refuses the company's own contact
    as a source on the locked record."""
    from fastapi import HTTPException

    from celerp.models.company import Company
    from celerp_contacts.routes import merge_contacts_service

    h = await _reg(client)
    me = await _me(client, h)
    other = (await client.post("/crm/contacts", headers=h, json={"name": "Other", "email": "o@t.test"})).json()["id"]
    company_id = (await session.execute(select(Projection.company_id).where(
        Projection.entity_id == me["id"]))).scalar_one()
    owner = (await session.execute(select(LedgerEntry.actor_id).where(
        LedgerEntry.entity_id == me["id"]))).scalars().first()
    before = await _contact_events(session)
    with pytest.raises(HTTPException) as exc:
        await merge_contacts_service(session, company_id, owner, target_contact_id=other, source_contact_ids=[me["id"]])
    assert exc.value.status_code == 422
    await session.rollback()
    assert await _contact_events(session) == before
    assert await session.get(Company, company_id) is not None

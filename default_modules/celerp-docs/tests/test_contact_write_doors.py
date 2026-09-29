# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every way a Document or List gets its contact applies the same rules as selecting it."""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from test_contact_reference_lifecycle import _NoCommit
from test_contact_selection import _contact, _doc, _get, _item, _line, _list, _owner, _seed, _select


def _fields(**values) -> dict:
    return {k: {"old": None, "new": v} for k, v in values.items()}


async def _patch(client, h, resource, entity_id, version=None, key=None, **values):
    body: dict = {"fields_changed": _fields(**values)}
    if version is not None:
        body["expected_version"] = version
    if key is not None:
        body["idempotency_key"] = key
    return await client.patch(f"/{resource}/{entity_id}", headers=h, json=body)


# ── Older customer field names ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_customer_id_patch_is_a_full_contact_selection(client, resource):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, name="Old Field Buyer", currency="EUR", price_list="Wholesale")
    entity_id = await (_doc if resource == "docs" else _list)(client, h, item_id)
    version = (await _get(client, h, resource, entity_id))["version"]
    r = await _patch(client, h, resource, entity_id, version, customer_id=cid, customer_name="Spoofed")
    assert r.status_code == 200, r.text
    record = await _get(client, h, resource, entity_id)
    assert record["contact_id"] == cid
    assert record["contact_name"] == "Old Field Buyer"
    assert record["currency"] == "EUR"
    assert record["price_list"] == "Wholesale"
    assert record["line_items"][0]["unit_price"] == 80
    assert "customer_id" not in record and "customer_name" not in record


@pytest.mark.asyncio
async def test_customer_id_patch_refuses_a_vendor(client):
    h = await _owner(client)
    vid = await _contact(client, h, name="Only Supplier", contact_type="vendor")
    list_id = await _list(client, h, None)
    before = await _get(client, h, "lists", list_id)
    r = await _patch(client, h, "lists", list_id, before["version"], customer_id=vid)
    assert r.status_code == 422, r.text
    assert "Only Supplier is not a customer" in r.text
    after = await _get(client, h, "lists", list_id)
    assert after["version"] == before["version"] and not after.get("contact_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_customer_id_create_refuses_a_deleted_contact(client, resource):
    h = await _owner(client)
    cid = await _contact(client, h, name="Gone Co")
    r = await client.post("/crm/contacts/bulk/delete", headers=h, json={"contact_ids": [cid]})
    assert r.status_code == 200, r.text
    body = {"doc_type": "invoice"} if resource == "docs" else {"list_type": "quotation"}
    r = await client.post(f"/{resource}", headers=h, json={**body, "customer_id": cid})
    assert r.status_code == 422, r.text
    assert "deleted" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_customer_id_create_becomes_the_contact(client, resource):
    h = await _owner(client)
    cid = await _contact(client, h, name="Named Buyer")
    body = {"doc_type": "invoice"} if resource == "docs" else {"list_type": "quotation"}
    r = await client.post(f"/{resource}", headers=h, json={**body, "customer_id": cid})
    assert r.status_code == 200, r.text
    record = await _get(client, h, resource, r.json()["id"])
    assert (record["contact_id"], record["contact_name"]) == (cid, "Named Buyer")
    assert "customer_id" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_conflicting_old_and_new_contact_fields_are_refused(client, resource):
    h = await _owner(client)
    first = await _contact(client, h, name="First")
    second = await _contact(client, h, name="Second")
    body = {"doc_type": "invoice"} if resource == "docs" else {"list_type": "quotation"}
    r = await client.post(f"/{resource}", headers=h, json={**body, "contact_id": first, "customer_id": second})
    assert r.status_code == 422, r.text
    assert "customer_id" in r.text and "contact_id" in r.text

    entity_id = await (_doc if resource == "docs" else _list)(client, h, None)
    before = await _get(client, h, resource, entity_id)
    r = await _patch(client, h, resource, entity_id, before["version"], contact_id=first, customer_id=second)
    assert r.status_code == 422, r.text
    assert (await _get(client, h, resource, entity_id))["version"] == before["version"]


# ── Creating with a contact ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("resource", "kind_field", "kind", "contact_type", "role"), [
    ("lists", "list_type", "quotation", "vendor", "customer"),
    ("docs", "doc_type", "invoice", "vendor", "customer"),
    ("docs", "doc_type", "purchase_order", "customer", "vendor"),
    ("docs", "doc_type", "subscription_po", "customer", "vendor"),
])
async def test_create_refuses_the_wrong_contact_type(client, resource, kind_field, kind, contact_type, role):
    h = await _owner(client)
    cid = await _contact(client, h, name="Wrong Side", contact_type=contact_type)
    r = await client.post(f"/{resource}", headers=h, json={kind_field: kind, "contact_id": cid})
    assert r.status_code == 422, r.text
    assert f"Wrong Side is not a {role}" in r.text


@pytest.mark.asyncio
async def test_invoice_created_with_a_contact_takes_the_contact_and_its_terms(client):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, name="Euro Buyer", email="buyer@example.test",
                         currency="EUR", price_list="Wholesale", payment_terms="Net 30")
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "issue_date": "2026-01-10", "contact_id": cid,
        "contact_name": "Spoofed", "contact_email": "spoof@example.test",
        "line_items": [_line(item_id)],
    })
    assert r.status_code == 200, r.text
    doc = await _get(client, h, "docs", r.json()["id"])
    assert doc["contact_name"] == "Euro Buyer"
    assert doc["contact_email"] == "buyer@example.test"
    assert doc["payment_terms"] == "Net 30"
    assert doc["due_date"] == "2026-02-09"
    assert doc["currency"] == "EUR"
    assert doc["price_list"] == "Wholesale"
    assert doc["line_items"][0]["unit_price"] == 80
    assert doc["total"] == 160
    assert doc["amount_outstanding"] == 160


@pytest.mark.asyncio
async def test_list_created_with_a_contact_takes_the_contact_and_its_prices(client):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, name="List Buyer", currency="EUR", price_list="Wholesale")
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation", "contact_id": cid, "contact_name": "Spoofed",
        "line_items": [_line(item_id)],
    })
    assert r.status_code == 200, r.text
    lst = await _get(client, h, "lists", r.json()["id"])
    assert lst["contact_name"] == "List Buyer"
    assert lst["currency"] == "EUR"
    assert lst["price_list"] == "Wholesale"
    assert lst["line_items"][0]["unit_price"] == 80
    assert lst["total"] == 160


@pytest.mark.asyncio
async def test_prices_chosen_on_create_are_kept(client):
    """A credit note copies its invoice: its own price list and prices stand."""
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, name="Chosen", currency="EUR", price_list="Wholesale")
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": cid, "currency": "USD", "price_list": "Retail",
        "line_items": [_line(item_id)],
    })
    assert r.status_code == 200, r.text
    doc = await _get(client, h, "docs", r.json()["id"])
    assert doc["contact_name"] == "Chosen"
    assert (doc["currency"], doc["price_list"]) == ("USD", "Retail")
    assert doc["line_items"][0]["unit_price"] == 100


# ── Repeated sends ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
@pytest.mark.parametrize("extra", ["scalar", "protected", "lines"])
async def test_a_different_request_at_the_same_version_is_never_reported_as_saved(client, resource, extra):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, currency="EUR", price_list="Wholesale")
    entity_id = await (_doc if resource == "docs" else _list)(client, h, item_id)
    version = (await _get(client, h, resource, entity_id))["version"]
    assert (await _select(client, h, resource, entity_id, cid, version=version)).status_code == 200
    after = await _get(client, h, resource, entity_id)

    more = {
        "scalar": {"notes": "changed"},
        "protected": {"status": "paid"},
        "lines": {"line_items": [_line(item_id)]},
    }[extra]
    r = await _select(client, h, resource, entity_id, cid, version=version, **more)
    assert r.status_code in (409, 422), r.text
    final = await _get(client, h, resource, entity_id)
    assert final["version"] == after["version"]
    assert final.get("notes") != "changed" and final["status"] == "draft"


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_a_reused_request_key_with_a_different_body_is_refused(client, resource):
    h = await _owner(client)
    entity_id = await (_doc if resource == "docs" else _list)(client, h, None)
    key = f"edit-{uuid.uuid4().hex}"
    assert (await _patch(client, h, resource, entity_id, key=key, notes="first")).status_code == 200
    r = await _patch(client, h, resource, entity_id, key=key, notes="second")
    assert r.status_code == 409, r.text
    assert (await _get(client, h, resource, entity_id))["notes"] == "first"


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_an_identical_versioned_edit_sent_twice_is_saved_once(client, resource):
    h = await _owner(client)
    entity_id = await (_doc if resource == "docs" else _list)(client, h, None)
    version = (await _get(client, h, resource, entity_id))["version"]
    first = await _patch(client, h, resource, entity_id, version, notes="once")
    assert first.status_code == 200, first.text
    again = await _patch(client, h, resource, entity_id, version, notes="once")
    assert again.status_code == 200, again.text
    assert again.json()["version"] == first.json()["version"]
    assert (await _get(client, h, resource, entity_id))["version"] == first.json()["version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_a_reused_create_key_with_a_different_body_is_refused(client, resource):
    h = await _owner(client)
    key = f"create-{uuid.uuid4().hex}"
    body = {"doc_type": "invoice"} if resource == "docs" else {"list_type": "quotation"}
    first = await client.post(f"/{resource}", headers=h, json={**body, "notes": "first", "idempotency_key": key})
    assert first.status_code == 200, first.text
    again = await client.post(f"/{resource}", headers=h, json={**body, "notes": "first", "idempotency_key": key})
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"], again.text
    other = await client.post(f"/{resource}", headers=h, json={**body, "notes": "second", "idempotency_key": key})
    assert other.status_code == 409, other.text


# ── List import ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_import_update_selects_the_contact(client):
    h = await _owner(client)
    cid = await _contact(client, h, name="Imported Buyer", currency="EUR")
    record = {"entity_id": "list:IMP-1", "event_type": "list.created", "idempotency_key": "imp-1", "source": "csv",
              "data": {"list_type": "quotation", "status": "draft", "currency": "USD", "line_items": []}}
    r = await client.post("/lists/import/batch", headers=h, json={"records": [record]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    record["data"] = {**record["data"], "customer_id": cid}
    r = await client.post("/lists/import/batch", headers=h, json={"records": [record], "upsert": True})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1, r.json()
    lst = await _get(client, h, "lists", "list:IMP-1")
    assert (lst["contact_id"], lst["contact_name"], lst["currency"]) == (cid, "Imported Buyer", "EUR")


# ── Contact retired while the record is written ──────────────────────────────


async def _company(factory):
    from celerp.models.company import Company, User
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="DoorCo", slug=f"door-{company_id.hex[:8]}"))
        s.add(User(id=user_id, email=f"door-{user_id.hex[:8]}@example.test", name="Editor"))
        await s.commit()
    return company_id, user_id


async def _cleanup(factory, company_id, user_id):
    from sqlalchemy import delete

    from celerp.models.company import Company, User
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    async with factory() as s:
        await s.execute(delete(Projection).where(Projection.company_id == company_id))
        await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _references(factory, company_id, contact_id) -> list[str]:
    from sqlalchemy import select

    from celerp.models.projections import Projection
    async with factory() as s:
        rows = (await s.execute(select(Projection).where(
            Projection.company_id == company_id, Projection.entity_type.in_(("doc", "list")),
        ))).scalars().all()
    return sorted(r.entity_id for r in rows if (r.state or {}).get("contact_id") == contact_id)


async def _statuses(call) -> int:
    from fastapi import HTTPException
    try:
        await call()
        return 200
    except HTTPException as exc:
        return exc.status_code


async def _retire(session, company_id, user_id, retire):
    from celerp_contacts import routes as contact_routes
    if retire == "merge":
        await contact_routes.merge_contacts_service(
            session, company_id, user_id, target_contact_id="contact:B", source_contact_ids=["contact:A"])
    else:
        await contact_routes.bulk_delete_contacts(
            contact_routes.BulkContactDeleteBody(contact_ids=["contact:A"]),
            company_id=company_id, user=types.SimpleNamespace(id=user_id), _=None, session=_NoCommit(session))


async def _race(factory, first, second):
    """Hold first()'s transaction open, start second(), then commit first.

    Returns whether second waited for first, and both statuses."""
    held = factory()
    first_status = await _statuses(lambda: first(_NoCommit(held)))

    async def run() -> int:
        async with factory() as s:
            status = await _statuses(lambda: second(s))
            await (s.commit() if status == 200 else s.rollback())
            return status

    task = asyncio.create_task(run())
    await asyncio.sleep(0.5)
    waited = not task.done()
    if first_status == 200:
        await held.commit()
    await held.close()
    return waited, first_status, await task


_CONTACTS = [
    ("contact:A", "contact", "crm.contact.created", {"name": "Alpha", "contact_type": "customer"}),
    ("contact:B", "contact", "crm.contact.created", {"name": "Beta", "contact_type": "customer"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("retire", ["merge", "delete"])
@pytest.mark.parametrize("door", ["patch", "create"])
async def test_customer_id_waits_for_a_merge_or_delete(_db_engine, retire, door):
    from celerp_docs import routes
    from celerp_docs.routes import ListCreatePayload, ListPatch

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _company(factory)
    await _seed(factory, company_id, [*_CONTACTS, (
        "list:OLD-1", "list", "list.created",
        {"list_type": "quotation", "status": "draft", "currency": "USD", "ref_id": "OLD-1", "line_items": []},
    )])
    user = types.SimpleNamespace(id=user_id)
    kw = {"company_id": company_id, "_": None, "role": "owner", "settings": {}, "user": user}
    try:
        async def write(s):
            if door == "patch":
                await routes.patch_list("list:OLD-1", ListPatch(fields_changed=_fields(customer_id="contact:A")),
                                        session=s, **kw)
            else:
                await routes.create_list(ListCreatePayload(list_type="quotation", customer_id="contact:A"),
                                         session=s, **kw)

        waited, retired, status = await _race(
            factory, lambda s: _retire(s, company_id, user_id, retire), write)
        assert retired == 200
        assert waited, "the write must wait for the contact"
        assert status == 422
        assert await _references(factory, company_id, "contact:A") == []
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_first", [True, False], ids=["merge-first", "copy-first"])
@pytest.mark.parametrize("door", ["duplicate_list", "convert_list", "convert_doc", "shipment"])
async def test_a_copy_racing_a_merge_never_names_the_merged_contact(_db_engine, door, merge_first):
    from celerp_docs import routes

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _company(factory)
    line = {"description": "Service", "quantity": 1, "unit_price": 5, "line_total": 5}
    common = {"currency": "USD", "contact_id": "contact:A", "contact_name": "Alpha", "line_items": [line]}
    source = {
        "duplicate_list": ("list:SRC-1", "list", "list.created",
                           {**common, "list_type": "quotation", "status": "draft", "ref_id": "SRC-1"}),
        "convert_list": ("list:SRC-1", "list", "list.created",
                         {**common, "list_type": "quotation", "status": "finalized", "ref_id": "SRC-1"}),
        "convert_doc": ("doc:SRC-1", "doc", "doc.created",
                        {**common, "doc_type": "quotation", "status": "draft", "ref_id": "SRC-1"}),
        "shipment": ("doc:SRC-1", "doc", "doc.created",
                     {**common, "doc_type": "invoice", "status": "final", "ref_id": "SRC-1"}),
    }[door]
    await _seed(factory, company_id, [*_CONTACTS, source])
    user = types.SimpleNamespace(id=user_id)
    try:
        async def copy(s):
            kw = {"company_id": company_id, "_": None, "user": user, "session": s}
            if door == "duplicate_list":
                await routes.duplicate_list("list:SRC-1", **kw)
            elif door == "convert_list":
                await routes.convert_list("list:SRC-1", routes.ListConvertBody(target_type="invoice"), **kw)
            elif door == "convert_doc":
                await routes.convert_doc("doc:SRC-1", **kw)
            else:
                await routes.create_shipment_from_docs(routes.ShipmentFromDocsBody(doc_ids=["doc:SRC-1"]), **kw)

        async def merge(s):
            await _retire(s, company_id, user_id, "merge")

        first, second = (merge, copy) if merge_first else (copy, merge)
        waited, first_status, second_status = await _race(factory, first, second)
        assert first_status == 200
        assert waited, "the second writer must wait for the first"
        assert second_status in (200, 409)
        assert await _references(factory, company_id, "contact:A") == []
    finally:
        await _cleanup(factory, company_id, user_id)

def _patcher(resource, entity_id, company_id, user_id, **payload):
    from celerp_docs import routes

    async def call(s):
        fn = routes.patch_doc if resource == "docs" else routes.patch_list
        await fn(entity_id, routes.DocPatch(**payload), company_id=company_id, _=None, role="owner",
                 settings={}, user=types.SimpleNamespace(id=user_id), session=s)
    return call


_DRAFTS = {
    "docs": ("doc:D-1", "doc", "doc.created",
             {"doc_type": "invoice", "status": "draft", "currency": "USD", "ref_id": "D-1", "line_items": []}),
    "lists": ("list:L-1", "list", "list.created",
              {"list_type": "quotation", "status": "draft", "currency": "USD", "ref_id": "L-1", "line_items": []}),
}


async def _version(factory, company_id, entity_id) -> int:
    from celerp.models.projections import Projection
    async with factory() as s:
        return (await s.get(Projection, {"company_id": company_id, "entity_id": entity_id})).version


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_a_different_body_sent_at_the_same_moment_with_one_key_is_refused(_db_engine, resource):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _company(factory)
    entity_id = _DRAFTS[resource][0]
    await _seed(factory, company_id, [_DRAFTS[resource]])
    key = f"edit-{uuid.uuid4().hex}"
    try:
        waited, first, second = await _race(
            factory,
            _patcher(resource, entity_id, company_id, user_id, fields_changed=_fields(notes="first"), idempotency_key=key),
            _patcher(resource, entity_id, company_id, user_id, fields_changed=_fields(notes="second"), idempotency_key=key),
        )
        assert (waited, first, second) == (True, 200, 409)
    finally:
        await _cleanup(factory, company_id, user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
@pytest.mark.parametrize("keyed", [True, False], ids=["with-key", "without-key"])
async def test_an_identical_versioned_edit_sent_at_the_same_moment_is_saved_once(_db_engine, resource, keyed):
    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _company(factory)
    entity_id = _DRAFTS[resource][0]
    await _seed(factory, company_id, [_DRAFTS[resource]])
    payload = {"fields_changed": _fields(notes="once"),
               "expected_version": await _version(factory, company_id, entity_id)}
    if keyed:
        payload["idempotency_key"] = f"edit-{uuid.uuid4().hex}"
    try:
        waited, first, second = await _race(
            factory,
            _patcher(resource, entity_id, company_id, user_id, **payload),
            _patcher(resource, entity_id, company_id, user_id, **payload),
        )
        assert (waited, first, second) == (True, 200, 200)
    finally:
        await _cleanup(factory, company_id, user_id)

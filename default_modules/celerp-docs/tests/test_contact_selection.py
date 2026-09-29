# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Selecting a contact on a Document or List is one write.

The contact, its snapshot, payment terms, due date, currency, price list, repriced lines
and totals change together in one event, or nothing changes at all.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from test_helpers import grant_permission, perm_setup


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Select Co",
        "email": f"select-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner",
        "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return _h(r.json()["access_token"])


async def _item(client, h: dict, sku: str = "SEL-1") -> str:
    r = await client.post("/items", headers=h, json={
        "status": "available", "sku": sku, "name": "Select", "quantity": 10,
        "sell_by": "piece", "retail_price": 100, "wholesale_price": 80,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _contact(client, h: dict, **fields) -> str:
    """Create a contact; fields the create body does not take are set by a contact update."""
    later = {k: fields.pop(k) for k in ("price_list", "payment_terms", "tax_id") if k in fields}
    body = {"name": "Customer", "contact_type": "customer", **fields}
    r = await client.post("/crm/contacts", headers=h, json=body)
    assert r.status_code == 200, r.text
    contact_id = r.json()["id"]
    if later:
        r = await client.patch(f"/crm/contacts/{contact_id}", headers=h, json={
            "fields_changed": {k: {"old": None, "new": v} for k, v in later.items()},
        })
        assert r.status_code == 200, r.text
    return contact_id


def _line(item_id: str, sku: str = "SEL-1") -> dict:
    return {"item_id": item_id, "sku": sku, "description": "Select", "quantity": 2,
            "unit_price": 100, "line_total": 200}


async def _doc(client, h: dict, item_id: str | None, **fields) -> str:
    body = {"doc_type": "invoice", "price_list": "Retail", "currency": "USD",
            "issue_date": "2026-01-10", "line_items": [_line(item_id)] if item_id else [], **fields}
    r = await client.post("/docs", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _list(client, h: dict, item_id: str | None, **fields) -> str:
    body = {"list_type": "quotation", "price_list": "Retail", "currency": "USD",
            "line_items": [_line(item_id)] if item_id else [], **fields}
    r = await client.post("/lists", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _get(client, h: dict, resource: str, entity_id: str) -> dict:
    r = await client.get(f"/{resource}/{entity_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _select(client, h: dict, resource: str, entity_id: str, contact_id: str | None,
                  version: int | None = None, **extra):
    if version is None:
        version = (await _get(client, h, resource, entity_id))["version"]
    fields = {"contact_id": {"old": None, "new": contact_id}}
    fields.update({k: {"old": None, "new": v} for k, v in extra.items()})
    return await client.patch(f"/{resource}/{entity_id}", headers=h,
                              json={"fields_changed": fields, "expected_version": version})


_ADDRESSES = [
    {"address_type": "billing", "line1": "1 Bill St", "city": "Paris", "is_default": True},
    {"address_type": "shipping", "line1": "2 Ship Rd", "city": "Lyon", "attn": "Dock 4", "is_default": True},
]


async def _add_addresses(client, h: dict, contact_id: str) -> None:
    for a in _ADDRESSES:
        r = await client.post(f"/crm/contacts/{contact_id}/addresses", headers=h, json=a)
        assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_invoice_selection_applies_every_default_in_one_event(client):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, name="Euro Buyer", company_name="Euro Buyer SA",
                         email="buyer@example.test", phone="+33 1", tax_id="FR1",
                         currency="EUR", price_list="Wholesale", payment_terms="Net 30")
    await _add_addresses(client, h, cid)
    doc_id = await _doc(client, h, item_id, conversion_rate=1.1)
    before = await _get(client, h, "docs", doc_id)

    # A client-sent snapshot is ignored: the server copies the contact.
    r = await _select(client, h, "docs", doc_id, cid, contact_name="Spoofed")
    assert r.status_code == 200, r.text
    doc = await _get(client, h, "docs", doc_id)
    assert doc["contact_id"] == cid
    assert doc["contact_name"] == "Euro Buyer"
    assert doc["contact_company_name"] == "Euro Buyer SA"
    assert doc["contact_email"] == "buyer@example.test"
    assert doc["contact_phone"] == "+33 1"
    assert doc["contact_tax_id"] == "FR1"
    assert "1 Bill St" in doc["contact_billing_address"]
    assert "2 Ship Rd" in doc["contact_shipping_address"]
    assert doc["shipping_attn"] == "Dock 4"
    assert doc["payment_terms"] == "Net 30"
    assert doc["due_date"] == "2026-02-09"
    assert doc["currency"] == "EUR"
    assert doc.get("conversion_rate") is None
    assert doc["price_list"] == "Wholesale"
    assert doc["line_items"][0]["unit_price"] == 80
    assert doc["line_items"][0]["line_total"] == 160
    assert doc["subtotal"] == 160
    assert doc["total"] == 160
    assert doc["version"] > before["version"]


@pytest.mark.asyncio
async def test_selection_is_a_single_ledger_event(client, session):
    from sqlalchemy import func, select

    from celerp.models.ledger import LedgerEntry

    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, currency="EUR", price_list="Wholesale", payment_terms="Net 30")
    doc_id = await _doc(client, h, item_id)

    async def count() -> int:
        return (await session.execute(
            select(func.count()).select_from(LedgerEntry).where(LedgerEntry.entity_id == doc_id)
        )).scalar_one()

    before = await count()
    assert (await _select(client, h, "docs", doc_id, cid)).status_code == 200
    assert await count() == before + 1


@pytest.mark.asyncio
async def test_money_list_selection_is_one_step(client):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, name="List Buyer", currency="EUR", price_list="Wholesale")
    list_id = await _list(client, h, item_id)
    r = await _select(client, h, "lists", list_id, cid, contact_name="Spoofed")
    assert r.status_code == 200, r.text
    lst = await _get(client, h, "lists", list_id)
    assert lst["contact_id"] == cid
    assert lst["contact_name"] == "List Buyer"
    assert lst["currency"] == "EUR"
    assert lst["price_list"] == "Wholesale"
    assert lst["line_items"][0]["unit_price"] == 80
    assert lst["line_items"][0]["line_total"] == 160
    assert lst["total"] == 160


async def _assert_unchanged(client, h, resource, entity_id, before):
    after = await _get(client, h, resource, entity_id)
    for key in ("version", "contact_id", "contact_name", "currency", "price_list", "payment_terms", "total"):
        assert after.get(key) == before.get(key), key
    assert after["line_items"][0]["unit_price"] == before["line_items"][0]["unit_price"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_selection_without_price_permission_changes_nothing(client, session, resource):
    ctx = await perm_setup(client, session)
    admin, operator = ctx["admin_h"], ctx["operator_h"]
    await grant_permission(client, admin, "set_sales_doc_prices", "manager")
    item_id = await _item(client, admin)
    cid = await _contact(client, admin, currency="EUR", price_list="Wholesale", payment_terms="Net 30")
    entity_id = await (_doc if resource == "docs" else _list)(client, admin, item_id)
    before = await _get(client, admin, resource, entity_id)
    r = await _select(client, operator, resource, entity_id, cid)
    assert r.status_code == 403, r.text
    await _assert_unchanged(client, admin, resource, entity_id, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_selection_without_view_contacts_changes_nothing(client, session, resource):
    ctx = await perm_setup(client, session)
    admin, operator = ctx["admin_h"], ctx["operator_h"]
    await grant_permission(client, admin, "view_contacts", "manager")
    item_id = await _item(client, admin)
    cid = await _contact(client, admin, price_list="Retail")
    entity_id = await (_doc if resource == "docs" else _list)(client, admin, item_id)
    before = await _get(client, admin, resource, entity_id)
    r = await _select(client, operator, resource, entity_id, cid)
    assert r.status_code == 403, r.text
    assert "view_contacts" in r.text
    await _assert_unchanged(client, admin, resource, entity_id, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_selection_with_unknown_price_list_changes_nothing(client, resource):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, currency="EUR", price_list="No Such List", payment_terms="Net 30")
    entity_id = await (_doc if resource == "docs" else _list)(client, h, item_id)
    before = await _get(client, h, resource, entity_id)
    r = await _select(client, h, resource, entity_id, cid)
    assert r.status_code == 422, r.text
    assert "No Such List" in r.text
    await _assert_unchanged(client, h, resource, entity_id, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_stale_selection_changes_nothing(client, resource):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, currency="EUR", price_list="Wholesale")
    entity_id = await (_doc if resource == "docs" else _list)(client, h, item_id)
    before = await _get(client, h, resource, entity_id)
    r = await _select(client, h, resource, entity_id, cid, version=before["version"] - 1)
    assert r.status_code == 409, r.text
    await _assert_unchanged(client, h, resource, entity_id, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_lost_response_retry_replays_the_selection(client, resource):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, currency="EUR", price_list="Wholesale")
    entity_id = await (_doc if resource == "docs" else _list)(client, h, item_id)
    version = (await _get(client, h, resource, entity_id))["version"]
    first = await _select(client, h, resource, entity_id, cid, version=version)
    assert first.status_code == 200, first.text
    retry = await _select(client, h, resource, entity_id, cid, version=version)
    assert retry.status_code == 200, retry.text
    assert retry.json()["version"] == first.json()["version"]
    assert (await _get(client, h, resource, entity_id))["version"] == first.json()["version"]


@pytest.mark.asyncio
async def test_vendor_document_selection_does_not_reprice(client):
    h = await _owner(client)
    item_id = await _item(client, h)
    vid = await _contact(client, h, name="Supplier", contact_type="vendor",
                         price_list="Wholesale", payment_terms="Net 30")
    doc_id = await _doc(client, h, item_id, doc_type="purchase_order")
    r = await _select(client, h, "docs", doc_id, vid)
    assert r.status_code == 200, r.text
    doc = await _get(client, h, "docs", doc_id)
    assert doc["contact_name"] == "Supplier"
    assert doc["payment_terms"] == "Net 30"
    assert doc["price_list"] == "Retail"
    assert doc["line_items"][0]["unit_price"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(("doc_type", "contact_type", "role"), [
    ("purchase_order", "customer", "vendor"),
    ("invoice", "vendor", "customer"),
])
async def test_wrong_contact_type_is_refused(client, doc_type, contact_type, role):
    h = await _owner(client)
    cid = await _contact(client, h, name="Wrong Side", contact_type=contact_type)
    doc_id = await _doc(client, h, None, doc_type=doc_type)
    before = await _get(client, h, "docs", doc_id)
    r = await _select(client, h, "docs", doc_id, cid)
    assert r.status_code == 422, r.text
    assert f"Wrong Side is not a {role}" in r.text
    assert (await _get(client, h, "docs", doc_id))["version"] == before["version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("list_type", ["transfer", "audit"])
async def test_non_money_list_takes_the_customer_but_is_never_repriced(client, list_type):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h, name="Moving Customer", currency="EUR", price_list="Wholesale")
    list_id = await _list(client, h, item_id, list_type=list_type)
    assert (await _select(client, h, "lists", list_id, cid)).status_code == 200
    lst = await _get(client, h, "lists", list_id)
    assert lst["contact_name"] == "Moving Customer"
    assert lst["currency"] == "USD"
    assert lst["price_list"] == "Retail"
    assert lst["line_items"][0]["unit_price"] == 100


@pytest.mark.asyncio
async def test_list_refuses_a_vendor(client):
    h = await _owner(client)
    vid = await _contact(client, h, name="Only Supplier", contact_type="vendor")
    list_id = await _list(client, h, None)
    r = await _select(client, h, "lists", list_id, vid)
    assert r.status_code == 422, r.text
    assert "Only Supplier is not a customer" in r.text


@pytest.mark.asyncio
async def test_currency_change_clears_the_old_conversion_rate(client):
    h = await _owner(client)
    cid = await _contact(client, h, currency="EUR")
    doc_id = await _doc(client, h, None, currency="GBP", conversion_rate=1.27)
    assert (await _get(client, h, "docs", doc_id))["conversion_rate"] == 1.27
    assert (await _select(client, h, "docs", doc_id, cid)).status_code == 200
    doc = await _get(client, h, "docs", doc_id)
    assert doc["currency"] == "EUR"
    assert doc.get("conversion_rate") is None


@pytest.mark.asyncio
async def test_contact_without_currency_keeps_the_draft_currency(client):
    h = await _owner(client)
    cid = await _contact(client, h)
    doc_id = await _doc(client, h, None, currency="GBP", conversion_rate=1.27)
    assert (await _select(client, h, "docs", doc_id, cid)).status_code == 200
    doc = await _get(client, h, "docs", doc_id)
    assert doc["currency"] == "GBP"
    assert doc["conversion_rate"] == 1.27


@pytest.mark.asyncio
async def test_clearing_the_contact_clears_the_snapshot(client):
    h = await _owner(client)
    cid = await _contact(client, h, name="Gone Soon", email="gone@example.test", phone="1")
    await _add_addresses(client, h, cid)
    doc_id = await _doc(client, h, None)
    assert (await _select(client, h, "docs", doc_id, cid)).status_code == 200
    assert (await _select(client, h, "docs", doc_id, None)).status_code == 200
    doc = await _get(client, h, "docs", doc_id)
    assert not doc.get("contact_id")
    for field in ("contact_name", "contact_email", "contact_phone", "contact_billing_address",
                  "contact_shipping_address", "shipping_attn"):
        assert not doc.get(field), field


@pytest.mark.asyncio
async def test_contact_without_terms_takes_the_default_or_clears_the_old_terms(client):
    h = await _owner(client)
    with_terms = await _contact(client, h, name="Termed", payment_terms="Net 60")
    without = await _contact(client, h, name="Untermed")
    doc_id = await _doc(client, h, None)
    assert (await _select(client, h, "docs", doc_id, with_terms)).status_code == 200
    assert (await _get(client, h, "docs", doc_id))["payment_terms"] == "Net 60"

    assert (await _select(client, h, "docs", doc_id, without)).status_code == 200
    assert (await _get(client, h, "docs", doc_id)).get("payment_terms") is None

    r = await client.patch("/companies/me/contact-defaults", headers=h,
                           json={"defaults": {"default_payment_terms": "Net 15"}})
    assert r.status_code == 200, r.text
    assert (await _select(client, h, "docs", doc_id, with_terms)).status_code == 200
    assert (await _select(client, h, "docs", doc_id, without)).status_code == 200
    doc = await _get(client, h, "docs", doc_id)
    assert doc["payment_terms"] == "Net 15"
    assert doc["due_date"] == "2026-01-25"


@pytest.mark.asyncio
async def test_finalized_document_selection_changes_only_the_contact(client):
    h = await _owner(client)
    item_id = await _item(client, h)
    first = await _contact(client, h, name="First")
    second = await _contact(client, h, name="Second", currency="EUR", price_list="Wholesale")
    doc_id = await _doc(client, h, item_id, contact_id=first, contact_name="First")
    r = await client.post(f"/docs/{doc_id}/finalize", headers=h, json={})
    assert r.status_code == 200, r.text
    assert (await _select(client, h, "docs", doc_id, second)).status_code == 200
    doc = await _get(client, h, "docs", doc_id)
    assert doc["contact_name"] == "Second"
    assert doc["currency"] == "USD"
    assert doc["price_list"] == "Retail"
    assert doc["line_items"][0]["unit_price"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["docs", "lists"])
async def test_contact_and_lines_in_one_patch_is_refused(client, resource):
    h = await _owner(client)
    item_id = await _item(client, h)
    cid = await _contact(client, h)
    entity_id = await (_doc if resource == "docs" else _list)(client, h, item_id)
    r = await _select(client, h, resource, entity_id, cid, line_items=[_line(item_id)])
    assert r.status_code == 422, r.text
    assert "separate saves" in r.text


# Two users select different customers at the same version, through separate committed
# transactions. The contact and record row locks serialize them: one wins with its whole
# transition, the other is refused and leaves no trace.

async def _seed(factory, company_id, events):
    from celerp.events.engine import emit_event
    async with factory() as s:
        for entity_id, entity_type, event_type, data in events:
            await emit_event(
                s, company_id=company_id, entity_id=entity_id, entity_type=entity_type,
                event_type=event_type, data=data, actor_id=None, location_id=None, source="test",
                idempotency_key=str(uuid.uuid4()), metadata_={},
            )
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["doc", "list"])
async def test_concurrent_selections_have_one_coherent_winner(_db_engine, kind):
    from fastapi import HTTPException
    from sqlalchemy import delete

    from celerp.models.company import Company, User
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp_docs import routes
    from celerp_docs.routes import DocPatch, ListPatch

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="RaceCo", slug=f"race-{company_id.hex[:8]}"))
        s.add(User(id=user_id, email=f"race-{user_id.hex[:8]}@example.test", name="Editor"))
        await s.commit()
    entity_id = f"{kind}:RACE-SEL"
    base = {"status": "draft", "currency": "USD", "price_list": "Retail", "line_items": [],
            "ref_id": "RACE-SEL"}
    record = ({**base, "doc_type": "invoice"} if kind == "doc" else {**base, "list_type": "quotation"})
    await _seed(factory, company_id, [
        ("contact:A", "contact", "crm.contact.created", {"name": "Alpha", "contact_type": "customer", "currency": "EUR"}),
        ("contact:B", "contact", "crm.contact.created", {"name": "Beta", "contact_type": "customer", "currency": "JPY"}),
        (entity_id, kind, f"{kind}.created", record),
    ])
    try:
        async with factory() as s:
            version = (await s.get(Projection, {"company_id": company_id, "entity_id": entity_id})).version
        user = types.SimpleNamespace(id=user_id)
        patch_cls = DocPatch if kind == "doc" else ListPatch
        fn = routes.patch_doc if kind == "doc" else routes.patch_list

        async def select(contact_id: str):
            async with factory() as s:
                payload = patch_cls(fields_changed={"contact_id": {"old": None, "new": contact_id}},
                                    expected_version=version)
                try:
                    await fn(entity_id, payload, company_id=company_id, _=None, role="owner",
                             settings={}, user=user, session=s)
                    return 200
                except HTTPException as exc:
                    await s.rollback()
                    return exc.status_code

        results = await asyncio.gather(select("contact:A"), select("contact:B"))
        assert sorted(results) == [200, 409], results
        winner = "contact:A" if results[0] == 200 else "contact:B"
        async with factory() as s:
            state = (await s.get(Projection, {"company_id": company_id, "entity_id": entity_id})).state
        assert state["contact_id"] == winner
        assert state["contact_name"] == ("Alpha" if winner == "contact:A" else "Beta")
        assert state["currency"] == ("EUR" if winner == "contact:A" else "JPY")
    finally:
        async with factory() as s:
            await s.execute(delete(Projection).where(Projection.company_id == company_id))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
            await s.execute(delete(Company).where(Company.id == company_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()

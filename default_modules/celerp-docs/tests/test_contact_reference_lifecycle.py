# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A List's customer stays a live contact through merge and delete, and every surface
(detail, index, search, CSV, email) shows the same customer."""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from test_helpers import perm_setup
from ui.i18n import t


async def _contact(client, h: dict, name: str, **fields) -> str:
    r = await client.post("/crm/contacts", headers=h, json={"name": name, "contact_type": "customer", **fields})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _quotation(client, h: dict, **fields) -> str:
    r = await client.post("/lists", headers=h, json={
        "list_type": "quotation",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 5, "line_total": 5}],
        **fields,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _select(client, h: dict, list_id: str, contact_id: str):
    return await client.patch(f"/lists/{list_id}", headers=h, json={
        "fields_changed": {"contact_id": {"old": None, "new": contact_id}},
    })


async def _get_list(client, h: dict, list_id: str) -> dict:
    r = await client.get(f"/lists/{list_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ── Merge and delete ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_repoints_lists_to_the_surviving_contact(client, session):
    h = (await perm_setup(client, session))["admin_h"]
    source = await _contact(client, h, "Old Name Ltd")
    target = await _contact(client, h, "New Name Ltd")
    list_id = await _quotation(client, h)
    assert (await _select(client, h, list_id, source)).status_code == 200

    r = await client.post("/crm/contacts/merge", headers=h,
                          json={"target_contact_id": target, "source_contact_ids": [source]})
    assert r.status_code == 200, r.text

    lst = await _get_list(client, h, list_id)
    assert lst["contact_id"] == target
    assert lst["contact_name"] == "New Name Ltd"


@pytest.mark.asyncio
async def test_delete_refuses_a_contact_named_on_a_list(client, session):
    h = (await perm_setup(client, session))["admin_h"]
    cid = await _contact(client, h, "Quoted Co")
    list_id = await _quotation(client, h)
    assert (await _select(client, h, list_id, cid)).status_code == 200

    r = await client.post("/crm/contacts/bulk/delete", headers=h, json={"contact_ids": [cid]})
    assert r.status_code == 422, r.text
    assert "Quoted Co: 1 quotation(s)" in r.json()["detail"]
    contact = (await client.get(f"/crm/contacts/{cid}", headers=h)).json()
    assert not contact.get("deleted")
    assert (await _get_list(client, h, list_id))["contact_id"] == cid


@pytest.mark.asyncio
async def test_create_list_rejects_a_deleted_or_non_contact_reference(client, session):
    ctx = await perm_setup(client, session)
    h = ctx["admin_h"]
    cid = await _contact(client, h, "Gone Co")
    assert (await client.post("/crm/contacts/bulk/delete", headers=h, json={"contact_ids": [cid]})).status_code == 200

    r = await client.post("/lists", headers=h, json={"list_type": "quotation", "contact_id": cid})
    assert r.status_code == 422, r.text
    assert "deleted" in r.text
    r = await client.post("/lists", headers=h, json={"list_type": "quotation", "contact_id": ctx["item_id"]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("documents.err_contact_not_contact", "en")


# A selection and a merge or delete of the same contact are serialized by the contact row
# lock, taken first by both. Whichever commits second sees the first one's result, so a List
# never ends up naming a retired contact.

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
@pytest.mark.parametrize("retire", ["merge", "delete"])
async def test_selection_waits_for_a_merge_or_delete_in_progress(_db_engine, retire):
    from fastapi import HTTPException
    from sqlalchemy import delete

    from celerp.models.company import Company, User
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp_contacts import routes as contact_routes
    from celerp_docs import routes
    from celerp_docs.routes import ListPatch

    factory = async_sessionmaker(bind=_db_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="RetireCo", slug=f"retire-{company_id.hex[:8]}"))
        s.add(User(id=user_id, email=f"retire-{user_id.hex[:8]}@example.test", name="Editor"))
        await s.commit()
    await _seed(factory, company_id, [
        ("contact:A", "contact", "crm.contact.created", {"name": "Alpha", "contact_type": "customer"}),
        ("contact:B", "contact", "crm.contact.created", {"name": "Beta", "contact_type": "customer"}),
        ("list:RET-1", "list", "list.created",
         {"list_type": "quotation", "status": "draft", "currency": "USD", "ref_id": "RET-1", "line_items": []}),
    ])
    user = types.SimpleNamespace(id=user_id)
    try:
        crm = factory()
        if retire == "merge":
            await contact_routes.merge_contacts_service(
                crm, company_id, user_id, target_contact_id="contact:B", source_contact_ids=["contact:A"])
        else:
            await contact_routes.bulk_delete_contacts(
                contact_routes.BulkContactDeleteBody(contact_ids=["contact:A"]),
                company_id=company_id, user=user, _=None, session=_NoCommit(crm))

        async def select() -> int:
            async with factory() as s:
                payload = ListPatch(fields_changed={"contact_id": {"old": None, "new": "contact:A"}})
                try:
                    await routes.patch_list("list:RET-1", payload, company_id=company_id, _=None,
                                            role="owner", settings={}, user=user, session=s)
                    return 200
                except HTTPException as exc:
                    await s.rollback()
                    return exc.status_code

        selecting = asyncio.create_task(select())
        await asyncio.sleep(0.5)
        assert not selecting.done(), "the selection must wait for the contact lock"
        await crm.commit()
        await crm.close()
        assert await selecting == 422

        async with factory() as s:
            state = (await s.get(Projection, {"company_id": company_id, "entity_id": "list:RET-1"})).state
        assert state.get("contact_id") is None
    finally:
        async with factory() as s:
            await s.execute(delete(Projection).where(Projection.company_id == company_id))
            await s.execute(delete(LedgerEntry).where(LedgerEntry.company_id == company_id))
            await s.execute(delete(Company).where(Company.id == company_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()


class _NoCommit:
    """The bulk delete route commits itself; this holds its transaction open for the test."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def commit(self):
        await self._session.flush()


# ── One customer on every surface ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_selected_list_customer_shows_on_every_surface(client, session, monkeypatch):
    from celerp_docs import routes

    h = (await perm_setup(client, session))["admin_h"]
    cid = await _contact(client, h, "Harbour Traders")
    list_id = await _quotation(client, h)
    assert (await _select(client, h, list_id, cid)).status_code == 200

    assert (await _get_list(client, h, list_id))["contact_name"] == "Harbour Traders"
    index = (await client.get("/lists", headers=h)).json()["items"]
    assert [r["customer"] for r in index if r["id"] == list_id] == ["Harbour Traders"]
    for q in ("harbour", cid):
        found = (await client.get("/lists", headers=h, params={"q": q})).json()["items"]
        assert [r["id"] for r in found] == [list_id], q
    csv_text = (await client.get("/lists/export/csv", headers=h)).text
    assert "Harbour Traders" in csv_text

    sent: list[dict] = []
    monkeypatch.setattr(routes, "_email_with_receipt", lambda *a, **kw: sent.append(kw))
    assert (await client.post(f"/lists/{list_id}/finalize", headers=h)).status_code == 200
    r = await client.post(f"/lists/{list_id}/send", headers=h, json={"sent_to": "buyer@example.test"})
    assert r.status_code == 200, r.text
    assert "Harbour Traders" in sent[0]["body_text"]


@pytest.mark.asyncio
async def test_legacy_customer_fields_become_the_list_contact(client, session):
    h = (await perm_setup(client, session))["admin_h"]
    list_id = await _quotation(client, h, customer_id="contact:legacy-1", customer_name="Legacy Buyer")

    lst = await _get_list(client, h, list_id)
    assert (lst["contact_id"], lst["contact_name"]) == ("contact:legacy-1", "Legacy Buyer")
    assert "customer_name" not in lst and "customer_id" not in lst
    index = (await client.get("/lists", headers=h, params={"q": "legacy buyer"})).json()["items"]
    assert [r["customer"] for r in index] == ["Legacy Buyer"]


def test_list_projection_folds_legacy_customer_fields():
    from celerp_docs.doc_projections import apply_documents_event

    state = apply_documents_event({}, "list.created", {
        "list_type": "quotation", "customer_id": "contact:x", "customer_name": "Legacy Buyer",
    })
    assert (state["contact_id"], state["contact_name"]) == ("contact:x", "Legacy Buyer")
    assert "customer_id" not in state and "customer_name" not in state
    state = apply_documents_event(state, "list.updated", {
        "fields_changed": {"customer_name": {"old": "Legacy Buyer", "new": "Renamed Buyer"}},
    })
    assert state["contact_name"] == "Renamed Buyer" and "customer_name" not in state
    kept = apply_documents_event({}, "list.created", {
        "list_type": "transfer", "contact_name": "Canonical", "receiver": "Old Receiver",
    })
    assert kept["contact_name"] == "Canonical" and "receiver" not in kept
    upserted = apply_documents_event(kept, "list.patched", {"customer_name": "Upserted"})
    assert upserted["contact_name"] == "Upserted" and "customer_name" not in upserted


# ── Currency at every write boundary ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_currency_is_refused_at_every_write(client, session):
    h = (await perm_setup(client, session))["admin_h"]
    r = await client.post("/crm/contacts", headers=h, json={"name": "Bad", "currency": "ZZZ"})
    assert r.status_code == 422, r.text
    cid = await _contact(client, h, "Good", currency="EUR")
    r = await client.patch(f"/crm/contacts/{cid}", headers=h,
                           json={"fields_changed": {"currency": {"old": "EUR", "new": "ZZZ"}}})
    assert r.status_code == 422, r.text
    assert (await client.get(f"/crm/contacts/{cid}", headers=h)).json()["currency"] == "EUR"

    list_id = await _quotation(client, h)
    r = await client.patch(f"/lists/{list_id}", headers=h,
                           json={"fields_changed": {"currency": {"old": None, "new": "ZZZ"}}})
    assert r.status_code == 422, r.text
    assert (await _get_list(client, h, list_id))["currency"] != "ZZZ"

    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "line_items": []})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.patch(f"/docs/{doc_id}", headers=h,
                           json={"fields_changed": {"currency": {"old": None, "new": "ZZZ"}}})
    assert r.status_code == 422, r.text
    assert (await client.get(f"/docs/{doc_id}", headers=h)).json()["currency"] != "ZZZ"

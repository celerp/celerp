# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""P2: collapse a legacy two-contact company (customer + vendor self-contacts) into one both+is_self."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.company import Company, User
from celerp.models.projections import Projection
from celerp_contacts.migrations import migrate_self_contacts


async def _register(client) -> None:
    addr = f"admin-{uuid.uuid4().hex[:8]}@selfmig.test"
    r = await client.post("/auth/register", json={"company_name": "SelfMig Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200


async def _emit(session, **kw):
    await emit_event(session, location_id=None, metadata_={}, **kw)


@pytest.mark.asyncio
async def test_migrate_collapses_legacy_self_contacts(client, session):
    await _register(client)
    company = (await session.execute(select(Company))).scalars().first()
    user = (await session.execute(select(User))).scalars().first()
    cid = company.id

    # Simulate a LEGACY company: emit the two registration self-contacts directly, plus a real invoice
    # on the customer-self and a real bill on the vendor-self. The vendor carries a tax_id the customer
    # lacks (exercises the field backfill, T5).
    cust_id = f"contact:{uuid.uuid4()}"
    vend_id = f"contact:{uuid.uuid4()}"
    await _emit(session, company_id=cid, entity_id=cust_id, entity_type="contact",
                event_type="crm.contact.created",
                data={"name": "SelfMig Co", "company_name": "SelfMig Co", "email": "self@mig.test", "contact_type": "customer"},
                actor_id=user.id, source="registration", idempotency_key=f"reg:contact:customer:{cid}")
    await _emit(session, company_id=cid, entity_id=vend_id, entity_type="contact",
                event_type="crm.contact.created",
                data={"name": "SelfMig Co", "company_name": "SelfMig Co", "tax_id": "TAX-123", "contact_type": "vendor"},
                actor_id=user.id, source="registration", idempotency_key=f"reg:contact:vendor:{cid}")
    await _emit(session, company_id=cid, entity_id="doc:inv-1", entity_type="doc", event_type="doc.created",
                data={"doc_type": "invoice", "contact_id": cust_id, "contact_name": "SelfMig Co"},
                actor_id=user.id, source="test", idempotency_key="t:inv")
    await _emit(session, company_id=cid, entity_id="doc:bill-1", entity_type="doc", event_type="doc.created",
                data={"doc_type": "bill", "contact_id": vend_id, "contact_name": "SelfMig Co"},
                actor_id=user.id, source="test", idempotency_key="t:bill")
    await session.commit()

    res = await migrate_self_contacts(session, cid, user.id)
    await session.commit()
    assert res["status"] == "migrated" and res["winner"] == cust_id and res["merged_vendor"] is True

    # Winner retyped both+is_self; tax_id backfilled from vendor (T5).
    win = await session.get(Projection, {"company_id": cid, "entity_id": cust_id})
    assert win.state["contact_type"] == "both" and win.state["is_self"] is True
    assert win.state.get("tax_id") == "TAX-123"
    # Vendor tombstoned into the winner.
    ven = await session.get(Projection, {"company_id": cid, "entity_id": vend_id})
    assert ven.state.get("deleted") is True and ven.state.get("merged_into") == cust_id

    # Both docs now resolve to the winner (invoice unchanged, bill re-pointed).
    inv = await session.get(Projection, {"company_id": cid, "entity_id": "doc:inv-1"})
    bill = await session.get(Projection, {"company_id": cid, "entity_id": "doc:bill-1"})
    assert inv.state["contact_id"] == cust_id
    assert bill.state["contact_id"] == cust_id

    # self_contact_id cached on the company.
    await session.refresh(company)
    assert company.settings.get("self_contact_id") == cust_id

    # Idempotent: re-running collapses nothing further and never errors.
    res2 = await migrate_self_contacts(session, cid, user.id)
    await session.commit()
    assert res2["merged_vendor"] is False
    assert (await session.get(Projection, {"company_id": cid, "entity_id": vend_id})).state.get("merged_into") == cust_id


@pytest.mark.asyncio
async def test_migrate_noop_for_new_seed_company(client, session):
    """A company seeded under the new model (one both+is_self contact, no legacy keys) is left alone."""
    await _register(client)
    company = (await session.execute(select(Company))).scalars().first()
    user = (await session.execute(select(User))).scalars().first()
    res = await migrate_self_contacts(session, company.id, user.id)
    assert res["status"] == "noop"


@pytest.mark.asyncio
async def test_backfill_hook_idempotent_and_skips_seeded(client, session):
    """The on_modules_ready hook runs on startup: a new-model company (self_contact_id already cached)
    is skipped untouched, and re-running never duplicates anything."""
    from celerp_contacts.migrations import backfill_self_contacts_hook
    await _register(client)
    company = (await session.execute(select(Company))).scalars().first()
    before = (company.settings or {}).get("self_contact_id")
    assert before, "P1 seed should have cached self_contact_id"

    await backfill_self_contacts_hook(session=session)
    await backfill_self_contacts_hook(session=session)  # idempotent
    await session.refresh(company)
    assert (company.settings or {}).get("self_contact_id") == before

    contacts = (await session.execute(
        select(Projection).where(Projection.company_id == company.id, Projection.entity_type == "contact")
    )).scalars().all()
    selfs = [c for c in contacts if c.state.get("is_self") and not c.state.get("deleted")]
    assert len(selfs) == 1  # no duplication


@pytest.mark.asyncio
async def test_migrate_single_survivor_just_retypes(client, session):
    """If only one legacy self-contact remains (the other was deleted), retype it - no merge needed."""
    await _register(client)
    company = (await session.execute(select(Company))).scalars().first()
    user = (await session.execute(select(User))).scalars().first()
    cid = company.id
    only_id = f"contact:{uuid.uuid4()}"
    await _emit(session, company_id=cid, entity_id=only_id, entity_type="contact",
                event_type="crm.contact.created",
                data={"name": "Solo", "company_name": "Solo", "contact_type": "customer"},
                actor_id=user.id, source="registration", idempotency_key=f"reg:contact:customer:{cid}")
    await session.commit()

    res = await migrate_self_contacts(session, cid, user.id)
    await session.commit()
    assert res["status"] == "migrated" and res["winner"] == only_id and res["merged_vendor"] is False
    win = await session.get(Projection, {"company_id": cid, "entity_id": only_id})
    assert win.state["contact_type"] == "both" and win.state["is_self"] is True
    await session.refresh(company)
    assert company.settings.get("self_contact_id") == only_id


@pytest.mark.asyncio
async def test_address_backfill_from_default_location(client, session):
    """The company's default-Location address is copied onto the self-contact as a billing address when
    the self-contact has none (so Company Details + letterhead show it). Idempotent."""
    from celerp.models.company import Location
    from celerp_contacts.migrations import backfill_self_contact_address
    await _register(client)
    company = (await session.execute(select(Company))).scalars().first()
    cid = company.id
    sid = (company.settings or {})["self_contact_id"]  # set by the P1 seed
    # Registration seeds a default Head Office location; set its address (as company setup would).
    loc = (await session.execute(
        select(Location).where(Location.company_id == cid, Location.is_default.is_(True))
    )).scalars().first()
    loc.address = {"text": "1 Main St, Town"}
    await session.commit()

    assert await backfill_self_contact_address(session, cid) is True
    await session.commit()
    contact = await session.get(Projection, {"company_id": cid, "entity_id": sid})
    addrs = contact.state.get("addresses") or []
    assert len(addrs) == 1
    assert addrs[0]["address_type"] == "billing" and addrs[0]["line1"] == "1 Main St, Town"

    # Idempotent: re-run does not add another.
    assert await backfill_self_contact_address(session, cid) is False

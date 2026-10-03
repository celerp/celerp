# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Two imports of the same new contact at the same moment, on real PostgreSQL.

The second import waits for the first and matches the contact the first created, so
the company ends with one contact holding both files' details. An importer whose
access is taken away while waiting is refused and writes nothing.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_fresh_authority_races_pg import _app_client, _direct, _http, _ok, _race, _refused, _revoke, _seed

pytestmark = pytest.mark.asyncio

_EMAIL = "race@contact.test"


def _import(client, h, pending, data: dict):
    body = {"records": [{"entity_id": f"contact:{uuid.uuid4()}", "event_type": "crm.contact.created",
                         "data": data, "source": "csv_import", "idempotency_key": f"k-{uuid.uuid4().hex}"}]}
    return lambda held: _http(pending, held, lambda: client.post("/crm/contacts/import/batch", json=body, headers=h))


async def _contacts(factory, company_id) -> tuple[list[dict], int]:
    async with factory() as s:
        states = (await s.execute(select(Projection.state).where(
            Projection.company_id == company_id, Projection.entity_type == "contact"))).scalars().all()
        events = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == company_id, LedgerEntry.entity_type == "contact"))).scalar_one()
    return [dict(st) for st in states], events


@pytest.mark.parametrize("first, second", [
    ({"name": "Race Co", "email": _EMAIL}, {"name": "Race Co", "email": _EMAIL, "phone": "+66 2 000 0000"}),
    ({"name": "Race Co", "email": _EMAIL, "phone": "+66 2 000 0000"}, {"name": "Race Co", "email": _EMAIL}),
])
async def test_two_imports_of_one_new_contact_at_once_make_one_contact(committed_engine, first, second):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    h = c["h"]["manager"]
    async with _app_client(factory) as (client, pending):
        one, two = await _race(committed_engine, _import(client, h, pending, first), _import(client, h, pending, second))
    _ok(one)
    _ok(two)
    assert one.json()["created"] == 1, one.json()
    assert two.json()["created"] == 0 and two.json()["errors"] == [], two.json()
    [contact], _ = await _contacts(factory, c["company_id"])
    assert contact["email"] == _EMAIL and contact["phone"] == "+66 2 000 0000"


async def test_an_importer_whose_access_is_revoked_while_waiting_writes_nothing(committed_engine):
    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    c = await _seed(factory)
    before = await _contacts(factory, c["company_id"])
    async with _app_client(factory) as (client, pending):
        changed, wrote = await _race(
            committed_engine,
            lambda held: _direct(factory, held, _revoke(c["company_id"], "import_export_data", "manager")),
            _import(client, c["h"]["manager"], pending, {"name": "Late", "email": "late@contact.test"}),
        )
    _ok(changed)
    _refused(wrote, 403)
    assert await _contacts(factory, c["company_id"]) == before

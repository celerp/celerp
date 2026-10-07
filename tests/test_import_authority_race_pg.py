# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Imports are judged by the importer's authority once they hold the company lock.

Each case holds the company lock in one transaction, starts an import that was
authorized when it began and now waits for that lock, takes away the importer's
role or one of the two permissions the import needs in the holding transaction,
then releases the lock. The import must be refused and leave what it would have
written exactly as it was. A waiter that is still authorized imports normally.
"""

from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp.routers import companies
from celerp.services.company_lock import lock_company, locked_company
from celerp.services.permissions import role_has_permission
from celerp_docs import routes as docs
from celerp_inventory import routes as inventory
from celerp_inventory.services import BatchImportRequest

pytestmark = [pytest.mark.asyncio, pytest.mark.process]


def _rec(entity_id: str, event_type: str, data: dict) -> dict:
    return {"entity_id": entity_id, "event_type": event_type, "data": data,
            "source": "csv", "idempotency_key": uuid.uuid4().hex}


def _setting(route, key, data):
    """A settings import: what it writes is one key of the company settings."""
    return {
        "keys": ("manage_company_settings", "import_export_data"),
        "call": lambda s, cid, user: route(
            companies.SettingsBatchImportRequest(records=[_rec("x", "import", data)]),
            company_id=cid, user=user, session=s),
        "stored": ("SELECT (settings::jsonb -> :key)::text FROM companies WHERE id = :id", {"key": key}),
        "added": lambda outcome: outcome.created == 1,
    }


def _entity(keys, call, entity_id):
    """A record import: what it writes is the record's projection and ledger events."""
    return {
        "keys": keys,
        "call": call,
        "stored": ("SELECT (SELECT count(*) FROM projections WHERE company_id = :id AND entity_id = :eid)"
                   " + (SELECT count(*) FROM ledger WHERE company_id = :id AND entity_id = :eid)",
                   {"eid": entity_id}),
        "added": lambda outcome: (outcome.created if hasattr(outcome, "created") else 1) == 1,
    }


_DOC = _rec("doc:RACED", "doc.created", {"doc_type": "invoice", "total": 1})
_LIST = _rec("list:RACED", "list.created", {"ref_id": "RACED", "list_type": "sale", "total": 1})
_ITEM = _rec("item:raced", "item.created", {"sku": "RACED", "name": "Raced", "quantity": 1, "sell_by": "piece"})
_DOCS = ("edit_documents", "import_export_data")

_IMPORTS = {
    "taxes": _setting(companies.import_taxes_batch, "taxes", {"name": "Raced tax", "rate": 3}),
    "payment_terms": _setting(companies.import_payment_terms_batch, "payment_terms", {"name": "Raced terms", "days": 9}),
    "purchasing_taxes": _setting(
        companies.import_purchasing_taxes_batch, "purchasing_taxes", {"name": "Raced tax", "rate": 3}),
    "purchasing_payment_terms": _setting(
        companies.import_purchasing_payment_terms_batch, "purchasing_payment_terms", {"name": "Raced terms", "days": 9}),
    "doc": _entity(_DOCS, lambda s, cid, user: docs.import_doc(
        docs.DocImportRecord(**_DOC), company_id=cid, user=user, session=s), _DOC["entity_id"]),
    "doc_batch": _entity(_DOCS, lambda s, cid, user: docs.batch_import_docs(
        docs.DocBatchImportRequest(records=[_DOC]), company_id=cid, user=user, session=s), _DOC["entity_id"]),
    "list": _entity(_DOCS, lambda s, cid, user: docs.import_list(
        docs.ListImportRecord(**_LIST), company_id=cid, user=user, session=s), _LIST["entity_id"]),
    "list_batch": _entity(_DOCS, lambda s, cid, user: docs.batch_import_lists(
        docs.ListBatchImportRequest(records=[_LIST]), company_id=cid, user=user, session=s), _LIST["entity_id"]),
    "item_batch": _entity(("import_export_data", "edit_inventory"), lambda s, cid, user: inventory.batch_import_items(
        BatchImportRequest(records=[_ITEM]), company_id=cid, user=user, session=s), _ITEM["entity_id"]),
}


async def _seed(factory) -> tuple[uuid.UUID, uuid.UUID]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="ImportRace", slug=f"race-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await s.commit()
    return company_id, user_id


async def _revoke_role(s, company_id, user_id, keys):
    await s.execute(update(UserCompany).where(
        UserCompany.user_id == user_id, UserCompany.company_id == company_id,
    ).values(role="viewer"))


def _revoke_grant(index):
    async def _revoke(s, company_id, user_id, keys):
        company = await locked_company(s, company_id)
        company.settings = {**(company.settings or {}), "role_grants": {keys[index]: ["owner"]}}
    return _revoke


async def _keep(s, company_id, user_id, keys):
    """Positive control: the holder changes nothing about the importer's authority."""


async def _stored(engine, company_id, case):
    """What the import would write, exactly as stored."""
    sql, params = case["stored"]
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), {**params, "id": company_id})).scalar_one()


async def _until_blocked(engine, task: asyncio.Task) -> None:
    """Wait until the import is blocked on a row lock (pg_stat_activity is a
    per-transaction snapshot, so every poll opens its own)."""
    for _ in range(400):
        assert not task.done(), f"the import did not wait for the company lock: {task.exception()!r}"
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the import never blocked on the company lock")


async def _race(engine, name, change):
    """Hold the lock, start the import, apply ``change`` in the holding
    transaction, release. Returns (company_id, stored before, import outcome)."""
    case = _IMPORTS[name]
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _seed(factory)
    # The request-start permission dependencies pass for this importer.
    for k in case["keys"]:
        assert role_has_permission({}, "admin", k)
    before = await _stored(engine, company_id, case)

    async with factory() as holder, factory() as s:
        await lock_company(holder, company_id)
        task = asyncio.create_task(case["call"](s, company_id, types.SimpleNamespace(id=user_id)))
        await _until_blocked(engine, task)
        await change(holder, company_id, user_id, case["keys"])
        await holder.commit()
        outcome = (await asyncio.gather(asyncio.wait_for(task, timeout=30), return_exceptions=True))[0]
    return company_id, before, outcome


@pytest.mark.parametrize("name", list(_IMPORTS))
@pytest.mark.parametrize(
    "revoke", [_revoke_role, _revoke_grant(0), _revoke_grant(1)],
    ids=["role", "first_permission", "second_permission"],
)
async def test_import_refuses_authority_revoked_while_it_waited_for_the_lock(committed_engine, name, revoke):
    company_id, before, outcome = await _race(committed_engine, name, revoke)
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 403
    assert await _stored(committed_engine, company_id, _IMPORTS[name]) == before


@pytest.mark.parametrize("name", list(_IMPORTS))
async def test_import_still_authorized_after_the_wait_imports(committed_engine, name):
    company_id, before, outcome = await _race(committed_engine, name, _keep)
    assert not isinstance(outcome, BaseException), outcome
    assert _IMPORTS[name]["added"](outcome)
    assert await _stored(committed_engine, company_id, _IMPORTS[name]) != before

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Settings imports are judged by the importer's authority once they hold the
company lock.

Each case holds the company lock in one transaction, starts an import that was
authorized when it began and now waits for that lock, takes away the importer's
role or one of the two permissions the import needs in the holding transaction,
then releases the lock. The import must be refused and leave the imported
setting exactly as it was. A waiter that is still authorized imports normally.
"""

from __future__ import annotations

import asyncio
import json
import types
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.routers import companies
from celerp.services.company_lock import lock_company, locked_company
from celerp.services.permissions import role_has_permission

pytestmark = pytest.mark.asyncio

_KEYS = ("manage_company_settings", "import_export_data")

# (route function, settings key it writes, one record it would add)
_IMPORTS = {
    "taxes": (companies.import_taxes_batch, "taxes", {"name": "Raced tax", "rate": 3}),
    "payment_terms": (companies.import_payment_terms_batch, "payment_terms", {"name": "Raced terms", "days": 9}),
    "purchasing_taxes": (
        companies.import_purchasing_taxes_batch, "purchasing_taxes", {"name": "Raced tax", "rate": 3}),
    "purchasing_payment_terms": (
        companies.import_purchasing_payment_terms_batch, "purchasing_payment_terms",
        {"name": "Raced terms", "days": 9}),
}


async def _seed(factory) -> tuple[uuid.UUID, uuid.UUID]:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="ImportRace", slug=f"race-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"a-{user_id.hex[:8]}@example.test", name="Admin",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="admin", is_active=True))
        await s.commit()
    return company_id, user_id


async def _revoke_role(s, company_id, user_id):
    await s.execute(update(UserCompany).where(
        UserCompany.user_id == user_id, UserCompany.company_id == company_id,
    ).values(role="viewer"))


def _revoke_grant(key):
    async def _revoke(s, company_id, user_id):
        company = await locked_company(s, company_id)
        company.settings = {**(company.settings or {}), "role_grants": {key: ["owner"]}}
    return _revoke


async def _keep(s, company_id, user_id):
    """Positive control: the holder changes nothing about the importer's authority."""


async def _stored(engine, company_id, key) -> str | None:
    """The imported setting exactly as stored (NULL when the key is absent)."""
    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT (settings::jsonb -> :key)::text FROM companies WHERE id = :id"),
            {"key": key, "id": company_id},
        )).scalar_one()


async def _until_blocked(engine, task: asyncio.Task) -> None:
    """Wait until the import is blocked on a row lock (pg_stat_activity is a
    per-transaction snapshot, so every poll opens its own)."""
    for _ in range(400):
        assert not task.done(), "the import did not wait for the company lock"
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
    transaction, release. Returns (company_id, setting before, import outcome)."""
    route, key, record = _IMPORTS[name]
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _seed(factory)
    # The request-start permission dependencies pass for this importer.
    for k in _KEYS:
        assert role_has_permission({}, "admin", k)
    before = await _stored(engine, company_id, key)

    payload = companies.SettingsBatchImportRequest(records=[{
        "entity_id": "x", "event_type": "import", "data": record,
        "source": "csv", "idempotency_key": uuid.uuid4().hex,
    }])
    async with factory() as holder, factory() as s:
        await lock_company(holder, company_id)
        task = asyncio.create_task(route(
            payload, company_id=company_id, user=types.SimpleNamespace(id=user_id), session=s,
        ))
        await _until_blocked(engine, task)
        await change(holder, company_id, user_id)
        await holder.commit()
        outcome = (await asyncio.gather(asyncio.wait_for(task, timeout=30), return_exceptions=True))[0]
    return company_id, key, before, outcome


@pytest.mark.parametrize("name", list(_IMPORTS))
@pytest.mark.parametrize(
    "revoke", [_revoke_role, _revoke_grant("manage_company_settings"), _revoke_grant("import_export_data")],
    ids=["role", "manage_company_settings", "import_export_data"],
)
async def test_settings_import_refuses_authority_revoked_while_it_waited_for_the_lock(
    committed_engine, name, revoke,
):
    company_id, key, before, outcome = await _race(committed_engine, name, revoke)
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == 403
    assert await _stored(committed_engine, company_id, key) == before


@pytest.mark.parametrize("name", list(_IMPORTS))
async def test_settings_import_still_authorized_after_the_wait_imports(committed_engine, name):
    company_id, key, _before, outcome = await _race(committed_engine, name, _keep)
    assert not isinstance(outcome, BaseException), outcome
    assert outcome.created == 1
    stored = json.loads(await _stored(committed_engine, company_id, key))
    assert _IMPORTS[name][2]["name"] in [row["name"] for row in stored]

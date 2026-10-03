# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Migration session ownership and staged-company isolation.

Moving another company in never replaces the owner's working session: the run is
controlled by its creator from any of their sessions, a token scoped to the staged
company reaches only the migration surfaces, and the owner keeps operating their
active company while the other one is staged."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from migration_support import (
    RUN_ROUTES,
    OWNER_EMAIL,
    OWNER_PASSWORD,
    auth,
    fake_bytes,
    finalize_run,
    load_run,
    maker,
    migrate_as_owner,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    save_decisions,
    scan_upload,
    staged_run,
)
from test_helpers import create_item, default_location_id, make_authed_token, register_admin

NOT_FOUND = "Migration not found."
STAGED = "This company is still being moved into Celerp. Finish or discard the migration first."


async def _company(session_or_engine, company_id):
    from celerp.models.company import Company
    async with maker(session_or_engine)() as s:
        return await s.get(Company, uuid.UUID(str(company_id)))


async def _link(engine, user_id, company_id, role: str = "owner", link_id: uuid.UUID | None = None):
    from celerp.models.accounting import UserCompany
    async with maker(engine)() as s:
        s.add(UserCompany(id=link_id or uuid.uuid4(), user_id=user_id, company_id=company_id, role=role))
        await s.commit()


async def _token(engine, user_id, company_id, role: str = "owner") -> str:
    async with maker(engine)() as s:
        return await make_authed_token(s, str(user_id), str(company_id), role)


@pytest.mark.asyncio
async def test_staged_flag_is_server_controlled_and_cleared_by_finalize(real_client, real_engine, migration_env):
    from celerp.models.company import Company
    from celerp.services import migrations

    admin_token = await register_admin(real_client)
    async with maker(real_engine)() as s:
        normal = await s.scalar(select(Company).where(Company.name == "Perm Co"))
    assert normal.is_migration_staged is False

    run_id, company_id, user_id = await staged_run(real_engine)
    staged = await _company(real_engine, company_id)
    assert (staged.is_active, staged.is_migration_staged) == (False, True)

    # Not a company setting: a settings write cannot clear it.
    r = await real_client.patch("/companies/me", headers=auth(admin_token), json={"is_migration_staged": True})
    assert (await _company(real_engine, normal.id)).is_migration_staged is False, r.text

    await migrations.run_migration(run_id)
    async with maker(real_engine)() as s:
        await finalize_run(s, await migrations.get_owned_migration_run(s, run_id, user_id))
    done = await _company(real_engine, company_id)
    assert (done.is_active, done.is_migration_staged) == (True, False)


@pytest.mark.asyncio
async def test_staged_token_reaches_only_migration_routes(real_client, real_engine, migration_env):
    run_id, company_id, user_id = await staged_run(real_engine, start=False)
    token = await _token(real_engine, user_id, company_id)

    r = await real_client.post("/docs", json={"doc_type": "invoice"}, headers=auth(token))
    assert r.status_code == 403 and r.json()["detail"] == STAGED, r.text
    r = await real_client.patch(f"/items/{uuid.uuid4()}", headers=auth(token),
                                json={"fields_changed": {"name": {"old": None, "new": "X"}}})
    assert r.status_code == 403 and r.json()["detail"] == STAGED, r.text
    r = await real_client.get("/companies/me", headers=auth(token))
    assert r.status_code == 403 and r.json()["detail"] == STAGED, r.text

    assert (await real_client.get(f"/migrations/{run_id}", headers=auth(token))).status_code == 200
    r = await real_client.post(f"/migrations/{run_id}/start", headers=auth(token))
    assert r.status_code == 202, r.text
    r = await real_client.post(f"/migrations/{run_id}/cancel", headers=auth(token))
    assert r.status_code == 202 and r.json()["status"] == "cancel_requested", r.text
    r = await real_client.get("/migrations/staged", headers=auth(token))
    assert r.status_code == 200 and r.json()["id"] == str(run_id), r.text


@pytest.mark.asyncio
async def test_start_from_scan_keeps_the_working_session_and_company(real_client, real_engine, migration_env):
    """The owner's Company A token starts, watches and controls the run, and keeps
    writing documents, payments and inventory in A while the other company is staged."""
    admin_token = await register_admin(real_client)
    headers = auth(admin_token)

    r = await scan_upload(real_client, fake_bytes(), token=admin_token)
    scan_token = r.json()["scan_token"]
    assert (await save_decisions(real_client, scan_token, token=admin_token)).status_code == 200
    r = await real_client.post("/migrations/start-from-scan", headers=headers,
                               json={"scan_token": scan_token, "company_name": "Moved Co"})
    assert r.status_code == 201, r.text
    assert set(r.json()) == {"run_id"}
    run_id = r.json()["run_id"]

    r = await real_client.get(f"/migrations/{run_id}", headers=headers)
    assert r.status_code == 200 and r.json()["company_name"] == "Moved Co", r.text
    r = await real_client.post(f"/migrations/{run_id}/cancel", headers=headers)
    assert r.status_code == 202, r.text

    me = await real_client.get("/companies/me", headers=headers)
    assert me.status_code == 200 and me.json()["name"] == "Perm Co"
    location_id = await default_location_id(real_client, headers)
    item_id = await create_item(real_client, headers, location_id, sku="SKU-A-1")
    r = await real_client.patch(f"/items/{item_id}", headers=headers,
                                json={"fields_changed": {"name": {"old": "Perm Item", "new": "Renamed"}}})
    assert r.status_code == 200, r.text
    r = await real_client.post("/docs", headers=headers, json={
        "doc_type": "invoice", "total": 10.0,
        "line_items": [{"entity_id": item_id, "sku": "SKU-A-1", "name": "Renamed",
                        "quantity": 1, "unit_price": 10.0, "sell_by": "piece"}]})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert (await real_client.post(f"/docs/{doc_id}/finalize", headers=headers)).status_code == 200
    r = await real_client.post(f"/docs/{doc_id}/payment", headers=headers, json={
        "amount": 10.0, "payment_date": "2026-07-01", "bank_account": "1111"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_only_the_creator_controls_a_run(real_client, real_engine, migration_env):
    """Another owner of the staged company, even on a token scoped to it, cannot see or act on the run."""
    from celerp.models.company import User

    admin_token = await register_admin(real_client)
    run_id = await migrate_as_owner(real_client, admin_token)
    run = await load_run(real_engine, uuid.UUID(run_id))
    async with maker(real_engine)() as s:
        other = User(email="other-owner@example.com", name="Other")
        s.add(other)
        await s.commit()
    await _link(real_engine, other.id, run.company_id)
    other_token = await _token(real_engine, other.id, run.company_id)

    for method, path in RUN_ROUTES:
        r = await getattr(real_client, method)(f"/migrations/{run_id}{path}", headers=auth(other_token))
        assert r.status_code == 404 and r.json() == {"detail": NOT_FOUND}, (path, r.text)
    assert (await load_run(real_engine, uuid.UUID(run_id))).status == "running"


@pytest.mark.asyncio
async def test_login_prefers_an_active_company_over_a_staged_one(real_client, real_engine, migration_env):
    from celerp.models.company import Company, User
    from celerp.services.auth import decode_access_token

    await real_client.post("/auth/register", json={
        "company_name": "Working Co", "email": OWNER_EMAIL, "name": "Owner", "password": OWNER_PASSWORD})
    async with maker(real_engine)() as s:
        user = await s.scalar(select(User).where(User.email == OWNER_EMAIL))
        working = await s.scalar(select(Company.id).where(Company.name == "Working Co"))
        staged = Company(name="Staged Co", slug=f"staged-{uuid.uuid4().hex[:8]}", settings={},
                         is_active=False, is_migration_staged=True)
        s.add(staged)
        await s.commit()
    # The staged link sorts first by id, so only an explicit preference picks the working company.
    await _link(real_engine, user.id, staged.id, link_id=uuid.UUID(int=0))
    r = await real_client.post("/auth/login", json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    assert decode_access_token(r.json()["access_token"])["company_id"] == str(working)

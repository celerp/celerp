# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A sign-in with the old password never outlives a password change made while it runs.

Each test signs in on one connection while the password is reset or changed on another,
in a fixed order: the sign-in has read the account, then the change commits, then the
sign-in goes on. The sign-in must be refused, issue nothing, and leave the new password
in place. Real Postgres, a database of the test's own.
"""
from __future__ import annotations

import uuid

import bcrypt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp import credentials
from celerp.models.accounting import UserCompany
from celerp.models.auth import SessionRegistry, UserAuthState
from celerp.models.company import Company, User
from celerp.routers import auth as auth_router
from celerp.services.auth import hash_password, verify_password
from celerp.services.password_reset import reset_password, set_password

pytestmark = pytest.mark.asyncio

OLD = "old-password-1"
NEW = "new-password-2"


@pytest_asyncio.fixture
async def factory(committed_engine):
    return async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(factory, auth_hash: str) -> dict:
    async with factory() as s:
        suffix = uuid.uuid4().hex[:8]
        member = User(email=f"member-{suffix}@example.com", name="Member", auth_hash=auth_hash)
        owner = User(email=f"owner-{suffix}@example.com", name="Owner", auth_hash=hash_password(OLD))
        company = Company(name="Race Co", slug=f"race-{suffix}", settings={})
        s.add_all([member, owner])
        await s.flush()
        s.add(company)
        await s.flush()
        s.add_all([
            UserCompany(user_id=member.id, company_id=company.id, role="manager", is_active=True),
            UserCompany(user_id=owner.id, company_id=company.id, role="owner", is_active=True),
            UserAuthState(user_id=member.id, nonce=str(uuid.uuid4())),
        ])
        await s.commit()
        return {"user_id": member.id, "email": member.email, "owner_id": owner.id, "company_id": company.id}


async def _emailed(s, user, seeded):
    await reset_password(s, user, NEW, method="email_code")


async def _owner(s, user, seeded):
    await reset_password(s, user, NEW, method="owner", by_user=await s.get(User, seeded["owner_id"]))


async def _terminal(s, user, seeded):
    await reset_password(s, user, NEW, method="terminal", os_user="someone")


async def _change(s, user, seeded):
    await set_password(s, user, NEW)


async def _sign_in_while_changed(factory, seeded, monkeypatch, change) -> HTTPException | None:
    """Sign in with OLD; the change commits right after the sign-in has read the account."""
    loaded = auth_router.user_by_email

    async def read_then_change(session, email):
        user = await loaded(session, email)
        async with factory() as other:
            await change(other, await other.get(User, seeded["user_id"]), seeded)
        return user

    monkeypatch.setattr(auth_router, "user_by_email", read_then_change)
    async with factory() as s:
        try:
            user = await auth_router.authenticate(s, seeded["email"], OLD, f"race-{uuid.uuid4().hex}")
            await credentials.issue_token_pair(s, user=user, company_id=seeded["company_id"],
                                               expected_snonce=None)
        except HTTPException as exc:
            return exc
    return None


async def _assert_new_password_stands(factory, seeded):
    async with factory() as s:
        stored = (await s.execute(select(User.auth_hash).where(User.id == seeded["user_id"]))).scalar_one()
        sessions = (await s.execute(
            select(SessionRegistry.jti).where(SessionRegistry.user_id == seeded["user_id"]))).all()
    assert verify_password(NEW, stored)
    assert not verify_password(OLD, stored)
    assert sessions == []


@pytest.mark.parametrize("change", [_emailed, _owner, _terminal, _change],
                         ids=["emailed-code", "owner", "terminal", "own-change"])
async def test_old_password_sign_in_is_refused_after_a_concurrent_change(factory, monkeypatch, change):
    seeded = await _seed(factory, hash_password(OLD))
    refused = await _sign_in_while_changed(factory, seeded, monkeypatch, change)
    assert refused is not None and refused.status_code == 401
    await _assert_new_password_stands(factory, seeded)


async def test_upgrading_an_older_hash_never_overwrites_a_concurrent_reset(factory, monkeypatch):
    """An account restored with an older hash is upgraded at sign-in; a reset that lands
    first keeps its new password."""
    seeded = await _seed(factory, bcrypt.hashpw(OLD.encode(), bcrypt.gensalt(rounds=4)).decode())
    refused = await _sign_in_while_changed(factory, seeded, monkeypatch, _emailed)
    assert refused is not None and refused.status_code == 401
    await _assert_new_password_stands(factory, seeded)


async def test_sign_in_without_a_change_still_succeeds_and_upgrades_an_older_hash(factory):
    """Neighbour: with nothing racing, the old scheme signs in and is upgraded."""
    seeded = await _seed(factory, bcrypt.hashpw(OLD.encode(), bcrypt.gensalt(rounds=4)).decode())
    async with factory() as s:
        user = await auth_router.authenticate(s, seeded["email"], OLD, f"race-{uuid.uuid4().hex}")
        tokens = await credentials.issue_token_pair(s, user=user, company_id=seeded["company_id"],
                                                    expected_snonce=None)
    assert tokens["access_token"]
    async with factory() as s:
        stored = (await s.execute(select(User.auth_hash).where(User.id == seeded["user_id"]))).scalar_one()
    assert verify_password(OLD, stored) and not stored.startswith("$2")

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Accounts restored from an older backup carry a bcrypt password hash.

Sign-in accepts the legacy hash and replaces it with the current scheme on the first
successful sign-in. A wrong password, a bcrypt hash or an unreadable hash never turns
sign-in or change-password into a 500.
"""
from __future__ import annotations

import bcrypt
import pytest
from sqlalchemy import select

_PW = "legacy-pass-1"


def _bcrypt(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _register(client, email="owner@example.com"):
    r = await client.post("/auth/register", json={
        "company_name": "HashCo", "email": email, "name": "Owner", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _set_hash(session, email, value):
    from celerp.models.company import User
    user = (await session.execute(select(User).where(User.email == email))).scalar_one()
    user.auth_hash = value
    await session.commit()


async def _hash(session, email):
    from celerp.models.company import User
    session.expire_all()
    return (await session.execute(select(User.auth_hash).where(User.email == email))).scalar_one()


def test_verify_password_reads_bcrypt():
    from celerp.services.auth import verify_password
    h = _bcrypt(_PW)
    assert verify_password(_PW, h) is True
    assert verify_password("wrong-pass-1", h) is False


def test_verify_password_unreadable_hash_is_false():
    from celerp.services.auth import verify_password
    assert verify_password(_PW, "not-a-hash") is False


def test_current_scheme_still_verifies():
    """Neighbour: the current scheme is unchanged and never flagged for rehash."""
    from celerp.services.auth import hash_password, password_needs_rehash, verify_password
    h = hash_password(_PW)
    assert h.startswith("$pbkdf2-sha256$")
    assert verify_password(_PW, h) and not verify_password("wrong-pass-1", h)
    assert password_needs_rehash(h) is False
    assert password_needs_rehash(_bcrypt(_PW)) is True


@pytest.mark.asyncio
async def test_login_bcrypt_wrong_password_is_401(client, session):
    await _register(client)
    await _set_hash(session, "owner@example.com", _bcrypt(_PW))
    r = await client.post("/auth/login", json={"email": "owner@example.com", "password": "wrong-pass-1"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_login_bcrypt_right_password_signs_in_and_rehashes(client, session):
    await _register(client)
    await _set_hash(session, "owner@example.com", _bcrypt(_PW))
    r = await client.post("/auth/login", json={"email": "owner@example.com", "password": _PW})
    assert r.status_code == 200, r.text
    assert r.json().get("access_token")
    new = await _hash(session, "owner@example.com")
    assert new.startswith("$pbkdf2-sha256$")
    # The rehashed password still signs in.
    r2 = await client.post("/auth/login", json={"email": "owner@example.com", "password": _PW})
    assert r2.status_code == 200, r2.text


@pytest.mark.asyncio
async def test_login_unreadable_hash_is_401(client, session):
    await _register(client)
    await _set_hash(session, "owner@example.com", "not-a-hash")
    r = await client.post("/auth/login", json={"email": "owner@example.com", "password": _PW})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_change_password_bcrypt_user(client, session):
    token = await _register(client)
    await _set_hash(session, "owner@example.com", _bcrypt(_PW))
    hdr = {"Authorization": f"Bearer {token}"}
    r = await client.post("/auth/change-password", headers=hdr,
                          json={"current_password": "wrong-pass-1", "new_password": "N3w-password-xyz"})
    assert r.status_code == 400, r.text
    r = await client.post("/auth/change-password", headers=hdr,
                          json={"current_password": _PW, "new_password": "N3w-password-xyz"})
    assert r.status_code == 200, r.text
    assert (await _hash(session, "owner@example.com")).startswith("$pbkdf2-sha256$")

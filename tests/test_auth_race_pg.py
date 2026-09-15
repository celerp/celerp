# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Postgres row-lock concurrency proofs for issuance vs revocation (F2).

The crown-jewel invariant: a continuation issuance authenticated on generation
N0 can NEVER mint a usable token onto a generation N1 created by a concurrent
revoke, and no post-revocation JTI or token survives.

These tests target the POST-FIX API shape and are RED at merge-base ea480c48:

- ``issue_token_pair`` has no ``expected_snonce`` keyword yet (TypeError now).
- ``invalidate_sessions`` / ``invalidate_all_sessions`` take no ``SELECT ...
  FOR UPDATE`` row lock yet, so nothing serializes the read-check-mint window.

They go GREEN once Phase A lands: a ``FOR UPDATE`` lock on the ``UserAuthState``
row in both issuance and revocation, and a continuation issuance that presents
its authenticated ``expected_snonce`` and is rejected (neutral 401) before
minting or registering a JTI when that snonce no longer matches the locked row.

Real cross-connection contention is mandatory (a single-connection fixture
cannot show ``FOR UPDATE``), so every test drives two AsyncSessions on two
connections against its own committed Postgres database, interleaved
deterministically with ``asyncio.Event`` (no sleeps).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Importing the models package registers every table on Base.metadata so
# create_all builds the full FK graph (users, companies, user_companies,
# user_auth_state, session_registry).
import celerp.models  # noqa: F401
from celerp.models.accounting import UserCompany
from celerp.models.auth import SessionRegistry, UserAuthState
from celerp.models.base import Base
from celerp.models.company import Company, User
from celerp.services import auth as auth_svc
from celerp.services import session_tracker

pytestmark = pytest.mark.asyncio

DATABASE_URL = os.environ["DATABASE_URL"]


# ---------------------------------------------------------------------------
# Committed-database fixtures (own engine, real cross-connection contention)
# ---------------------------------------------------------------------------


def _admin_conn(dbname: str):
    """A psycopg2 autocommit connection to an existing database on the same server,
    for issuing CREATE/DROP DATABASE against a different database."""
    from urllib.parse import urlsplit
    import psycopg2

    parts = urlsplit(DATABASE_URL.replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=dbname)
    conn.autocommit = True
    return conn


@pytest_asyncio.fixture
async def engine():
    # These tests need REAL cross-connection contention, so they COMMIT seed rows
    # (users, companies, auth state) to disk rather than rolling back like the
    # shared `session` fixture. Running them against the ambient per-xdist-worker
    # database would leak that committed state into every other test that shares
    # the worker DB - e.g. an empty-DB bootstrap or inventory assertion later on
    # the same worker. Each race test therefore gets its OWN database, created here
    # and dropped on teardown, so nothing leaks.
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(DATABASE_URL.replace("+asyncpg", ""))
    base_db = parts.path.lstrip("/") or "postgres"
    race_db = f"{base_db}_race_{uuid.uuid4().hex[:8]}"

    conn = _admin_conn(base_db)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{race_db}"')
    finally:
        conn.close()

    race_url = urlunsplit(parts._replace(path=f"/{race_db}")).replace(
        "postgresql://", "postgresql+asyncpg://")
    eng = create_async_engine(race_url, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()
        admin = _admin_conn(base_db)
        try:
            # dispose() closes the pool, but a session a test left open can outlive
            # it, and a freshly-created database also briefly attracts an autovacuum
            # worker. We do NOT use DROP DATABASE ... WITH (FORCE): FORCE terminates
            # every backend including that autovacuum worker, which is owned by the
            # bootstrap superuser and cannot be terminated by the unprivileged test
            # role. Instead we terminate only the client backends this role itself
            # owns (same-user termination is always permitted) and let the plain
            # DROP handle the rest.
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid() "
                    "AND usename = current_user",
                    (race_db,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{race_db}"')
        finally:
            admin.close()


@pytest_asyncio.fixture
async def sessionmaker(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _fresh_session(engine) -> AsyncSession:
    """A brand-new session on its OWN connection (its own transaction)."""
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return factory()


@pytest_asyncio.fixture
async def seeded(engine, sessionmaker):
    """Seed one user, one company (plus a second for switch/create paths), an
    active membership on each, and the user's UserAuthState with nonce N0.

    Truncates first so a reused database from a prior run starts clean, and busts
    the in-process nonce cache so N0 is read from the committed row, not a stale
    cache entry left by another test.
    """
    async with sessionmaker() as s:
        # Clean slate: order respects FKs.
        for model in (SessionRegistry, UserAuthState, UserCompany, User, Company):
            await s.execute(model.__table__.delete())
        await s.commit()

        n0 = str(uuid.uuid4())
        suffix = uuid.uuid4().hex[:8]
        user = User(email=f"race-{suffix}@example.com", name="Race User", auth_hash=None)
        company_a = Company(name="Company A", slug=f"company-a-{suffix}", settings={})
        company_b = Company(name="Company B", slug=f"company-b-{suffix}", settings={})
        s.add_all([user, company_a, company_b])
        await s.flush()
        s.add_all([
            UserCompany(user_id=user.id, company_id=company_a.id, role="owner", is_active=True),
            UserCompany(user_id=user.id, company_id=company_b.id, role="owner", is_active=True),
            UserAuthState(user_id=user.id, nonce=n0),
        ])
        await s.commit()
        ids = {
            "user_id": user.id,
            "company_a_id": company_a.id,
            "company_b_id": company_b.id,
            "n0": n0,
        }

    # The committed nonce is authoritative; drop any cache so N0 is read fresh.
    session_tracker._nonce_cache_bust(str(ids["user_id"]))
    return ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load(session: AsyncSession, model, pk):
    return await session.get(model, pk)


async def _current_nonce(sessionmaker, user_id) -> str:
    async with sessionmaker() as s:
        row = await s.get(UserAuthState, user_id)
        return row.nonce


async def _registry_jtis(sessionmaker, user_id) -> set[str]:
    async with sessionmaker() as s:
        rows = await s.execute(
            select(SessionRegistry.jti).where(SessionRegistry.user_id == user_id)
        )
        return {r[0] for r in rows}


async def _token_is_usable(sessionmaker, access_token: str) -> bool:
    """True iff validate_access_token accepts the token against committed state."""
    from fastapi import HTTPException

    async with sessionmaker() as s:
        session_tracker._nonce_cache_bust_all()
        try:
            await auth_svc.validate_access_token(s, access_token)
            return True
        except HTTPException:
            return False


def _is_neutral_401(exc: BaseException) -> bool:
    from fastapi import HTTPException

    return isinstance(exc, HTTPException) and exc.status_code == 401


# ---------------------------------------------------------------------------
# Deterministic interleave primitive
# ---------------------------------------------------------------------------


async def _run_revoke_first(engine, seeded, *, issuance_role="owner", company_id_key="company_a_id"):
    """Revoke commits FIRST, then the continuation issuance (pinned to N0) runs.

    Returns (issuance_result, issuance_exc): exactly one is non-None. Post-fix,
    the FOR-UPDATE-locked row shows N1 and the N0-pinned continuation is rejected
    with a neutral 401 before minting or registering a JTI.
    """
    user_id = seeded["user_id"]
    company_id = seeded[company_id_key]
    n0 = seeded["n0"]

    revoke_done = asyncio.Event()

    async def revoker():
        s = await _fresh_session(engine)
        try:
            await session_tracker.invalidate_sessions(
                s, str(user_id), expected_snonce=n0
            )
        finally:
            await s.close()
        revoke_done.set()

    async def continuation():
        # Wait until the revoke has committed N1, then attempt the N0 issuance.
        await revoke_done.wait()
        s = await _fresh_session(engine)
        try:
            user = await s.get(User, user_id)
            company = await s.get(Company, company_id)
            try:
                result = await auth_svc.issue_token_pair(
                    s,
                    user=user,
                    company=company,
                    role=issuance_role,
                    expected_snonce=n0,
                )
                return result, None
            except BaseException as exc:  # noqa: BLE001 - assert on it in the test
                return None, exc
        finally:
            await s.close()

    revoke_task = asyncio.create_task(revoker())
    result, exc = await continuation()
    await revoke_task
    return result, exc


# ---------------------------------------------------------------------------
# Tests 16-19: continuation on N0 vs concurrent logout->N1
# ---------------------------------------------------------------------------


async def test_16_refresh_continuation_loses_to_concurrent_logout(engine, seeded, sessionmaker):
    """16. refresh(N0) vs concurrent logout->N1: continuation must fail; no valid
    post-logout token or JTI survives."""
    result, exc = await _run_revoke_first(engine, seeded)

    assert result is None, "continuation minted onto a revoked generation"
    assert _is_neutral_401(exc), f"expected neutral 401, got {exc!r}"

    # No JTI was registered by the losing continuation.
    assert await _registry_jtis(sessionmaker, seeded["user_id"]) == set()
    # The nonce advanced to N1.
    assert await _current_nonce(sessionmaker, seeded["user_id"]) != seeded["n0"]


async def test_17_sliding_bearer_refresh_loses_to_concurrent_logout(engine, seeded, sessionmaker):
    """17. sliding-bearer refresh (reused JTI, expected_snonce=N0) vs logout: same
    invariant. Even reusing an existing JTI, the N0 continuation cannot mint."""
    user_id = seeded["user_id"]
    company_id = seeded["company_a_id"]
    n0 = seeded["n0"]

    reused_jti = str(uuid.uuid4())
    revoke_done = asyncio.Event()

    async def revoker():
        s = await _fresh_session(engine)
        try:
            await session_tracker.invalidate_sessions(s, str(user_id), expected_snonce=n0)
        finally:
            await s.close()
        revoke_done.set()

    async def continuation():
        await revoke_done.wait()
        s = await _fresh_session(engine)
        try:
            user = await s.get(User, user_id)
            company = await s.get(Company, company_id)
            try:
                return await auth_svc.issue_token_pair(
                    s, user=user, company=company, role="owner",
                    jti=reused_jti, expected_snonce=n0,
                ), None
            except BaseException as exc:  # noqa: BLE001
                return None, exc
        finally:
            await s.close()

    revoke_task = asyncio.create_task(revoker())
    result, exc = await continuation()
    await revoke_task

    assert result is None, "sliding-bearer continuation minted onto revoked generation"
    assert _is_neutral_401(exc), f"expected neutral 401, got {exc!r}"
    assert reused_jti not in await _registry_jtis(sessionmaker, user_id)


async def test_18_switch_company_continuation_loses_to_concurrent_logout(engine, seeded, sessionmaker):
    """18. switch-company vs logout: a request authenticated on N0 cannot mint a
    token for company B on the new generation N1."""
    result, exc = await _run_revoke_first(
        engine, seeded, company_id_key="company_b_id"
    )

    assert result is None, "switch-company minted onto a revoked generation"
    assert _is_neutral_401(exc), f"expected neutral 401, got {exc!r}"
    assert await _registry_jtis(sessionmaker, seeded["user_id"]) == set()


async def test_19_create_company_continuation_loses_to_concurrent_logout(engine, seeded, sessionmaker):
    """19. create-company vs logout: an owner authenticated on N0 cannot mint an
    owner token for the freshly created company on the new generation N1.

    The create-company continuation issues role="owner"; the invariant is
    identical to switch-company - the N0 snonce no longer matches the locked row."""
    result, exc = await _run_revoke_first(
        engine, seeded, issuance_role="owner", company_id_key="company_b_id"
    )

    assert result is None, "create-company minted onto a revoked generation"
    assert _is_neutral_401(exc), f"expected neutral 401, got {exc!r}"
    assert await _registry_jtis(sessionmaker, seeded["user_id"]) == set()


# ---------------------------------------------------------------------------
# Tests 20-21: commit ordering
# ---------------------------------------------------------------------------


async def test_20_issuance_first_then_logout_deletes_jti_and_rotates(engine, seeded, sessionmaker):
    """20. issuance commits FIRST -> a subsequent logout deletes its JTI and
    rotates the nonce, so that token is unusable next use."""
    user_id = seeded["user_id"]
    company_id = seeded["company_a_id"]
    n0 = seeded["n0"]

    # Issue on the current generation (N0) and commit.
    s = await _fresh_session(engine)
    try:
        user = await s.get(User, user_id)
        company = await s.get(Company, company_id)
        pair = await auth_svc.issue_token_pair(
            s, user=user, company=company, role="owner", expected_snonce=n0
        )
    finally:
        await s.close()

    access_token = pair["access_token"]
    jtis_after_issue = await _registry_jtis(sessionmaker, user_id)
    assert len(jtis_after_issue) == 1, "issuance did not register exactly one JTI"
    assert await _token_is_usable(sessionmaker, access_token), "freshly issued token rejected"

    # Now logout: it must delete that JTI and rotate the nonce.
    s2 = await _fresh_session(engine)
    try:
        await session_tracker.invalidate_sessions(s2, str(user_id), expected_snonce=n0)
    finally:
        await s2.close()

    assert await _registry_jtis(sessionmaker, user_id) == set(), "logout left the JTI behind"
    assert await _current_nonce(sessionmaker, user_id) != n0, "logout did not rotate the nonce"
    assert not await _token_is_usable(sessionmaker, access_token), "revoked token still usable"


async def test_21_logout_first_then_issuance_rejected_before_registration(engine, seeded, sessionmaker):
    """21. logout commits FIRST -> the N0 continuation issuance fails before any
    JTI registration; no new SessionRegistry row appears."""
    result, exc = await _run_revoke_first(engine, seeded)

    assert result is None, "issuance succeeded after a committed logout"
    assert _is_neutral_401(exc), f"expected neutral 401, got {exc!r}"
    # The defining assertion for test 21: no registry row was created.
    assert await _registry_jtis(sessionmaker, seeded["user_id"]) == set(), \
        "a JTI was registered despite the pre-committed logout"


# ---------------------------------------------------------------------------
# Test 22: a fresh login after revocation may mint on the current generation
# ---------------------------------------------------------------------------


async def test_22_fresh_login_after_revocation_mints_on_current_generation(engine, seeded, sessionmaker):
    """22. a fresh password login after revocation IS allowed to mint on the
    current generation (the expected_snonce=None path performs no continuation
    check - it always issues on whatever the locked row currently holds)."""
    user_id = seeded["user_id"]
    company_id = seeded["company_a_id"]
    n0 = seeded["n0"]

    # Revoke: rotates N0 -> N1.
    s = await _fresh_session(engine)
    try:
        await session_tracker.invalidate_sessions(s, str(user_id), expected_snonce=n0)
    finally:
        await s.close()

    n1 = await _current_nonce(sessionmaker, user_id)
    assert n1 != n0, "revocation did not rotate the nonce"

    # A fresh login is NOT a continuation: expected_snonce=None, so it mints on N1.
    s2 = await _fresh_session(engine)
    try:
        user = await s2.get(User, user_id)
        company = await s2.get(Company, company_id)
        pair = await auth_svc.issue_token_pair(
            s2, user=user, company=company, role="owner", expected_snonce=None
        )
    finally:
        await s2.close()

    assert pair["access_token"], "fresh login failed to mint a token"
    assert await _token_is_usable(sessionmaker, pair["access_token"]), \
        "freshly minted post-revocation token is not usable on the current generation"
    assert len(await _registry_jtis(sessionmaker, user_id)) == 1, \
        "fresh login did not register its JTI"

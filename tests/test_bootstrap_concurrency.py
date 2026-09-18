# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""First-admin bootstrap is serialized and transactional.

Two things must hold for the one-time first-admin registration:

* Concurrency: two registrations racing on a fresh install can never both win.
  The route takes a transaction-scoped advisory lock before it reads user state,
  so the second caller blocks until the first commits and then sees the existing
  owner and is refused. Exactly one company, one owner user, and one owner
  membership survive.
* Atomicity: core bootstrap rows and direct demo/self-contact seeding commit
  through the central token issuer as a single transaction. Any failure before
  that commit rolls back every core/direct-seed row and returns a generic error.
  Module lifecycle hooks retain their established best-effort policy. A failure
  in setup-code cleanup after commit still returns the token pair and cannot
  permit a second registration.

The concurrency proofs use the real production advisory lock, production request
timeouts, and independent Postgres sessions - never two requests plus sleeps
hoping they collide.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from test_helpers import DATABASE_URL


# ── Real-concurrency proof (independent Postgres sessions) ────────────────────


@pytest_asyncio.fixture
async def real_engine(_db_engine):
    """A dedicated NullPool engine on the worker DB, so each session opens its own
    real backend connection (the transaction-rollback `session` fixture shares one
    connection and cannot model concurrent backends). Depends on `_db_engine` so
    the schema is already created. Truncates the bootstrap tables before and after
    so the race starts from a genuine first-install state."""
    # Match the production request connection bounds. The bootstrap lock is not
    # timeout-exempt: ordinary contention should serialize, while an abnormal
    # holder must fail closed instead of pinning a request connection forever.
    engine = create_async_engine(
        DATABASE_URL,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "lock_timeout": "3000",
                "statement_timeout": "30000",
            }
        },
    )

    async def _truncate():
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))

    await _truncate()
    try:
        yield engine
    finally:
        await _truncate()
        await engine.dispose()


async def _count(engine, table: str) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


async def _wait_for_advisory_wait(engine, task) -> None:
    """Prove *task* reached the bootstrap lock before the 3s production timeout."""
    for _ in range(40):
        await asyncio.sleep(0.05)
        async with engine.connect() as probe:
            waiting = (await probe.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            ))).scalar_one()
        if waiting >= 1:
            return
        if task.done():
            break
    raise AssertionError("registration never blocked on the bootstrap advisory lock")


async def _assert_no_bootstrap_state(session) -> None:
    """No core, direct-seed, or auth-session row may survive a failed bootstrap."""
    for table in (
        "session_registry",
        "user_auth_state",
        "ledger",
        "projections",
        "locations",
        "user_companies",
        "users",
        "companies",
    ):
        count = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        assert count == 0, f"{table} retained {count} row(s) after bootstrap rollback"


@pytest.mark.asyncio
async def test_bootstrap_race_serializes_to_single_owner(real_engine):
    """Session A holds the production bootstrap advisory lock; a real registration
    in session B blocks before the user-state check, and only wakes to a 403 once A
    has committed the first (and only) bootstrap."""
    from fastapi import HTTPException

    from celerp.routers.auth import register, RegisterRequest, _BOOTSTRAP_LOCK_KEY
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User

    maker = lambda: AsyncSession(bind=real_engine, expire_on_commit=False)
    session_a = maker()
    session_b = maker()

    try:
        # A takes the SAME transaction-scoped advisory lock the route uses.
        await session_a.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY})

        # B starts a real registration; it must block on the lock before it can
        # read user state or create anything.
        payload_b = RegisterRequest(
            company_name="RacerB", email="b@example.com", name="Owner B", password="validpass1"
        )
        task_b = asyncio.create_task(register(payload_b, session=session_b))

        # Prove B is genuinely blocked on the advisory lock (not merely slow)
        # and reaches it well inside the production 3s lock timeout.
        await _wait_for_advisory_wait(real_engine, task_b)
        assert not task_b.done(), "session B proceeded past the lock while A held it"
        assert await _count(real_engine, "users") == 0, "a user existed before A committed"

        # A completes the first bootstrap and commits, releasing the lock.
        company = Company(id=uuid.uuid4(), name="RacerA", slug=f"racera-{uuid.uuid4().hex[:8]}",
                          settings={"fiscal_year_start": "01-01"})
        owner = User(id=uuid.uuid4(), email="a@example.com", name="Owner A",
                     auth_hash="x", api_key=None, is_active=True)
        session_a.add(company)
        session_a.add(owner)
        await session_a.flush()
        session_a.add(UserCompany(id=uuid.uuid4(), user_id=owner.id, company_id=company.id, role="owner"))
        await session_a.commit()

        # B wakes, sees the existing owner, and is refused.
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(task_b, timeout=15)
        assert exc.value.status_code == 403
    finally:
        if not task_b.done():
            task_b.cancel()
        await session_a.rollback()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()

    # Exactly one bootstrap survives.
    assert await _count(real_engine, "companies") == 1
    assert await _count(real_engine, "users") == 1
    async with real_engine.connect() as conn:
        owners = (await conn.execute(text(
            "SELECT count(*) FROM user_companies WHERE role = 'owner'"
        ))).scalar_one()
    assert owners == 1


@pytest.mark.asyncio
async def test_bootstrapped_register_rejects_before_waiting_on_lock(real_engine):
    """Once an owner exists, public registration never queues on the bootstrap lock."""
    from fastapi import HTTPException

    from celerp.routers.auth import register, RegisterRequest, _BOOTSTRAP_LOCK_KEY
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User

    maker = lambda: AsyncSession(bind=real_engine, expire_on_commit=False)
    session_a = maker()
    session_b = maker()
    try:
        company = Company(
            id=uuid.uuid4(), name="ExistingCo", slug=f"existing-{uuid.uuid4().hex[:8]}",
            settings={"fiscal_year_start": "01-01"},
        )
        owner = User(
            id=uuid.uuid4(), email="existing@example.com", name="Existing Owner",
            auth_hash="x", api_key=None, is_active=True,
        )
        session_a.add_all([company, owner])
        await session_a.flush()
        session_a.add(UserCompany(
            id=uuid.uuid4(), user_id=owner.id, company_id=company.id, role="owner"
        ))
        await session_a.commit()

        # Hold the lock after bootstrap. A registration that unnecessarily joins
        # the queue would block here until production lock_timeout; the fast path
        # must instead return the established 403 immediately.
        await session_a.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY}
        )
        payload = RegisterRequest(
            company_name="ShouldNotExist", email="later@example.com",
            name="Later Owner", password="validpass1",
        )
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(register(payload, session=session_b), timeout=1.0)
        assert exc.value.status_code == 403

        async with real_engine.connect() as probe:
            waiting = (await probe.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            ))).scalar_one()
        assert waiting == 0, "bootstrapped registration attempted to join the lock queue"
    finally:
        await session_a.rollback()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()


@pytest.mark.asyncio
async def test_bootstrap_lock_timeout_fails_closed_without_partial_state(real_engine):
    """A holder that outlives production lock_timeout makes the waiter fail closed.

    The failed waiter must not create any bootstrap state while it is unable to
    acquire the serialization lock.
    """
    from fastapi import HTTPException

    from celerp.routers.auth import register, RegisterRequest, _BOOTSTRAP_LOCK_KEY

    maker = lambda: AsyncSession(bind=real_engine, expire_on_commit=False)
    session_a = maker()
    session_b = maker()
    try:
        await session_a.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY})
        payload = RegisterRequest(
            company_name="TimeoutCo", email="timeout@example.com", name="Owner", password="validpass1"
        )

        with pytest.raises(HTTPException) as exc:
            await register(payload, session=session_b)
        assert exc.value.status_code == 500
        assert exc.value.detail == "Registration failed"
        for table in ("user_companies", "users", "companies"):
            assert await _count(real_engine, table) == 0
    finally:
        await session_a.rollback()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()


@pytest.mark.asyncio
async def test_wrong_setup_code_is_rejected_before_bootstrap_lock(real_engine):
    """An invalid setup code never joins the lock queue, even while it is held."""
    import hashlib
    from unittest.mock import patch

    from fastapi import HTTPException

    from celerp.routers.auth import register, RegisterRequest, _BOOTSTRAP_LOCK_KEY

    maker = lambda: AsyncSession(bind=real_engine, expire_on_commit=False)
    session_a = maker()
    session_b = maker()
    try:
        await session_a.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY})
        digest = hashlib.sha256(b"correct-code").hexdigest()
        payload = RegisterRequest(
            company_name="WrongCodeCo", email="wrong@example.com", name="Owner",
            password="validpass1", setup_code="wrong-code",
        )
        with patch("celerp.routers.auth._setup_code_hash", return_value=digest):
            task = asyncio.create_task(register(payload, session=session_b))
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=1.0)
        assert exc.value.status_code == 403
        assert exc.value.detail == "Invalid or missing setup code."

        async with real_engine.connect() as probe:
            waiting = (await probe.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            ))).scalar_one()
        assert waiting == 0, "invalid setup code attempted to acquire the bootstrap lock"
    finally:
        await session_a.rollback()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()


@pytest.mark.asyncio
async def test_bootstrap_waiter_succeeds_after_holder_rolls_back(real_engine):
    """If the current lock holder aborts without bootstrapping, the waiter proceeds."""
    from celerp.routers.auth import register, RegisterRequest, _BOOTSTRAP_LOCK_KEY

    maker = lambda: AsyncSession(bind=real_engine, expire_on_commit=False)
    session_a = maker()
    session_b = maker()
    task_b = None
    try:
        await session_a.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY})
        payload = RegisterRequest(
            company_name="RecoveredCo", email="recovered@example.com", name="Owner",
            password="validpass1",
        )
        task_b = asyncio.create_task(register(payload, session=session_b))
        await _wait_for_advisory_wait(real_engine, task_b)

        await session_a.rollback()
        tokens = await asyncio.wait_for(task_b, timeout=10)
        assert tokens["access_token"]
        assert tokens["refresh_token"]
    finally:
        if task_b is not None and not task_b.done():
            task_b.cancel()
        await session_a.rollback()
        await session_b.rollback()
        await session_a.close()
        await session_b.close()

    assert await _count(real_engine, "companies") == 1
    assert await _count(real_engine, "users") == 1
    assert await _count(real_engine, "user_companies") == 1


# ── Atomicity / error-boundary (shared session, mocked failures) ──────────────


@pytest.mark.asyncio
async def test_seeding_failure_rolls_back_and_returns_generic_error(client, session):
    """A failure during bootstrap seeding rolls back every bootstrap row and returns
    a generic server error, never the raw exception text."""
    from unittest.mock import AsyncMock, patch

    async def _boom(*a, **k):
        raise RuntimeError("SECRET-INTERNAL-DETAIL")

    with patch("celerp.services.demo.seed_demo_items", new=AsyncMock(side_effect=_boom)):
        r = await client.post(
            "/auth/register",
            json={"company_name": "FailCo", "email": "fail@example.com", "name": "Owner", "password": "validpass1"},
        )
    assert r.status_code == 500
    assert "SECRET-INTERNAL-DETAIL" not in r.text
    await _assert_no_bootstrap_state(session)


@pytest.mark.asyncio
async def test_token_issuance_failure_rolls_back_bootstrap_rows(client, session):
    """A failure inside token issuance (before its commit) rolls back the bootstrap
    rows: the register is all-or-nothing through the single commit point."""
    from unittest.mock import AsyncMock, patch

    with patch("celerp.routers.auth.issue_token_pair", new=AsyncMock(side_effect=RuntimeError("issuer down"))):
        r = await client.post(
            "/auth/register",
            json={"company_name": "TokFail", "email": "tokfail@example.com", "name": "Owner", "password": "validpass1"},
        )
    assert r.status_code == 500
    assert "issuer down" not in r.text
    await _assert_no_bootstrap_state(session)


@pytest.mark.asyncio
async def test_setup_code_cleanup_failure_after_commit_still_issues_tokens(client, session):
    """The setup-code cleanup is best-effort AFTER the commit: if it fails the caller
    still receives a valid token pair, and a second registration is still refused."""
    import hashlib
    from unittest.mock import patch

    digest = hashlib.sha256(b"code123").hexdigest()

    with patch("celerp.routers.auth._setup_code_hash", return_value=digest), \
         patch("celerp.routers.auth._clear_setup_code", side_effect=OSError("config write failed")):
        r = await client.post(
            "/auth/register",
            json={
                "company_name": "CleanupCo", "email": "cleanup@example.com", "name": "Owner",
                "password": "validpass1", "setup_code": "code123",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["access_token"]

        # The bootstrap committed, so a second registration is locked out even
        # though cleanup raised.
        r2 = await client.post(
            "/auth/register",
            json={
                "company_name": "CleanupCo2", "email": "cleanup2@example.com", "name": "Owner2",
                "password": "validpass1", "setup_code": "code123",
            },
        )
    assert r2.status_code == 403

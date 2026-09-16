# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Database-backed session registry.

Replaces the former in-process dict + file persistence approach.  Each worker
process reads/writes the same database tables, so auth state is consistent
across Uvicorn worker processes.

Session registry
----------------
One row per active access token (JTI).  Expired rows are cleaned up hourly
by a lifespan-managed background task that uses a Postgres advisory lock in
production so only one worker runs the cleanup at a time.

Per-user nonce
--------------
Each user has a nonce stored in ``user_auth_state``.  Both access and refresh
tokens embed it at issuance (``snonce`` claim).  ``validate_access_token``
rejects any token whose snonce doesn't match the current DB value - this
invalidates all previously issued access AND refresh tokens for that user
immediately when ``invalidate_sessions`` is called, regardless of expiry.

Per-user (not global) nonce means logout/force-login only affects the evicted
user; other users remain logged in.

Eviction IP
-----------
``invalidate_sessions(evicting_ip=...)`` stores the IP so the evicted user sees
a meaningful message on their next 401 redirect.  ``pop_evicted_by_ip`` reads
and clears it in one atomic operation.
"""
from __future__ import annotations

import uuid as _uuid_mod
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import SessionLocal
from celerp.models.auth import SessionRegistry, UserAuthState


# ---------------------------------------------------------------------------
# Retired nonce-cache compatibility seams
# ---------------------------------------------------------------------------
# Postgres is the sole nonce authority. A process-local cache can be repopulated
# with an old value after a concurrent revocation commits, temporarily reviving
# a revoked credential and making revocation inconsistent across workers. Keep
# these helper names temporarily for current callers/tests, but never store or
# return authentication state from them.
# ---------------------------------------------------------------------------


def _nonce_cache_set(user_id: str, nonce: str) -> None:
    return None


def _nonce_cache_get(user_id: str) -> str | None:
    return None


def _nonce_cache_bust(user_id: str) -> None:
    return None


def _nonce_cache_bust_all() -> None:
    return None


def get_nonce_from_cache(user_id: str) -> str | None:
    """Always miss so session-watch falls through to the authoritative DB row."""
    return None


# ---------------------------------------------------------------------------
# Public API  (all async, take an AsyncSession)
# ---------------------------------------------------------------------------

async def lock_auth_state(session: AsyncSession, user_id: str) -> UserAuthState:
    """Return the ``UserAuthState`` row for *user_id* under a ``SELECT ... FOR
    UPDATE`` row lock, creating it (fresh nonce) if absent.

    This is the serialization point shared by issuance and revocation: while a
    caller holds the lock, no other transaction can read-then-write the same
    user's nonce, which is what closes the issue-vs-revoke TOCTOU window (F2).
    The row is created-and-flushed (never committed) when absent so a
    brand-new user's first issuance still holds a lock the concurrent path
    blocks on.  The caller owns the surrounding transaction and its commit.
    """
    uid = _uuid_mod.UUID(user_id)
    # with_for_update forces the locking SELECT; populate_existing is also
    # required because this AsyncSession may already hold N0 in its identity map.
    # Refresh it from the locked row before any continuation check or mint.
    row = await session.get(
        UserAuthState, uid, with_for_update=True, populate_existing=True
    )
    if row is None:
        row = UserAuthState(user_id=uid, nonce=str(_uuid_mod.uuid4()))
        session.add(row)
        await session.flush()
    return row


async def register_token(
    session: AsyncSession, jti: str, user_id: str, expiry: datetime, *, commit: bool = True
) -> None:
    """Record a newly-issued access token, extending the stored expiry when the
    JTI is re-minted.  Sliding refresh reuses the original JTI, so its registry
    slot must slide forward to match the refreshed token's expiry; otherwise a
    continuously active session would fall out of the registry at its original
    expiry while the holder still carries a valid token.

    *commit* is False when the caller (``issue_token_pair``) commits the JTI in
    the same transaction as the locked-nonce read, so the whole issuance is one
    atomic unit under the row lock."""
    existing = await session.get(SessionRegistry, jti)
    if existing is None:
        session.add(SessionRegistry(jti=jti, user_id=_uuid_mod.UUID(user_id), expiry=expiry))
    else:
        existing.expiry = expiry
    if commit:
        await session.commit()


async def active_user_ids(session: AsyncSession) -> set[str]:
    """Return user_ids with at least one non-expired JTI registered."""
    now = datetime.now(timezone.utc)
    rows = await session.execute(
        select(SessionRegistry.user_id).where(SessionRegistry.expiry > now).distinct()
    )
    return {str(r[0]) for r in rows}


async def get_nonce(session: AsyncSession, user_id: str) -> str:
    """Return the current nonce for *user_id* directly from Postgres.

    Authentication must observe a revocation as soon as its transaction commits,
    including across workers, so this path deliberately has no in-process cache.

    Auto-creates a ``user_auth_state`` row with a fresh nonce on first call
    (new user, first login).  Returns empty string if the user no longer exists
    (e.g. after factory-reset) so callers treat it as an eviction (nonce mismatch).
    """
    uid = _uuid_mod.UUID(user_id)
    row = await session.get(UserAuthState, uid, populate_existing=True)
    if row is not None:
        return row.nonce
    # Check the user exists before creating a new auth-state row.
    # If the user was deleted (e.g. factory-reset), return "" so the caller
    # detects a nonce mismatch and treats the session as evicted — no FK insert.
    from celerp.models.company import User as _User
    user_exists = await session.get(_User, uid)
    if user_exists is None:
        return ""
    nonce = str(_uuid_mod.uuid4())
    session.add(UserAuthState(user_id=uid, nonce=nonce))
    await session.commit()
    return nonce


async def invalidate_sessions(
    session: AsyncSession,
    user_id: str,
    *,
    expected_snonce: str | None = None,
    evicting_ip: str | None = None,
) -> None:
    """Wipe all JTIs for *user_id* and rotate their nonce, under a row lock.

    Called by logout, force-login and every security-sensitive account change
    (password change/reset, admin password change, role change, membership
    state change).  After this call every existing access AND refresh token for
    this user is immediately rejected (snonce mismatch), regardless of expiry.
    Other users are unaffected.

    *expected_snonce* gates a credential-driven revocation (logout with a
    presented access/refresh token): the ``UserAuthState`` row is locked FOR
    UPDATE and, when *expected_snonce* is supplied and no longer equals the
    locked nonce, the call returns WITHOUT rotating - a stale credential can
    never revoke a newer session generation (F3).  Authoritative revocations
    (password/role/membership changes) pass ``expected_snonce=None`` and always
    rotate.
    """
    uid = _uuid_mod.UUID(user_id)
    new_nonce = str(_uuid_mod.uuid4())
    # This session may already hold N0 from access-token validation. Refresh the
    # identity-mapped row under FOR UPDATE before comparing expected_snonce, so
    # a stale credential cannot rotate a newer N1 generation.
    row = await session.get(
        UserAuthState, uid, with_for_update=True, populate_existing=True
    )
    if expected_snonce is not None and (row is None or expected_snonce != row.nonce):
        # A stale credential authenticated on an older generation (or a user
        # with no auth state) must not rotate the current one. Do nothing; the
        # request/session closing releases the row lock.
        return
    await session.execute(
        delete(SessionRegistry).where(SessionRegistry.user_id == uid)
    )
    if row is not None:
        row.nonce = new_nonce
        row.evicted_by_ip = evicting_ip
    else:
        session.add(UserAuthState(user_id=uid, nonce=new_nonce, evicted_by_ip=evicting_ip))
    await session.commit()
    _nonce_cache_bust(user_id)  # bust cache so next get_nonce reads fresh nonce


async def invalidate_all_sessions(
    session: AsyncSession,
    evicting_user_id: str,
    evicting_ip: str | None = None,
) -> None:
    """Wipe ALL JTIs globally and rotate nonces for every affected user.

    Called by login-force when a user takes over the session slot.
    - Every existing auth-state nonce is rotated, not merely users with a live
      access JTI: a user whose access JTI has expired but whose refresh token is
      still valid (a dormant session) must be rotated too (F4).
    - The force-logging user's nonce is rotated (invalidates their own old tokens).
    - Every OTHER user gets the evicting_ip stored so they see the eviction
      message on their next 401.
    - After this call, the global active_user_ids() set is empty.
    """
    evicting_uid = _uuid_mod.UUID(evicting_user_id)

    # Lock every auth-state row in a deterministic order (by user_id) so
    # concurrent force-logins acquire the rows in the same sequence and cannot
    # deadlock. This is the durable list of every v2 session (dormant included),
    # so no refresh-token registry is needed to find users to rotate.
    rows = (
        await session.execute(
            select(UserAuthState)
            .order_by(UserAuthState.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().all()

    # Wipe all JTIs.
    await session.execute(delete(SessionRegistry))

    seen: set[_uuid_mod.UUID] = set()
    for row in rows:
        row.nonce = str(_uuid_mod.uuid4())
        # Only store evicting IP for OTHER users (not the one taking over).
        row.evicted_by_ip = evicting_ip if row.user_id != evicting_uid else None
        seen.add(row.user_id)

    # Ensure the evicting user also has a rotated nonce even with no prior row.
    if evicting_uid not in seen:
        session.add(UserAuthState(user_id=evicting_uid, nonce=str(_uuid_mod.uuid4())))

    await session.commit()
    # Every nonce moved: drop the whole in-process cache.
    _nonce_cache_bust_all()


async def pop_evicted_by_ip(session: AsyncSession, user_id: str) -> str | None:
    """Return and clear the stored eviction IP for *user_id* (one-shot)."""
    uid = _uuid_mod.UUID(user_id)
    row = await session.get(UserAuthState, uid)
    if row is None:
        return None
    ip = row.evicted_by_ip
    if ip is not None:
        row.evicted_by_ip = None
        await session.commit()
    return ip


async def clear(session: AsyncSession) -> None:
    """Wipe all session_registry rows.  Test helper only - does NOT rotate nonces."""
    await session.execute(delete(SessionRegistry))
    await session.commit()


async def run_jti_cleanup_loop() -> None:
    """Background task: delete expired JTI rows hourly.

    Uses ``pg_try_advisory_xact_lock`` so that with N workers only one runs the
    cleanup each cycle; the others skip it.
    """
    import asyncio
    # SessionLocal imported at module level

    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return
        try:
            async with SessionLocal() as s:
                # Advisory lock (0x43454C5250 = b"CELERP"): only one of N workers
                # acquires it; the others skip this cleanup cycle.
                from sqlalchemy import text as _text
                result = await s.execute(_text("SELECT pg_try_advisory_xact_lock(0x43454C5250)"))
                if result.scalar():
                    now = datetime.now(timezone.utc)
                    await s.execute(delete(SessionRegistry).where(SessionRegistry.expiry < now))
                    await s.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            pass  # never crash the loop

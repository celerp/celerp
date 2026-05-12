# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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
Each user has a nonce stored in ``user_auth_state``.  Access tokens embed it
at issuance (``snonce`` claim).  ``get_current_user`` rejects any token whose
snonce doesn't match the current DB value - this invalidates all previously
issued tokens for that user immediately when ``invalidate_sessions`` is called,
regardless of expiry.

Per-user (not global) nonce means logout/force-login only affects the evicted
user; other users remain logged in.

Eviction IP
-----------
``invalidate_sessions(evicting_ip=...)`` stores the IP so the evicted user sees
a meaningful message on their next 401 redirect.  ``pop_evicted_by_ip`` reads
and clears it in one atomic operation.
"""
from __future__ import annotations

import time
import uuid as _uuid_mod
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import SessionLocal
from celerp.models.auth import SessionRegistry, UserAuthState


# ---------------------------------------------------------------------------
# In-process nonce cache (per user_id)
# ---------------------------------------------------------------------------
# Nonce only changes on logout or force-login - both paths call invalidate_sessions
# or invalidate_all_sessions which bust the cache explicitly.  A short TTL is a
# safety backstop (e.g. after a process restart the cache is cold anyway).
#
# TTL is intentionally short (60 s) so that, in the pathological case where cache
# invalidation is missed, the window is bounded.
# ---------------------------------------------------------------------------

# Short TTL so eviction propagates quickly (within one polling interval).
# session-watch polls every 10s, so 10s TTL means worst-case eviction lag ≈ 20s.
# Postgres remains authoritative - cache is evicted on every mutation.
_NONCE_TTL_S = 10  # seconds

# {user_id_str: (nonce: str, cached_at: float)}
_nonce_cache: dict[str, tuple[str, float]] = {}


def _nonce_cache_set(user_id: str, nonce: str) -> None:
    _nonce_cache[user_id] = (nonce, time.monotonic())


def _nonce_cache_get(user_id: str) -> str | None:
    entry = _nonce_cache.get(user_id)
    if entry is None:
        return None
    nonce, ts = entry
    if time.monotonic() - ts > _NONCE_TTL_S:
        del _nonce_cache[user_id]
        return None
    return nonce


def _nonce_cache_bust(user_id: str) -> None:
    _nonce_cache.pop(user_id, None)


def _nonce_cache_bust_all() -> None:
    _nonce_cache.clear()


def get_nonce_from_cache(user_id: str) -> str | None:
    """Return the cached nonce without hitting the DB.  Returns None on miss.

    Used by the session-watch SSE loop so it avoids a DB round-trip on every
    poll tick when the nonce is already known and fresh.
    """
    return _nonce_cache_get(user_id)


# ---------------------------------------------------------------------------
# Public API  (all async, take an AsyncSession)
# ---------------------------------------------------------------------------

async def register_token(
    session: AsyncSession, jti: str, user_id: str, expiry: datetime
) -> None:
    """Record a newly-issued access token.  No-op if JTI already exists."""
    existing = await session.get(SessionRegistry, jti)
    if existing is None:
        session.add(SessionRegistry(jti=jti, user_id=_uuid_mod.UUID(user_id), expiry=expiry))
        await session.commit()


async def active_user_ids(session: AsyncSession) -> set[str]:
    """Return user_ids with at least one non-expired JTI registered."""
    now = datetime.now(timezone.utc)
    rows = await session.execute(
        select(SessionRegistry.user_id).where(SessionRegistry.expiry > now).distinct()
    )
    return {str(r[0]) for r in rows}


async def get_nonce(session: AsyncSession, user_id: str) -> str:
    """Return the current nonce for *user_id*.

    Result is cached in-process for up to ``_NONCE_TTL_S`` seconds.  The cache
    is explicitly busted by ``invalidate_sessions`` and ``invalidate_all_sessions``
    so security is not weakened - the only window is TTL expiry, which is 60 s.

    Auto-creates a ``user_auth_state`` row with a fresh nonce on first call
    (new user, first login).
    """
    cached = _nonce_cache_get(user_id)
    if cached is not None:
        return cached
    uid = _uuid_mod.UUID(user_id)
    row = await session.get(UserAuthState, uid)
    if row is not None:
        _nonce_cache_set(user_id, row.nonce)
        return row.nonce
    nonce = str(_uuid_mod.uuid4())
    session.add(UserAuthState(user_id=uid, nonce=nonce))
    await session.commit()
    _nonce_cache_set(user_id, nonce)
    return nonce


async def invalidate_sessions(
    session: AsyncSession, user_id: str, evicting_ip: str | None = None
) -> None:
    """Wipe all JTIs for *user_id* and rotate their nonce.

    Called by logout and force-login.  After this call every existing access
    token for this user is immediately rejected (snonce mismatch), regardless
    of expiry.  Other users are unaffected.
    """
    uid = _uuid_mod.UUID(user_id)
    new_nonce = str(_uuid_mod.uuid4())
    await session.execute(
        delete(SessionRegistry).where(SessionRegistry.user_id == uid)
    )
    row = await session.get(UserAuthState, uid)
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
    - The force-logging user's nonce is rotated (invalidates their own old tokens).
    - Every OTHER displaced user gets the evicting_ip stored so they see the
      eviction message on their next 401.
    - After this call, the global active_user_ids() set is empty.
    """
    now = datetime.now(timezone.utc)
    # Collect all users with active JTIs before we wipe them
    active_rows = await session.execute(
        select(SessionRegistry.user_id).where(SessionRegistry.expiry > now).distinct()
    )
    active_ids: set[_uuid_mod.UUID] = {r[0] for r in active_rows}

    # Wipe all JTIs
    await session.execute(delete(SessionRegistry))

    # Rotate nonce for each previously-active user
    evicting_uid = _uuid_mod.UUID(evicting_user_id)
    for uid in active_ids:
        new_nonce = str(_uuid_mod.uuid4())
        row = await session.get(UserAuthState, uid)
        if row is not None:
            row.nonce = new_nonce
            # Only store evicting IP for OTHER users (not the one taking over)
            if uid != evicting_uid:
                row.evicted_by_ip = evicting_ip
        else:
            session.add(UserAuthState(
                user_id=uid,
                nonce=new_nonce,
                evicted_by_ip=evicting_ip if uid != evicting_uid else None,
            ))

    # Ensure the evicting user also has a rotated nonce even if they had no active JTI
    if evicting_uid not in active_ids:
        new_nonce = str(_uuid_mod.uuid4())
        row = await session.get(UserAuthState, evicting_uid)
        if row is not None:
            row.nonce = new_nonce
        else:
            session.add(UserAuthState(user_id=evicting_uid, nonce=new_nonce))

    await session.commit()
    # Bust cache for all affected users
    for uid in active_ids:
        _nonce_cache_bust(str(uid))
    if evicting_uid not in active_ids:
        _nonce_cache_bust(evicting_user_id)


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

    On Postgres, uses ``pg_try_advisory_xact_lock`` so only one of N workers
    runs the cleanup.  On SQLite (dev/test) the advisory lock call fails and
    falls through to direct deletion - this is safe because SQLite is
    single-process only.
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
                skip = False
                try:
                    # Advisory lock: 0x43454C5250 = b"CELERP" as int
                    # Only one of N workers acquires the lock; others skip cleanup.
                    from sqlalchemy import text as _text
                    result = await s.execute(_text("SELECT pg_try_advisory_xact_lock(0x43454C5250)"))
                    lock_acquired = result.scalar()
                    if lock_acquired is False:  # False = lock held by another worker; None = SQLite
                        skip = True
                except Exception:
                    pass  # SQLite: pg function unavailable - run anyway (single worker)
                if not skip:
                    now = datetime.now(timezone.utc)
                    await s.execute(delete(SessionRegistry).where(SessionRegistry.expiry < now))
                    await s.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            pass  # never crash the loop

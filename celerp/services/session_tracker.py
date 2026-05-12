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

import uuid as _uuid_mod
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.auth import SessionRegistry, UserAuthState


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

    Auto-creates a ``user_auth_state`` row with a fresh nonce on first call
    (new user, first login).
    """
    uid = _uuid_mod.UUID(user_id)
    row = await session.get(UserAuthState, uid)
    if row is not None:
        return row.nonce
    nonce = str(_uuid_mod.uuid4())
    session.add(UserAuthState(user_id=uid, nonce=nonce))
    await session.commit()
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

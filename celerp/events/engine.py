# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from celerp.events.schemas import EVENT_SCHEMA_MAP
from celerp.models.ledger import LedgerEntry
from celerp.projections.engine import ProjectionEngine


def apply_event(state: dict, event: LedgerEntry) -> dict:
    return ProjectionEngine._apply(state, event.event_type, event.data)


async def _check_period_lock(session, company_id, data: dict) -> None:
    """Reject events whose effective date falls within a locked period."""
    from celerp.models.company import Company

    company = await session.get(Company, company_id)
    if not company:
        return
    lock_date_str = (company.settings or {}).get("lock_date")
    if not lock_date_str:
        return
    try:
        lock_date = date.fromisoformat(lock_date_str)
    except (ValueError, TypeError):
        return
    # Determine the effective date of this event
    event_date_str = data.get("ts") or data.get("issue_date") or data.get("date")
    if event_date_str:
        try:
            event_date = date.fromisoformat(str(event_date_str)[:10])
        except (ValueError, TypeError):
            return  # Can't parse - don't block
    else:
        event_date = date.today()
    if event_date <= lock_date:
        raise HTTPException(
            status_code=422,
            detail=f"Period is locked through {lock_date_str}. Unlock in Settings > Accounting to modify past transactions.",
        )


async def _connector_entity_id(session, company_id, entity_type: str, idem_key: str) -> str | None:
    """The entity_id of an existing projection this connector record maps to, or None.

    Records are resolved by their stable ``idempotency_key`` (stored in projection
    state), not a freshly-minted entity_id — so a re-import updates the SAME projection
    instead of duplicating it. This includes records imported before the deterministic
    -id scheme, which stored a random-uuid entity_id: the backfill migration
    (`e4f5a6b7c8d9`) stamps ``idempotency_key`` onto those rows so they resolve here too.
    """
    row = (await session.execute(
        text("SELECT entity_id FROM projections WHERE company_id = CAST(:c AS uuid) "
             "AND entity_type = :t AND state ->> 'idempotency_key' = :k LIMIT 1"),
        {"c": str(company_id), "t": entity_type, "k": idem_key},
    )).first()
    return row[0] if row else None


async def connector_upsert(
    session, *, company_id, entity_type: str, event_type: str, idem_key: str, data: dict
) -> str:
    """Create-or-update a projection from a connector payload.

    Returns "created" (new projection), "updated" (existing projection, changed
    content), or "noop" (this exact content was already applied).

    ``idem_key`` (the stable platform id) is stored in projection state so a re-import
    resolves the SAME projection; the event's idempotency key varies with the content,
    so an unchanged re-import dedups (no-op) while a changed one updates.
    """
    import hashlib
    import json as _json

    data = {**data, "idempotency_key": idem_key}  # stable identity in state (rebuild-safe)

    existing_id = await _connector_entity_id(session, company_id, entity_type, idem_key)
    entity_id = existing_id or f"{entity_type}:{idem_key}"

    content = _json.dumps(
        {k: v for k, v in data.items() if k != "idempotency_key"}, sort_keys=True, default=str
    )
    event_idem = f"{idem_key}:{hashlib.sha1(content.encode()).hexdigest()[:12]}"

    seen = (await session.execute(
        text("SELECT id FROM ledger WHERE company_id = CAST(:cid AS uuid) AND idempotency_key=:k"),
        {"cid": str(company_id), "k": event_idem},
    )).first()
    if seen:
        return "noop"

    await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type=entity_type,
        event_type=event_type,
        data=data,
        actor_id=None,
        location_id=None,
        source="connector",
        idempotency_key=event_idem,
        metadata_={},
    )
    return "updated" if existing_id else "created"


async def emit_event(session, **kwargs) -> LedgerEntry:
    # A backup represents a clean point in time: while one is building, pause writes
    # (reads, which never emit, are unaffected) so nothing changes mid-backup.
    from celerp.services.backup_state import is_active as _backup_active
    if _backup_active():
        raise HTTPException(status_code=503, detail="Backup in progress, try again shortly.")

    schema = EVENT_SCHEMA_MAP.get(kwargs["event_type"])
    if schema is None:
        raise ValueError(f"Unknown event_type: {kwargs['event_type']}")

    schema(**kwargs["data"])

    # Enforce period lock
    await _check_period_lock(session, kwargs.get("company_id"), kwargs.get("data", {}))

    entry = LedgerEntry(**kwargs)

    try:
        # Insert inside a SAVEPOINT so a duplicate-idempotency collision only
        # rolls back this insert — NOT the caller's whole transaction. (A bare
        # session.rollback() here would silently undo everything the caller
        # already emitted, e.g. the doc.finalized event before its auto-JE.)
        async with session.begin_nested():
            session.add(entry)
            await session.flush()
    except IntegrityError:
        # Idempotency is per-company, so dedup within this company only.
        row = (
            await session.execute(
                text("SELECT id FROM ledger WHERE company_id = CAST(:cid AS uuid) AND idempotency_key=:k"),
                {"cid": str(kwargs["company_id"]), "k": kwargs["idempotency_key"]},
            )
        ).first()
        if row is None:
            raise
        return await session.get(LedgerEntry, row[0])

    await ProjectionEngine.apply_event(session, entry)

    # Notify listeners (LISTEN/NOTIFY) that an event landed.
    try:
        await session.execute(text("SELECT pg_notify('events', :payload)"), {"payload": str(entry.id)})
    except Exception:
        # Deterministic: do not fail event emission due to notification issues.
        pass

    return entry

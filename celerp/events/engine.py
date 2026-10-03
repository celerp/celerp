# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from celerp.events.schemas import EVENT_SCHEMA_MAP
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine
from celerp.services.document_lines import assert_document_item_uniqueness
from celerp.services.business_time import business_timezone


def apply_event(state: dict, event: LedgerEntry) -> dict:
    return ProjectionEngine._apply(state, event.event_type, event.data)


STRIPE_OWNED_PAYMENT = (
    "This payment was received through Stripe, so it can only be refunded or reversed in Stripe."
)
# Every event that takes a received payment back off a document.
PAYMENT_REMOVAL_EVENTS = frozenset({"doc.payment.voided", "doc.payment.deleted", "doc.payment.refunded"})


async def stripe_payment_references(session, company_id, entity_id) -> set[str]:
    """References of the payments the Stripe intake recorded on this document.

    Stripe holds the money for these, so only Stripe can give it back. The ledger
    records which writer received each payment; the method is free text that a
    connector or a person can also set to "stripe".
    """
    rows = (await session.execute(
        select(LedgerEntry.data["reference"].as_string()).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id == entity_id,
            LedgerEntry.event_type == "doc.payment.received",
            LedgerEntry.source == "stripe",
        )
    )).scalars().all()
    return {ref for ref in rows if ref}


async def _refuse_stripe_payment_removal(session, kwargs: dict) -> None:
    """Refuse to void, delete or refund a payment Stripe holds the money for."""
    row = await session.get(Projection, (kwargs.get("company_id"), kwargs.get("entity_id")))
    if row is None or row.entity_type != "doc":
        return
    index = (kwargs.get("data") or {}).get("payment_index")
    payment = next((p for p in (row.state or {}).get("payments", []) if p.get("index") == index), None)
    if payment is None or payment.get("method") != "stripe" or not payment.get("reference"):
        return
    if payment["reference"] in await stripe_payment_references(session, kwargs["company_id"], kwargs["entity_id"]):
        raise HTTPException(status_code=422, detail=STRIPE_OWNED_PAYMENT)


async def find_event_by_idempotency(session, company_id, idempotency_key: str | None) -> LedgerEntry | None:
    """Return the event already committed for this company/key, if any.

    Idempotency belongs to the canonical ledger, not to individual callers.
    Routes use this before allocating numbers/codes or repeating secondary work;
    ``emit_event`` remains the unique-constraint race backstop.
    """
    if not idempotency_key:
        return None
    return (await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.idempotency_key == idempotency_key,
        ).limit(1)
    )).scalars().first()


def write_period_lock(company, lock_date: str | None, user_id) -> None:
    """Lock *company*'s books through the ISO *lock_date*, recorded as set by *user_id*,
    or unlock them when *lock_date* is None. The caller holds the company row and commits."""
    settings = dict(company.settings or {})
    if lock_date:
        settings["lock_date"] = lock_date
        settings["lock_date_set_by"] = str(user_id)
        settings["lock_date_set_at"] = datetime.now(timezone.utc).isoformat()
    else:
        for key in ("lock_date", "lock_date_set_by", "lock_date_set_at"):
            settings.pop(key, None)
    company.settings = settings


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
    event_date_str = data.get("ts") or data.get("issue_date") or data.get("date")
    if event_date_str:
        try:
            raw = str(event_date_str)
            if "T" not in raw:
                event_date = date.fromisoformat(raw[:10])
            else:
                instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if instant.tzinfo is None or instant.utcoffset() is None:
                    event_date = date.fromisoformat(raw[:10])
                else:
                    try:
                        zone = business_timezone((company.settings or {}).get("timezone"))
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
                    event_date = instant.astimezone(zone).date()
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"{event_date_str} is not a date. Enter it as YYYY-MM-DD.") from None
    else:
        try:
            zone = business_timezone((company.settings or {}).get("timezone"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        event_date = datetime.now(timezone.utc).astimezone(zone).date()
    if event_date <= lock_date:
        raise HTTPException(
            status_code=422,
            detail=f"Period is locked through {lock_date_str}. Unlock in Settings > Accounting to modify past transactions.",
        )


class ConnectorIdentityConflict(Exception):
    """A platform record resolves to two different Celerp records."""


async def _connector_entity_id(
    session, company_id, entity_type: str, idem_key: str,
    external_identity: tuple[str, str] | None = None,
) -> tuple[str | None, bool]:
    """The entity_id of an existing projection this connector record maps to (or None),
    and whether it was found only by ``external_identity``.

    Records are resolved by their stable ``idempotency_key`` (stored in projection
    state), not a freshly-minted entity_id — so a re-import updates the SAME projection
    instead of duplicating it. This includes records imported before the deterministic
    -id scheme, which stored a random-uuid entity_id: the backfill migration
    (`e4f5a6b7c8d9`) stamps ``idempotency_key`` onto those rows so they resolve here too.

    ``external_identity`` is ``(field, value)``: the platform id as a Celerp record
    carries it once Celerp itself created the record on the platform. A record found
    only that way is Celerp's own; one found by each route, differently, is a
    conflict and nothing is written.
    """
    import uuid as _uuid

    scope = (
        Projection.company_id == _uuid.UUID(str(company_id)),
        Projection.entity_type == entity_type,
    )
    by_key = (await session.execute(
        select(Projection.entity_id)
        .where(*scope, Projection.state["idempotency_key"].as_string() == idem_key)
        .limit(1)
    )).scalar()
    if external_identity is None:
        return by_key, False
    field, value = external_identity
    by_external = (await session.execute(
        select(Projection.entity_id)
        .where(*scope, Projection.state[field].as_string() == value)
        .limit(2)
    )).scalars().all()
    ids = {i for i in (by_key, *by_external) if i is not None}
    if len(ids) > 1:
        raise ConnectorIdentityConflict(
            f"{field} {value} belongs to more than one record: {', '.join(sorted(ids))}"
        )
    return next(iter(ids), None), by_key is None and bool(by_external)


async def connector_upsert(
    session, *, company_id, entity_type: str, event_type: str, idem_key: str, data: dict,
    external_identity: tuple[str, str] | None = None,
) -> str:
    """Create-or-update a projection from a connector payload.

    Returns "created" (new projection), "updated" (existing projection, changed
    content), or "noop" (this exact content was already applied, or the record is one
    Celerp created on the platform, found only by ``external_identity``: Celerp owns
    it, so nothing is written). Raises ConnectorIdentityConflict when
    ``external_identity`` and ``idem_key`` resolve to different projections.

    ``idem_key`` (the stable platform id) is stored in projection state so a re-import
    resolves the SAME projection; the event's idempotency key varies with the content,
    so an unchanged re-import dedups (no-op) while a changed one updates.
    """
    import hashlib
    import json as _json

    existing_id, external_only = await _connector_entity_id(
        session, company_id, entity_type, idem_key, external_identity
    )
    if external_only:
        return "noop"

    data = {**data, "idempotency_key": idem_key}  # stable identity in state (rebuild-safe)
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
        # A platform's codes are recorded as the platform holds them; a duplicate is
        # reported by the resolver and Doctor rather than failing the sync.
        preserve_external_code_conflicts=entity_type == "item",
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


def _touches_physical_codes(state: dict, event_type: str, data: dict) -> bool:
    """True when an item event can change which physical codes the item resolves by.

    That is any event setting a barcode or RFID / EPC, by data shape, or any event
    that moves an item out of a status excluded from resolution (merged back to
    available, say), probed by applying the event to the item in an excluded status.
    A probe that cannot be evaluated counts as touching, so the check still runs.
    """
    from celerp.inventory_codes import PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES
    from celerp.services.physical_codes import PHYSICAL_CODE_FIELDS

    changed = data.get("fields_changed")
    if any(
        field in data or (isinstance(changed, dict) and field in changed)
        for field in PHYSICAL_CODE_FIELDS
    ):
        return True
    # A status change on a live item can only drop its codes from resolution, never
    # add one, so only an item in an excluded status needs the probe (and the lock).
    if str(state.get("status") or "").lower() not in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES:
        return False
    try:
        probe = ProjectionEngine._apply(dict(state), event_type, data)
    except Exception:
        return True
    return str(probe.get("status") or "").lower() not in PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES


async def emit_event(
    session, *, preserve_external_code_conflicts: bool = False, **kwargs
) -> LedgerEntry:
    # A backup represents a clean point in time: while one is building, pause writes
    # (reads, which never emit, are unaffected) so nothing changes mid-backup.
    from celerp.services.backup_state import is_active as _backup_active
    if _backup_active():
        raise HTTPException(status_code=503, detail="Backup in progress, try again shortly.")

    schema = EVENT_SCHEMA_MAP.get(kwargs["event_type"])
    if schema is None:
        raise ValueError(f"Unknown event_type: {kwargs['event_type']}")

    schema(**kwargs["data"])

    # The schema validates a Pydantic-made copy, so any code canonicalization it applies
    # (rfid_epc -> trimmed upper-case, gtin -> validated form) never reaches the raw dict
    # persisted below. Apply the same normalization to the dict that is actually stored, so
    # the stored value equals what the availability check and the partial unique index
    # compare against - otherwise a case-variant identifier is stored raw and never collides.
    _normalize = getattr(schema, "normalize_for_storage", None)
    if _normalize is not None:
        _normalize(kwargs["data"])

    # Enforce period lock
    await _check_period_lock(session, kwargs.get("company_id"), kwargs.get("data", {}))

    # Enforce physical-item uniqueness on new OUTBOUND doc writes (invoice, memo).
    # Extract the post-change line set by DATA SHAPE so every doc writer (create,
    # patch, shared_import, update, conversion, import) is covered by one rule,
    # keyed on entity_type == "doc" rather than an event list. The doc-type scope
    # lives in assert_document_item_uniqueness beside the invariant; this boundary
    # only resolves the doc_type to hand it. Rebuild/replay applies events via
    # apply_event, never emit_event, so historical events are never re-validated.
    if kwargs.get("entity_type") == "doc":
        data = kwargs.get("data") or {}
        line_set = None
        if isinstance(data.get("line_items"), list):
            line_set = data["line_items"]
        elif isinstance(data.get("fields_changed"), dict):
            changed = data["fields_changed"].get("line_items")
            if isinstance(changed, dict):
                line_set = changed.get("new")
        if line_set is not None:
            # Prefer the event's own doc_type (present on doc.created and any update
            # that carries it - zero query). Otherwise resolve it from the persisted
            # projection, reading state["doc_type"] only when that projection is a doc
            # (the Projection PK is (company_id, entity_id) with no type discriminator,
            # so the entity_type check guards against a same-id non-doc projection).
            doc_type = data.get("doc_type")
            if doc_type is None:
                proj = await session.get(
                    Projection, (kwargs.get("company_id"), kwargs.get("entity_id"))
                )
                if proj is not None and proj.entity_type == "doc":
                    doc_type = (proj.state or {}).get("doc_type")
            await assert_document_item_uniqueness(
                session, kwargs.get("company_id"), doc_type, line_set
            )

    if kwargs.get("entity_type") == "doc" and kwargs.get("event_type") in PAYMENT_REMOVAL_EVENTS:
        await _refuse_stripe_payment_removal(session, kwargs)

    if kwargs.get("event_type") in {"shop.sync.enabled", "shop.sync.disabled"}:
        from celerp.connectors.ownership import lock_connector_key

        await lock_connector_key(session, "shopify")

    # Physical codes (barcode, RFID / EPC) are unique per company among the codes a write
    # INTRODUCES: the item's code set after the event is compared with its set before, so
    # a duplicate already in the data never blocks an unrelated edit. An exact replay is
    # left to the idempotency dedup below. Imports and connectors pass
    # preserve_external_code_conflicts to record the source system's codes as given;
    # the resolver then reports the ambiguity and Doctor lists it.
    previous_item_state = None
    if kwargs.get("entity_type") == "item":
        from copy import deepcopy

        from celerp.services.physical_codes import (
            assert_new_physical_codes_available,
            lock_item_code_namespace,
        )

        key = (kwargs["company_id"], kwargs["entity_id"])
        previous = await session.get(Projection, key, populate_existing=True)
        if previous is not None and previous.entity_type == "item":
            previous_item_state = deepcopy(previous.state or {})
        check_codes = _touches_physical_codes(
            previous_item_state or {}, kwargs["event_type"], kwargs["data"]
        ) and await find_event_by_idempotency(
            session, kwargs["company_id"], kwargs.get("idempotency_key")
        ) is None
        if check_codes:
            await lock_item_code_namespace(session, kwargs["company_id"])
            # Under the lock the check must see the committed row, not the copy the
            # identity map held from before another writer changed it.
            previous = await session.get(Projection, key, populate_existing=True)
            previous_item_state = (
                deepcopy(previous.state or {})
                if previous is not None and previous.entity_type == "item"
                else None
            )
        if check_codes and not preserve_external_code_conflicts:
            after = ProjectionEngine._apply(
                previous_item_state or {}, kwargs["event_type"], kwargs["data"]
            )
            await assert_new_physical_codes_available(
                session, kwargs["company_id"], kwargs["entity_id"], previous_item_state, after
            )

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
        original = await session.get(LedgerEntry, row[0])
        # Callers that must distinguish a replay from a fresh insert (e.g. to
        # reject a stale form resubmitted with edited values) read this flag
        # instead of inferring from entity ids.
        original.was_deduped = True
        return original

    await ProjectionEngine.apply_event(session, entry)

    # Durable connector work is recorded in the same transaction as the item event.
    # No network I/O occurs here; the worker re-reads current state before sending.
    if entry.entity_type == "item":
        from celerp.connectors.outbound_queue import enqueue_item_change
        await enqueue_item_change(
            session, entry, previous_state=previous_item_state
        )

    # Notify listeners (LISTEN/NOTIFY) that an event landed.
    try:
        await session.execute(text("SELECT pg_notify('events', :payload)"), {"payload": str(entry.id)})
    except Exception:
        # Deterministic: do not fail event emission due to notification issues.
        pass

    return entry

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from celerp.inventory_codes import (
    BarcodeConflictError,
    RfidEpcConflictError,
    is_barcode_unique_violation,
    is_rfid_epc_unique_violation,
)
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.projections.handlers.system import apply_system_event
from celerp.projections.retired import RETIRED

log = logging.getLogger(__name__)

# The events that bring an item into being; every other item event changes one that exists.
ITEM_BIRTHS = frozenset({"item.created", "item.snapshot"})

# The events no module owns: each one's projection is its data merged onto the record's
# state, whichever modules are enabled. Any other event is replayed only by a retired
# handler or by the projection handler of the module that owns it. A schema in the event
# catalog says an event may be written, never how a rebuild must apply it.
MERGE_EVENTS = frozenset({
    "payment_batch.recorded", "line_action.recorded",
    "scan.barcode", "scan.rfid", "scan.nfc", "scan.resolved",
    "sub.created", "sub.updated", "sub.paused", "sub.cancelled", "sub.resumed",
    "sub.generated", "sub.expired",
})

# The kernel's own events, applied whichever modules are loaded.
_KERNEL_PREFIX = "sys."


_TYPE_LABELS = {"item": "Item", "doc": "Document", "list": "List", "contact": "Contact"}


def _not_found(entity_type: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{_TYPE_LABELS.get(entity_type, 'Record')} not found")


def _resolve_module_handler(dotted: str):
    """Import and return a handler callable from a 'module.path:function' string.

    Returns None on any resolution failure so a single bad projection_handler
    slot cannot stop the engine from picking up its siblings.
    """
    from celerp.modules.slots import resolve_handler

    try:
        return resolve_handler(dotted)
    except Exception as exc:
        log.error("ProjectionEngine: cannot resolve module handler %r: %s", dotted, exc)
        return None


def _get_module_handlers() -> dict[str, object]:
    """Return prefix -> handler dict built from registered projection_handler slots.

    Called on each _apply() so newly-loaded modules are picked up without restart.
    """
    from celerp.modules.slots import get as get_slot
    handlers: dict[str, object] = {}
    for contrib in get_slot("projection_handler"):
        prefix = contrib.get("prefix")
        handler_path = contrib.get("handler")
        if not prefix or not handler_path:
            log.warning("projection_handler slot missing 'prefix' or 'handler': %r", contrib)
            continue
        fn = _resolve_module_handler(handler_path)
        if fn is not None:
            handlers[prefix] = fn
    return handlers


@dataclass(frozen=True)
class Transition:
    """What one event did to its entity, read under the row lock that applied it: the
    state before (None for a new entity) and the state after."""

    before: dict | None
    after: dict


def _merge(state: dict, _event_type: str, data: dict) -> dict:
    return {**state, **data}


def _replay_handler(event_type: str):
    """The one answer to "can this build apply this event as it is meant to": the retired
    handler, the registered projection handler that owns its prefix, or the plain merge for
    a MERGE_EVENTS event; None when nothing here can (its module is not enabled)."""
    if event_type in RETIRED:
        retired = RETIRED[event_type]
        return lambda state, _event_type, data: retired(state, data)
    if event_type.startswith(_KERNEL_PREFIX):
        return apply_system_event
    for prefix, fn in _get_module_handlers().items():
        if event_type.startswith(prefix):
            return fn
    return _merge if event_type in MERGE_EVENTS else None


class UnhandledEventsError(Exception):
    """The ledger holds events this build cannot replay as written (ProjectionEngine.unreplayable)."""

    def __init__(self, event_types: set[str]):
        from celerp.modules.loader import modules_owning_events

        self.event_types = sorted(event_types)
        super().__init__(
            "Records cannot be rebuilt while these modules are not enabled: "
            f"{', '.join(modules_owning_events(event_types))} ({', '.join(self.event_types)}). "
            "Enable them in Modules, then try again.")


class ProjectionEngine:
    @staticmethod
    def replayable(event_type: str) -> bool:
        """Whether this build can replay a historical ledger event with its own semantics."""
        return _replay_handler(event_type) is not None

    @staticmethod
    async def unreplayable(session, company_id=None) -> set[str]:
        """The ledger's event types (one company's, or every company's) this build cannot replay."""
        query = select(LedgerEntry.event_type).distinct()
        if company_id is not None:
            query = query.where(LedgerEntry.company_id == company_id)
        return {t for t in (await session.execute(query)).scalars() if not ProjectionEngine.replayable(t)}

    @staticmethod
    def _apply(state: dict, event_type: str, data: dict) -> dict:
        handler = _replay_handler(event_type)
        if handler is None:
            raise ValueError(f"No enabled module applies {event_type} events")
        return handler(state, event_type, data)

    @staticmethod
    def _next_fields(state: dict, entry: LedgerEntry, fallback_version: int) -> dict:
        next_state = ProjectionEngine._apply(state, entry.event_type, entry.data)
        now = datetime.now(timezone.utc)

        location_id = next_state.get("location_id")
        if isinstance(location_id, str):
            try:
                import uuid as _uuid

                location_id = _uuid.UUID(location_id)
            except Exception:
                location_id = None

        expires_at = next_state.get("expires_at")
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except Exception:
                expires_at = None

        return {
            "state": next_state,
            "version": entry.id or fallback_version,
            "location_id": location_id,
            "updated_at": now,
            "is_on_memo": next_state.get("is_on_memo"),
            "is_on_marketplace": next_state.get("is_on_marketplace"),
            "is_sync_to_shopify": next_state.get("is_sync_to_shopify"),
            "is_in_production": next_state.get("is_in_production"),
            "is_expired": next_state.get("is_expired"),
            "expires_at": expires_at,
            "consignment_flag": next_state.get("consignment_flag"),
        }

    @staticmethod
    async def _locked_projection(session, entry: LedgerEntry) -> Projection | None:
        # FOR UPDATE serializes concurrent events on one entity: each applier
        # bases its state on the previous committed write instead of a shared
        # stale read, where whichever commit lands last would silently drop the
        # other's fields even though the ledger holds both events. Callers often
        # hold an unlocked copy of this row in the session's identity map from a
        # validation read, and session.get would return that stale object without
        # locking or re-reading; populate_existing overwrites it with the freshly
        # locked row.
        result = await session.execute(
            select(Projection)
            .where(Projection.company_id == entry.company_id, Projection.entity_id == entry.entity_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def apply_event(session, entry: LedgerEntry) -> Transition:
        """Apply a new event. An event of one kind on a record of another kind (an item
        change on a document's ID, an item creation over a contact's) is refused as not
        found: that ID holds no record of the event's kind. Replay (rebuild) keeps applying
        such rows from older ledgers unchanged, so a rebuild still reproduces the history
        it was given."""
        projection = await ProjectionEngine._locked_projection(session, entry)
        ProjectionEngine._refuse_other_kind(entry, projection)
        return await ProjectionEngine._write(session, entry, projection)

    @staticmethod
    def _refuse_other_kind(entry: LedgerEntry, projection: Projection | None) -> None:
        if projection is not None and projection.entity_type != entry.entity_type:
            raise _not_found(entry.entity_type)

    @staticmethod
    def _changes_missing_item(entry: LedgerEntry, projection: Projection | None) -> bool:
        """A change, not a birth, to an item with no projection: writing it would make
        an item out of the change alone."""
        return projection is None and entry.entity_type == "item" and entry.event_type not in ITEM_BIRTHS

    @staticmethod
    async def _write(session, entry: LedgerEntry, projection: Projection | None) -> Transition:
        if projection is None:
            fields = ProjectionEngine._next_fields({}, entry, 0)
            try:
                # SAVEPOINT so losing a concurrent first-event insert race rolls
                # back only this insert (same shape as the idempotency dedup in
                # emit_event), then retries as an update on the winner's row.
                async with session.begin_nested():
                    session.add(
                        Projection(
                            company_id=entry.company_id,
                            entity_id=entry.entity_id,
                            entity_type=entry.entity_type,
                            created_at=fields["updated_at"],
                            **fields,
                        )
                    )
                    await session.flush()
                return Transition(before=None, after=fields["state"])
            except IntegrityError as exc:
                # Only the (company_id, entity_id) primary-key race is a benign
                # concurrent-first-insert to retry as an update on the winner's row.
                # A stale physical-code unique index left by an earlier release is a real
                # conflict - surface it so the API maps it to 409 rather than swallowing it
                # as a PK race.
                if is_barcode_unique_violation(exc):
                    raise BarcodeConflictError((fields.get("state") or {}).get("barcode")) from exc
                if is_rfid_epc_unique_violation(exc):
                    raise RfidEpcConflictError((fields.get("state") or {}).get("rfid_epc")) from exc
                constraint = getattr(getattr(exc, "orig", None), "constraint_name", None)
                if constraint not in (None, "projections_pkey"):
                    raise
                projection = await ProjectionEngine._locked_projection(session, entry)
                if projection is None:
                    raise
                ProjectionEngine._refuse_other_kind(entry, projection)
        before = deepcopy(projection.state or {})
        fields = ProjectionEngine._next_fields(projection.state, entry, projection.version)
        for column, value in fields.items():
            setattr(projection, column, value)
        # Flush inside a SAVEPOINT so a stale physical-code unique index on an UPDATE
        # (not only a first insert) surfaces as a CodeConflictError -> 409 instead of
        # escaping to the outer commit masked as a 500. Unrelated integrity errors
        # re-raise unchanged.
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            if is_barcode_unique_violation(exc):
                raise BarcodeConflictError((fields.get("state") or {}).get("barcode")) from exc
            if is_rfid_epc_unique_violation(exc):
                raise RfidEpcConflictError((fields.get("state") or {}).get("rfid_epc")) from exc
            raise
        return Transition(before=before, after=fields["state"])

    @staticmethod
    async def rebuild(session, company_id=None) -> None:
        """Replace the projections with a replay of the ledger. Refused, before anything is
        deleted, when the ledger holds events of a module that is not enabled: replaying
        without its handler would rebuild those records wrong (UnhandledEventsError)."""
        unknown = await ProjectionEngine.unreplayable(session, company_id)
        if unknown:
            raise UnhandledEventsError(unknown)
        await session.execute(delete(Projection) if company_id is None else delete(Projection).where(Projection.company_id == company_id))
        query = select(LedgerEntry).order_by(LedgerEntry.id.asc())
        if company_id:
            query = query.where(LedgerEntry.company_id == company_id)
        for entry in (await session.execute(query)).scalars().all():
            projection = await ProjectionEngine._locked_projection(session, entry)
            # A change to an item with no birth before it is skipped, as a live write refuses
            # it live: replaying it would bring back a removed item as a ghost.
            if ProjectionEngine._changes_missing_item(entry, projection):
                continue
            await ProjectionEngine._write(session, entry, projection)

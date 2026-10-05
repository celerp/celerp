# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

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

log = logging.getLogger(__name__)


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


def _declared_prefixes() -> set[str]:
    """Every projection_handler prefix an installed module declares, running or not."""
    from celerp.modules.loader import module_search_path, read_manifest

    prefixes: set[str] = set()
    for entry in module_search_path().split(","):
        root = Path(entry)
        for pkg in (sorted(root.iterdir()) if entry and root.is_dir() else ()):
            contributions = ((read_manifest(pkg).get("slots") or {}).get("projection_handler")
                             if pkg.is_dir() else None)
            for c in contributions if isinstance(contributions, list) else ():
                if isinstance(c, dict) and isinstance(c.get("prefix"), str) and c["prefix"]:
                    prefixes.add(c["prefix"])
    return prefixes


class UnhandledEventsError(Exception):
    """The ledger holds events a replay cannot apply as written."""

    def __init__(self, event_types: set[str]):
        self.event_types = sorted(event_types)
        super().__init__(
            "Rebuild stopped before changing anything: the ledger has records no running module "
            f"can read ({', '.join(self.event_types)}). Turn on the module that wrote them, "
            "restart Celerp, then rebuild.")


async def unhandled_event_types(session, company_id=None) -> set[str]:
    """Ledger event types a replay cannot apply as written: not in the event catalog, or
    owned by a module whose handler is not running, which would fold them into records as
    raw data."""
    from celerp.events.schemas import EVENT_SCHEMA_MAP

    query = select(LedgerEntry.event_type).distinct()
    if company_id:
        query = query.where(LedgerEntry.company_id == company_id)
    types = set((await session.execute(query)).scalars())
    running = _get_module_handlers()
    declared = _declared_prefixes()
    return {t for t in types if t not in EVENT_SCHEMA_MAP
            or (not any(t.startswith(p) for p in running) and any(t.startswith(p) for p in declared))}


class ProjectionEngine:
    @staticmethod
    def _apply(state: dict, event_type: str, data: dict) -> dict:
        for prefix, fn in _get_module_handlers().items():
            if event_type.startswith(prefix):
                return fn(state, event_type, data)
        return {**state, **data}

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
    async def apply_event(session, entry: LedgerEntry) -> None:
        projection = await ProjectionEngine._locked_projection(session, entry)
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
                return
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

    @staticmethod
    async def rebuild(session, company_id=None) -> None:
        """Replay the ledger into fresh projections. Refused, with nothing changed, while
        any event in it cannot be applied as written (UnhandledEventsError)."""
        unhandled = await unhandled_event_types(session, company_id)
        if unhandled:
            raise UnhandledEventsError(unhandled)
        await session.execute(delete(Projection) if company_id is None else delete(Projection).where(Projection.company_id == company_id))
        query = select(LedgerEntry).order_by(LedgerEntry.id.asc())
        if company_id:
            query = query.where(LedgerEntry.company_id == company_id)
        for entry in (await session.execute(query)).scalars().all():
            await ProjectionEngine.apply_event(session, entry)

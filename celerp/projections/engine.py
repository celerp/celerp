# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

log = logging.getLogger(__name__)


def _resolve_module_handler(dotted: str):
    """Import and return a handler callable from a 'module.path:function' string."""
    try:
        module_path, func_name = dotted.rsplit(":", 1)
        mod = importlib.import_module(module_path)
        return getattr(mod, func_name)
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
            except IntegrityError:
                projection = await ProjectionEngine._locked_projection(session, entry)
        for column, value in ProjectionEngine._next_fields(projection.state, entry, projection.version).items():
            setattr(projection, column, value)

    @staticmethod
    async def rebuild(session, company_id=None) -> None:
        await session.execute(delete(Projection) if company_id is None else delete(Projection).where(Projection.company_id == company_id))
        query = select(LedgerEntry).order_by(LedgerEntry.id.asc())
        if company_id:
            query = query.where(LedgerEntry.company_id == company_id)
        for entry in (await session.execute(query)).scalars().all():
            await ProjectionEngine.apply_event(session, entry)

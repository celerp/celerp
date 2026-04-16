# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select

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
    async def apply_event(session, entry: LedgerEntry) -> None:
        projection = await session.get(Projection, {"company_id": entry.company_id, "entity_id": entry.entity_id})
        state = projection.state if projection else {}
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

        if projection is None:
            session.add(
                Projection(
                    company_id=entry.company_id,
                    entity_id=entry.entity_id,
                    entity_type=entry.entity_type,
                    state=next_state,
                    version=entry.id or 0,
                    location_id=location_id,
                    updated_at=now,
                    is_available=next_state.get("is_available"),
                    is_on_memo=next_state.get("is_on_memo"),
                    is_on_marketplace=next_state.get("is_on_marketplace"),
                    is_in_production=next_state.get("is_in_production"),
                    is_expired=next_state.get("is_expired"),
                    expires_at=expires_at,
                    consignment_flag=next_state.get("consignment_flag"),
                )
            )
        else:
            projection.state = next_state
            projection.version = entry.id or projection.version
            projection.location_id = location_id
            projection.updated_at = now
            projection.is_available = next_state.get("is_available")
            projection.is_on_memo = next_state.get("is_on_memo")
            projection.is_on_marketplace = next_state.get("is_on_marketplace")
            projection.is_in_production = next_state.get("is_in_production")
            projection.is_expired = next_state.get("is_expired")
            projection.expires_at = expires_at
            projection.consignment_flag = next_state.get("consignment_flag")

    @staticmethod
    async def rebuild(session, company_id=None) -> None:
        await session.execute(delete(Projection) if company_id is None else delete(Projection).where(Projection.company_id == company_id))
        query = select(LedgerEntry).order_by(LedgerEntry.id.asc())
        if company_id:
            query = query.where(LedgerEntry.company_id == company_id)
        for entry in (await session.execute(query)).scalars().all():
            await ProjectionEngine.apply_event(session, entry)

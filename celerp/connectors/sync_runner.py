# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Sync runner - wraps connector sync calls with audit trail recording."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from celerp.connectors.base import (
    ConnectorBase,
    ConnectorContext,
    SyncDirection,
    SyncEntity,
    SyncResult,
    entity_allowed,
)
from celerp.models.sync_run import SyncRun

log = logging.getLogger(__name__)

_SYNC_METHODS = {
    "products": "sync_products",
    "orders": "sync_orders",
    "contacts": "sync_contacts",
    "inventory": "sync_inventory",
    "products_out": "sync_products_out",
    "invoices_out": "sync_invoices_out",
    "inventory_out": "sync_inventory_out",
}
_OUTBOUND_ENTITIES = {"products_out", "invoices_out", "inventory_out"}


async def run_sync(
    connector: ConnectorBase,
    ctx: ConnectorContext,
    entity: str,
    since: datetime | None = None,
    direction: SyncDirection | None = None,
) -> SyncResult:
    """Execute a sync operation and record a SyncRun audit entry.

    If ``direction`` is provided, checks whether ``entity`` is allowed
    for that direction before running. Returns a failed SyncResult if blocked.
    """
    from celerp.db import get_session_ctx

    # Direction gate
    if direction and not entity_allowed(entity, direction):
        try:
            entity_enum = SyncEntity(entity)
        except ValueError:
            entity_enum = entity  # unknown entity, pass through
        direction_enum = direction if isinstance(direction, SyncDirection) else SyncDirection(direction)
        return SyncResult(
            entity=entity_enum,
            direction=direction_enum,
            errors=[f"{entity} sync blocked by direction={direction.value}"],
        )

    method_name = _SYNC_METHODS.get(entity)
    if method_name is None:
        raise ValueError(f"Unknown entity: {entity}")

    sync_method = getattr(connector, method_name, None)
    if sync_method is None:
        raise ValueError(f"{connector.name} has no method {method_name}")

    started_at = datetime.now(timezone.utc)

    try:
        if entity in _OUTBOUND_ENTITIES:
            result = await sync_method(ctx)
        else:
            result = await sync_method(ctx, since=since)
    except NotImplementedError:
        result = SyncResult(
            entity=entity,
            direction=connector.direction,
            errors=[f"{connector.name} does not support {entity} sync"],
        )
    except Exception as exc:
        result = SyncResult(
            entity=entity,
            direction=connector.direction,
            errors=[f"Unexpected error: {exc}"],
        )

    finished_at = datetime.now(timezone.utc)

    if result.errors and result.created == 0 and result.updated == 0:
        status = "failed"
    elif result.errors:
        status = "partial"
    else:
        status = "success"

    run = SyncRun(
        company_id=ctx.company_id,
        connector=connector.name,
        entity=entity,
        direction=result.direction.value if hasattr(result.direction, "value") else str(result.direction),
        started_at=started_at,
        finished_at=finished_at,
        created_count=result.created,
        updated_count=result.updated,
        skipped_count=result.skipped,
        errors_json=json.dumps(result.errors) if result.errors else None,
        status=status,
    )

    try:
        async with get_session_ctx() as session:
            session.add(run)
            await session.commit()
    except Exception as exc:
        log.warning("Failed to record SyncRun: %s", exc)

    log.info(
        "sync_run %s.%s company=%s status=%s created=%d updated=%d skipped=%d errors=%d",
        connector.name, entity, ctx.company_id, status,
        result.created, result.updated, result.skipped,
        len(result.errors or []),
    )

    return result

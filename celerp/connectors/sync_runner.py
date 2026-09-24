# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Sync runner - wraps connector sync calls with audit trail recording."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

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
CONNECTOR_RESET_ENTITY = "__connector_reset__"
_OUTBOUND_ENTITY_METHODS = {
    "products_out": "sync_products_out",
    "invoices_out": "sync_invoices_out",
    "inventory_out": "sync_inventory_out",
}


def supported_outbound(connector: ConnectorBase) -> list[str]:
    """Outbound entities implemented by this connector, in stable dispatch order."""
    return [
        entity for entity, method in _OUTBOUND_ENTITY_METHODS.items()
        if getattr(type(connector), method, None) is not getattr(ConnectorBase, method, None)
    ]


def sync_plan(connector: ConnectorBase, direction: SyncDirection) -> list[str]:
    """One direction-aware plan shared by connect, manual, and reconciliation paths."""
    direction = direction if isinstance(direction, SyncDirection) else SyncDirection(direction)
    plan: list[str] = []
    if direction in (SyncDirection.INBOUND, SyncDirection.BOTH):
        plan.extend(e.value for e in connector.supported_entities)
    if direction in (SyncDirection.OUTBOUND, SyncDirection.BOTH):
        plan.extend(supported_outbound(connector))
    return plan


async def run_connector_sync(
    connector: ConnectorBase,
    ctx: ConnectorContext,
    direction: SyncDirection,
    *,
    full_entities: set[str] | None = None,
) -> list[SyncResult]:
    """Execute a connector's canonical plan through the audited per-entity runner."""
    full_entities = full_entities or set()
    return [
        await run_sync(
            connector,
            ctx,
            entity,
            direction=direction,
            use_watermark=entity not in full_entities,
        )
        for entity in sync_plan(connector, direction)
    ]


async def _last_success_watermark(company_id: str, connector: str, entity: str):
    """The start time of the most recent FULLY successful sync for this
    (company, connector, entity), used as the incremental `since`. Returns None on
    the first sync or if it can't be read, which means a full pull.

    Only fully-successful runs advance the cursor: a partial run left some records
    errored, so the next run must re-pull from the last good point to retry them
    rather than skipping past them."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx

    try:
        async with get_session_ctx() as session:
            reset_at = await session.scalar(
                sa.select(sa.func.max(SyncRun.started_at)).where(
                    SyncRun.company_id == company_id,
                    SyncRun.connector == connector,
                    SyncRun.entity == CONNECTOR_RESET_ENTITY,
                )
            )
            conditions = [
                SyncRun.company_id == company_id,
                SyncRun.connector == connector,
                SyncRun.entity == entity,
                SyncRun.status == "success",
            ]
            if reset_at is not None:
                conditions.append(SyncRun.started_at > reset_at)
            return await session.scalar(
                sa.select(sa.func.max(SyncRun.started_at)).where(*conditions)
            )
    except Exception as exc:
        # A read failure degrades to a full re-pull (safe, dup-safe via idempotency
        # keys) — but say so rather than silently widening every sync.
        log.warning("Could not read sync watermark for %s.%s: %s — full pull", connector, entity, exc)
        return None


# Concurrency guard: a still-unfinished run older than this is treated as dead (the
# process likely crashed mid-sync), so a new run is allowed to supersede it.
_STALE_AFTER = timedelta(minutes=15)
_BUSY = "busy"


async def _begin_run(company_id: str, connector: str, entity: str, direction_str: str, started_at):
    """Insert an in-progress SyncRun (status=running, finished_at=None) so the UI can
    observe an active sync, and return its id. Returns _BUSY if a recent unfinished run
    for this (company, connector, entity) already exists (the concurrency guard). Returns
    None if the row could not be written, in which case the sync still runs and a single
    final row is written by _finish_run."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx

    try:
        async with get_session_ctx() as session:
            # Serialize check+insert itself. Without this lock, two workers can both
            # observe no running row and each insert one.
            await session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"sync-run:{company_id}:{connector}:{entity}"},
            )
            existing = await session.scalar(
                sa.select(SyncRun.id)
                .where(
                    SyncRun.company_id == company_id,
                    SyncRun.connector == connector,
                    SyncRun.entity == entity,
                    SyncRun.finished_at.is_(None),
                    SyncRun.started_at >= started_at - _STALE_AFTER,
                )
                .limit(1)
            )
            if existing is not None:
                return _BUSY
            run = SyncRun(
                company_id=company_id, connector=connector, entity=entity,
                direction=direction_str, started_at=started_at, finished_at=None,
                created_count=0, updated_count=0, skipped_count=0,
                errors_json=None, status="running",
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run.id
    except Exception as exc:
        log.warning("Failed to record in-progress SyncRun: %s", exc)
        return None


async def _finish_run(run_id, company_id, connector, entity, result, started_at, finished_at, status):
    """Update the in-progress row with the final outcome, or insert a final row if the
    in-progress write failed (run_id is None)."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx

    direction_str = result.direction.value if hasattr(result.direction, "value") else str(result.direction)
    errors_json = json.dumps(result.errors) if result.errors else None
    try:
        async with get_session_ctx() as session:
            if run_id is not None:
                await session.execute(
                    sa.update(SyncRun).where(SyncRun.id == run_id).values(
                        direction=direction_str, finished_at=finished_at,
                        created_count=result.created, updated_count=result.updated,
                        skipped_count=result.skipped, errors_json=errors_json, status=status,
                    )
                )
            else:
                session.add(SyncRun(
                    company_id=company_id, connector=connector, entity=entity,
                    direction=direction_str, started_at=started_at, finished_at=finished_at,
                    created_count=result.created, updated_count=result.updated,
                    skipped_count=result.skipped, errors_json=errors_json, status=status,
                ))
            await session.commit()
    except Exception as exc:
        log.warning("Failed to record SyncRun: %s", exc)


async def run_sync(
    connector: ConnectorBase,
    ctx: ConnectorContext,
    entity: str,
    since: datetime | None = None,
    direction: SyncDirection | None = None,
    use_watermark: bool = True,
) -> SyncResult:
    """Execute a sync operation and record a SyncRun audit entry.

    If ``direction`` is provided, checks whether ``entity`` is allowed
    for that direction before running. Returns a failed SyncResult if blocked.
    """
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
    intended_direction = direction if isinstance(direction, SyncDirection) else connector.direction
    direction_str = intended_direction.value if hasattr(intended_direction, "value") else str(intended_direction)

    # Mark this entity's sync as in progress (and refuse to start a second concurrent
    # run for the same entity). The UI polls these rows for live status.
    run_id = await _begin_run(ctx.company_id, connector.name, entity, direction_str, started_at)
    if run_id == _BUSY:
        return SyncResult(
            entity=entity,
            direction=intended_direction,
            errors=[f"{entity} sync already in progress"],
        )

    # Incremental by default: pull only what changed since the last successful run
    # for this entity. Idempotency keys make any overlap dup-safe.
    if since is None and entity not in _OUTBOUND_ENTITIES and use_watermark:
        since = await _last_success_watermark(ctx.company_id, connector.name, entity)

    try:
        from celerp.connectors.ownership import lock_connector_operation
        from celerp.db import get_session_ctx

        async with get_session_ctx() as guard_session:
            await lock_connector_operation(
                guard_session, ctx.company_id, connector.name
            )
            if entity in _OUTBOUND_ENTITIES:
                result = await sync_method(ctx)
            else:
                result = await sync_method(ctx, since=since)
            await guard_session.commit()
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

    await _finish_run(run_id, ctx.company_id, connector.name, entity, result, started_at, finished_at, status)

    log.info(
        "sync_run %s.%s company=%s status=%s created=%d updated=%d skipped=%d errors=%d",
        connector.name, entity, ctx.company_id, status,
        result.created, result.updated, result.skipped,
        len(result.errors or []),
    )

    return result

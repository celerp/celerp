# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Sync runner - wraps connector sync calls with audit trail recording."""
from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timedelta, timezone

from celerp.connectors.base import (
    INBOUND_ENTITIES,
    OUTBOUND_ENTITIES,
    ConnectorBase,
    ConnectorContext,
    FirstSync,
    SyncDirection,
    SyncEntity,
    SyncResult,
    entity_allowed,
)
from celerp.connectors.ownership import ConnectorStoreChangedError
from celerp.connectors.relay_token import ConnectorUpgradeRequired, ConnectRequired
from celerp.models.sync_run import SyncRun

log = logging.getLogger(__name__)

_SYNC_METHODS = {entity: f"sync_{entity}" for entity in (*INBOUND_ENTITIES, *OUTBOUND_ENTITIES)}
CONNECTOR_RESET_ENTITY = "__connector_reset__"


def implements(connector: ConnectorBase, entity: str) -> bool:
    """Whether the connector overrides the sync method for ``entity``."""
    method = _SYNC_METHODS.get(entity)
    if method is None:
        return False
    own = getattr(type(connector), method, None)
    return own is not None and own is not getattr(ConnectorBase, method, None)


def entity_direction(entity: str) -> SyncDirection:
    """The way one sync carries records: pulls are inbound, pushes outbound."""
    return SyncDirection.OUTBOUND if entity in OUTBOUND_ENTITIES else SyncDirection.INBOUND


def sync_plan(connector: ConnectorBase, directions: dict | None) -> list[str]:
    """The syncs these per-record directions run, shared by the first sync, manual,
    webhook and reconciliation paths: every pull first, then every push, each in
    the stable order of INBOUND_ENTITIES and OUTBOUND_ENTITIES."""
    return [
        entity for entity in (*INBOUND_ENTITIES, *OUTBOUND_ENTITIES)
        if entity_allowed(connector, entity, directions)
    ]


def _links_products(connector: ConnectorBase, entity: str, directions: dict,
                    first_sync: FirstSync | None) -> bool:
    """A first sync reads the platform's products to link the ones Celerp already has
    whenever products sync at all, even when they only go out."""
    return (
        first_sync is not None and entity == "products"
        and (directions or {}).get("products", SyncDirection.OFF.value) != SyncDirection.OFF.value
        and "first_sync" in inspect.signature(getattr(connector, _SYNC_METHODS[entity])).parameters
    )


async def run_connector_sync(
    connector: ConnectorBase,
    ctx: ConnectorContext,
    *,
    reconcile: bool = False,
    first_sync: FirstSync | None = None,
) -> list[SyncResult]:
    """Execute one stable connector generation through the audited per-entity
    runner, in the directions the connection is set to. ``reconcile`` marks the
    daily reconciliation pass; ``first_sync`` the first sync, whose product pass
    also runs when products only go out."""
    from celerp.connectors.ownership import lock_connector_operation
    from celerp.db import get_session_ctx

    async with get_session_ctx() as guard_session:
        config = await lock_connector_operation(
            guard_session, ctx.company_id, connector.name
        )
        directions = dict(config.entity_directions) if config is not None else connector.default_directions
        expected_config_id = config.id if config is not None else None
        expected_directions = directions if config is not None else None
        await guard_session.commit()

    plan = sync_plan(connector, directions)
    if "products" not in plan and _links_products(connector, "products", directions, first_sync):
        plan.insert(0, "products")
    if any(entity in OUTBOUND_ENTITIES for entity in plan):
        await connector.before_outbound(ctx)
    results: list[SyncResult] = []
    completed: dict[str, bool] = {}
    dependencies = getattr(connector, "outbound_dependencies", {})
    for entity in plan:
        if any(completed.get(dep) is not True for dep in dependencies.get(entity, ())):
            break
        result = await run_sync(
            connector, ctx, entity,
            reconcile=reconcile, first_sync=first_sync, expected_config_id=expected_config_id,
            expected_directions=expected_directions,
            expected_store_handle=ctx.store_handle,
        )
        completed[entity] = result.ok
        results.append(result)
    return results


async def run_first_sync(connector: ConnectorBase, ctx: ConnectorContext) -> list[SyncResult]:
    """Run a connection's first sync, once a person started it from its preview, with the
    choices saved then (ConnectorConfig.first_sync_undo). The items it creates join its
    import batch and the items it links are recorded, so both can be undone; an import
    batch that ends up empty is removed."""
    import uuid

    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig
    from celerp.models.import_batch import ImportBatch
    from celerp_inventory.services import ExternalImportRecord
    import sqlalchemy as sa

    async with get_session_ctx() as session:
        config = await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(ctx.company_id),
            ConnectorConfig.connector == connector.name,
        ))
        if config is None or config.first_sync_at is None:
            raise ValueError(f"The first sync of {connector.display_name} has not been started")
        choices = dict(config.first_sync_undo or {})
        create = entity_allowed(connector, "products", config.entity_directions)
    first_sync = FirstSync(match=choices.get("match", "keep"), create=create,
                           batch_id=choices.get("batch_id"), record=ExternalImportRecord())
    results = await run_connector_sync(connector, ctx, first_sync=first_sync)

    record = first_sync.record
    async with get_session_ctx() as session:
        config = await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(ctx.company_id),
            ConnectorConfig.connector == connector.name,
        ).with_for_update())
        if config is not None:
            undo = dict(config.first_sync_undo or {})
            undo["links"] = [*undo.get("links", []), *record.linked]
            config.first_sync_undo = undo
        if first_sync.batch_id is not None:
            batch = await session.get(ImportBatch, uuid.UUID(first_sync.batch_id), with_for_update=True)
            if batch is not None and not record.created and not batch.entity_ids:
                await session.delete(batch)
                if config is not None:
                    config.first_sync_undo = {**config.first_sync_undo, "batch_id": None}
            elif batch is not None:
                batch.entity_ids = [*batch.entity_ids, *(eid for eid, _ in record.created)]
                batch.idempotency_keys = [*batch.idempotency_keys, *(key for _, key in record.created),
                                          *record.files]
                batch.row_count = len(batch.entity_ids)
        await session.commit()
    return results


def _since_reset(company_id: str, connector: str):
    """Conditions selecting this connector's runs since its last reset: a
    disconnect or reconnect starts the history over."""
    import sqlalchemy as sa

    reset_at = (
        sa.select(sa.func.max(SyncRun.started_at))
        .where(
            SyncRun.company_id == company_id,
            SyncRun.connector == connector,
            SyncRun.entity == CONNECTOR_RESET_ENTITY,
        )
        .scalar_subquery()
    )
    return [
        SyncRun.company_id == company_id,
        SyncRun.connector == connector,
        sa.or_(reset_at.is_(None), SyncRun.started_at > reset_at),
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
            return await session.scalar(
                sa.select(sa.func.max(SyncRun.started_at)).where(
                    *_since_reset(company_id, connector),
                    SyncRun.entity == entity,
                    SyncRun.status == "success",
                )
            )
    except Exception as exc:
        # A read failure degrades to a full re-pull (safe, dup-safe via idempotency
        # keys) — but say so rather than silently widening every sync.
        log.warning("Could not read sync watermark for %s.%s: %s — full pull", connector, entity, exc)
        return None


async def _attention_run(
    session, company_id: str, connector: str, entity: str, *, for_update: bool = False
):
    """The run holding the current attention list for one entity: the latest
    finished run since the last reset whose sync returned a list. A run that
    stopped before its sync produced one (a failed fetch, a guard, a crash)
    records none, so it never replaces the list."""
    import sqlalchemy as sa

    query = (
        sa.select(SyncRun)
        .where(
            *_since_reset(company_id, connector),
            SyncRun.entity == entity,
            SyncRun.finished_at.is_not(None),
            SyncRun.attention_json.is_not(None),
        )
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    )
    return await session.scalar(query.with_for_update() if for_update else query)


async def attention_entries(
    company_id: str, connector: str, entity: str | None = None
) -> list[dict]:
    """Records still waiting on a person, for one entity or (entity None) the
    whole connector, where each entry also names its ``entity``. Read errors
    propagate: a sync that cannot read the list must fail rather than start an
    empty one."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        entities = [entity] if entity is not None else (await session.scalars(
            sa.select(SyncRun.entity).distinct().where(
                *_since_reset(company_id, connector),
                SyncRun.attention_json.is_not(None),
            )
        )).all()
        entries: list[dict] = []
        for name in entities:
            run = await _attention_run(session, company_id, connector, name)
            if run is not None:
                entries.extend(run.attention if entity is not None
                               else [{**e, "entity": name} for e in run.attention])
        return entries


async def update_attention_entry(
    session, company_id: str, connector: str, entity: str, record_id: str, update
) -> dict | None:
    """Apply ``update`` to one entry of the current attention list and save the
    list in the caller's transaction. Returns the updated entry, or None when
    the record is not on the list. ``update`` may raise to refuse the change."""
    run = await _attention_run(session, company_id, connector, entity, for_update=True)
    if run is None:
        return None
    entries = run.attention
    entry = next((e for e in entries if str(e.get("id")) == str(record_id)), None)
    if entry is None:
        return None
    update(entry)
    run.attention_json = json.dumps(entries)
    return entry


async def remove_attention_entry(
    session, company_id: str, connector: str, entity: str, record_id: str
) -> dict | None:
    """Take one entry off the current attention list in the caller's transaction and
    return it, or None when the record is not on the list."""
    run = await _attention_run(session, company_id, connector, entity, for_update=True)
    if run is None:
        return None
    entries = run.attention
    entry = next((e for e in entries if str(e.get("id")) == str(record_id)), None)
    if entry is None:
        return None
    run.attention_json = json.dumps([e for e in entries if e is not entry])
    return entry


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
    attention_json = json.dumps(result.attention) if result.attention is not None else None
    try:
        async with get_session_ctx() as session:
            if run_id is not None:
                await session.execute(
                    sa.update(SyncRun).where(SyncRun.id == run_id).values(
                        direction=direction_str, finished_at=finished_at,
                        created_count=result.created, updated_count=result.updated,
                        skipped_count=result.skipped, errors_json=errors_json,
                        attention_json=attention_json, status=status,
                    )
                )
            else:
                session.add(SyncRun(
                    company_id=company_id, connector=connector, entity=entity,
                    direction=direction_str, started_at=started_at, finished_at=finished_at,
                    created_count=result.created, updated_count=result.updated,
                    skipped_count=result.skipped, errors_json=errors_json,
                    attention_json=attention_json, status=status,
                ))
            await session.commit()
    except Exception as exc:
        log.warning("Failed to record SyncRun: %s", exc)


async def run_sync(
    connector: ConnectorBase,
    ctx: ConnectorContext,
    entity: str,
    since: datetime | None = None,
    use_watermark: bool = True,
    reconcile: bool = False,
    first_sync: FirstSync | None = None,
    expected_config_id=None,
    expected_directions: dict | None = None,
    expected_store_handle: str | None = None,
    expected_webhook_secret: str | None = None,
) -> SyncResult:
    """Execute one connector entity sync behind the current ownership/config
    fence. ``reconcile`` reaches sync methods that take it: the connector
    decides what its reconciliation pass checks beyond the incremental pull."""
    method_name = _SYNC_METHODS.get(entity)
    if method_name is None:
        raise ValueError(f"Unknown entity: {entity}")

    sync_method = getattr(connector, method_name, None)
    if sync_method is None:
        raise ValueError(f"{connector.name} has no method {method_name}")

    effective_direction = entity_direction(entity)
    started_at = datetime.now(timezone.utc)
    run_id = await _begin_run(
        ctx.company_id,
        connector.name,
        entity,
        effective_direction.value,
        started_at,
    )
    if run_id == _BUSY:
        return SyncResult(
            entity=entity,
            direction=effective_direction,
            errors=[f"{entity} sync already in progress"],
        )

    try:
        from celerp.connectors.ownership import ensure_store_bound, lock_connector_operation
        from celerp.connectors.relay_token import fetch_context
        from celerp.db import get_session_ctx

        async with get_session_ctx() as guard_session:
            config = await lock_connector_operation(
                guard_session, ctx.company_id, connector.name
            )
            directions = (
                dict(config.entity_directions) if config is not None
                else connector.default_directions
            )

            connection_changed = (
                (
                    expected_config_id is not None
                    and (config is None or config.id != expected_config_id)
                )
                or (
                    expected_directions is not None
                    and directions != expected_directions
                )
                or (
                    expected_webhook_secret is not None
                    and (
                        config is None
                        or config.webhook_secret != expected_webhook_secret
                    )
                )
            )
            if connection_changed:
                result = SyncResult(
                    entity=entity,
                    direction=effective_direction,
                    errors=["connector connection changed while sync plan was running"],
                )
            elif not (entity_allowed(connector, entity, directions)
                      or _links_products(connector, entity, directions, first_sync)):
                try:
                    entity_enum = SyncEntity(entity)
                except ValueError:
                    entity_enum = entity
                result = SyncResult(
                    entity=entity_enum,
                    direction=effective_direction,
                    errors=[f"{entity} sync blocked by direction setting"],
                )
            else:
                if (
                    since is None
                    and entity not in OUTBOUND_ENTITIES
                    and use_watermark
                ):
                    since = await _last_success_watermark(
                        ctx.company_id, connector.name, entity
                    )

                current_ctx = ctx
                if config is not None:
                    current_ctx = await fetch_context(
                        ctx.company_id,
                        connector.name,
                        ownership_session=guard_session,
                    )
                    if current_ctx is None:
                        raise RuntimeError(
                            "connector credentials are temporarily unavailable"
                        )

                if (
                    expected_store_handle is not None
                    and current_ctx.store_handle != expected_store_handle
                ):
                    result = SyncResult(
                        entity=entity,
                        direction=effective_direction,
                        errors=["connector connection changed while sync plan was running"],
                    )
                else:
                    # Its own short transaction: the sync below runs holding
                    # no lock on the company.
                    await ensure_store_bound(ctx.company_id, connector, current_ctx)
                    if entity in OUTBOUND_ENTITIES:
                        result = await sync_method(current_ctx)
                    else:
                        accepts = inspect.signature(sync_method).parameters
                        options: dict = {}
                        if "attention" in accepts:
                            options["attention"] = await attention_entries(
                                ctx.company_id, connector.name, entity
                            )
                        if reconcile and "reconcile" in accepts:
                            options["reconcile"] = True
                        if "orders_from" in accepts and config is not None:
                            options["orders_from"] = config.orders_from
                        if first_sync is not None and "first_sync" in accepts:
                            options["first_sync"] = first_sync
                        result = await sync_method(current_ctx, since=since, **options)
                await guard_session.commit()
    except (ConnectorStoreChangedError, ConnectorUpgradeRequired, ConnectRequired) as exc:
        result = SyncResult(entity=entity, direction=effective_direction, errors=[str(exc)])
    except NotImplementedError:
        result = SyncResult(
            entity=entity,
            direction=effective_direction,
            errors=[f"{connector.name} does not support {entity} sync"],
        )
    except Exception as exc:
        result = SyncResult(
            entity=entity,
            direction=effective_direction,
            errors=[f"Unexpected error: {exc}"],
        )

    finished_at = datetime.now(timezone.utc)

    if result.errors and result.created == 0 and result.updated == 0:
        status = "failed"
    elif result.errors:
        status = "partial"
    else:
        status = "success"

    await _finish_run(
        run_id,
        ctx.company_id,
        connector.name,
        entity,
        result,
        started_at,
        finished_at,
        status,
    )

    log.info(
        "sync_run %s.%s company=%s status=%s created=%d updated=%d skipped=%d errors=%d",
        connector.name,
        entity,
        ctx.company_id,
        status,
        result.created,
        result.updated,
        result.skipped,
        len(result.errors or []),
    )

    return result

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Sync runner - wraps connector sync calls with audit trail recording."""
from __future__ import annotations

import inspect
import json
import logging
import uuid
from datetime import datetime, timezone

from celerp.connectors.base import (
    INBOUND_ENTITIES,
    OUTBOUND_ENTITIES,
    ConnectorBase,
    ConnectorContext,
    ExternalImportRecord,
    FirstSync,
    SyncDirection,
    SyncEntity,
    SyncResult,
    entity_allowed,
)
from celerp.connectors.ownership import ConnectorStoreChangedError
from celerp.connectors.relay_token import ConnectorUpgradeRequired, ConnectRequired
from celerp.modules.boundary import ModuleBusy, ModuleRefused
from celerp.models.sync_run import SyncRun
from celerp.services.company_lock import lock_company

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
    company_id,
    *,
    purpose: str,
    reconcile: bool = False,
    first_sync: FirstSync | None = None,
    resource: str | None = None,
) -> list[SyncResult]:
    """Run every entity the connection's directions sync, in one execution over their
    resources (joined when the caller already runs one). ``reconcile`` marks the
    daily reconciliation pass; ``first_sync`` the first sync, whose product pass
    also runs when products only go out; ``resource`` limits it to the syncs that
    write that resource (a rerun)."""
    from celerp.connectors.execution import connector_execution, plan_resources

    directions = await _directions(company_id, connector.name)
    plan = sync_plan(connector, directions)
    if "products" not in plan and _links_products(connector, "products", directions, first_sync):
        plan.insert(0, "products")
    resources = sorted({_resource_of(entity) for entity in plan} | set(plan_resources(connector, directions)))
    if resource is not None:
        plan = [entity for entity in plan if _resource_of(entity) == resource]
        resources = [resource]
    results: list[SyncResult] = []
    async with connector_execution(company_id, connector, purpose, resources) as ex:
        if any(entity in OUTBOUND_ENTITIES for entity in plan):
            await connector.before_outbound(ex.ctx)
        completed: dict[str, bool] = {}
        phases: dict[str, bool] = {}
        last_of = {_resource_of(entity): entity for entity in plan}
        dependencies = getattr(connector, "outbound_dependencies", {})
        for entity in plan:
            phase = _resource_of(entity)
            if first_sync is not None and phase in first_sync.completed:
                completed[entity] = True
                continue
            # An entity whose dependency did not complete does not run; the rest still do.
            if any(completed.get(dep) is not True for dep in dependencies.get(entity, ())):
                completed[entity] = False
            else:
                result = await run_sync(connector, company_id, entity, purpose=purpose,
                                        reconcile=reconcile, first_sync=first_sync)
                completed[entity] = result.ok
                results.append(result)
            phases[phase] = phases.get(phase, True) and completed[entity]
            if first_sync is not None and last_of[phase] == entity:
                await _record_phase(company_id, connector.name, phase, phases[phase])
    return results


async def _directions(company_id, connector_name: str) -> dict:
    """The connection's directions; an unconnected company has none (fail closed: the
    execution's guard refuses it)."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    async with get_session_ctx() as session:
        directions = await session.scalar(sa.select(ConnectorConfig.entity_directions).where(
            ConnectorConfig.company_id == str(company_id), ConnectorConfig.connector == connector_name))
    return dict(directions or {})


async def run_first_sync(connector: ConnectorBase, ctx: ConnectorContext) -> list[SyncResult]:
    """Run a connection's first sync, inside its first-sync execution, with the choices a
    person saved on its preview (ConnectorFirstSync). The items it creates join its
    import batch and the items it links its undo record as each is written, and each
    resource's outcome is saved as it ends, so an attempt after a stop runs only the
    resources that did not complete. The record becomes ``completed`` only when every
    resource completed (an import batch then still empty is removed), else ``failed``
    with the reason."""
    from celerp.connectors.execution import plan_resources
    from celerp.db import get_session_ctx

    key = (str(ctx.company_id), connector.name)
    async with get_session_ctx() as session:
        row = await _locked_first_sync(session, *key)
        if row is None or row.state not in ("starting", "running"):
            raise ValueError(f"The first sync of {connector.display_name} has not been started")
        row.state = "running"
        directions = dict(row.directions or {})
        resources = plan_resources(connector, directions)
        progress = dict(row.progress or {})
        completed = frozenset(r for r in resources if (progress.get(r) or {}).get("state") == "completed")
        row.progress = {r: progress[r] if r in completed else {"state": "running"} for r in resources}
        match, batch_id, operation = row.match or "keep", row.batch_id, row.operation_id
        await session.commit()
    first_sync = FirstSync(match=match, create=entity_allowed(connector, "products", directions),
                           batch_id=batch_id, record=ExternalImportRecord(first_sync=key), operation=operation,
                           completed=completed)
    results = await run_connector_sync(connector, ctx.company_id, purpose="first_sync", first_sync=first_sync)

    async with get_session_ctx() as session:
        row = await _locked_first_sync(session, *key)
        if row is None:
            return results
        progress = dict(row.progress or {})
        row.progress = {r: progress.get(r) if (progress.get(r) or {}).get("state") == "completed"
                        else {"state": "failed"} for r in resources}
        done = all(row.progress[r]["state"] == "completed" for r in resources)
        row.state = "completed" if done else "failed"
        row.error = None if done else {
            "detail": "; ".join(e for f in results if not f.ok for e in (f.errors or [])[:1])}
        batch = await _first_sync_batch(session, row) if done else None
        if batch is not None and not batch.entity_ids:
            await session.delete(batch)
            row.batch_id = None
        await session.commit()
    return results


async def _record_phase(company_id, connector_name: str, resource: str, ok: bool) -> None:
    """Save how one resource of a running first sync ended, in a short transaction of its
    own, as the resource ends."""
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        row = await _locked_first_sync(session, str(company_id), connector_name)
        if row is not None and row.state == "running":
            row.progress = {**(row.progress or {}), resource: {"state": "completed" if ok else "failed"}}
        await session.commit()


async def execute_first_sync(company_id, connector_name: str, *, admission_wait: float | None = None) -> None:
    """Run a connection's started first sync to its end, in one execution over every
    resource it writes: the store's webhooks, then run_first_sync, with the choices saved
    when it started. The start and retry requests run it, and the execution sweeper runs a
    first sync no process holds. A failure the first sync did not record leaves it needing
    attention with the reason; a busy or refused execution leaves it as it was."""
    import sqlalchemy as sa

    from celerp import connectors
    from celerp.connectors.execution import WEBHOOKS, connector_execution, plan_resources
    from celerp.connectors.webhook_reconcile import reconcile_webhooks
    from celerp.db import get_session_ctx
    from celerp.models.connector_first_sync import ConnectorFirstSync

    connector = connectors.get(connector_name)
    async with get_session_ctx() as session:
        directions = await session.scalar(sa.select(ConnectorFirstSync.directions).where(
            ConnectorFirstSync.company_id == str(company_id), ConnectorFirstSync.connector == connector_name))
    resources = [*plan_resources(connector, directions or {}), WEBHOOKS]
    async with connector_execution(company_id, connector, "first_sync", resources,
                                   admission_wait=admission_wait) as ex:
        try:
            await reconcile_webhooks(ex)
            await run_first_sync(connector, ex.ctx)
        except Exception as exc:
            await fail_first_sync(company_id, connector_name, exc)
            raise


async def retry_first_sync(session, company_id, connector_name: str) -> None:
    """Start a failed first sync again, in the caller's transaction: it goes back to
    starting with its reason cleared, keeping its import batch, choices, order cutoff,
    the resources it completed and what it recorded. ValueError when it has not failed."""
    row = await _locked_first_sync(session, str(company_id), connector_name)
    if row is None or row.state != "failed":
        raise ValueError(f"The first sync of {connector_name} has not failed")
    row.state = "starting"
    row.error = None


async def _first_sync_batch(session, row):
    """The import batch of a first sync whose record the caller holds, locked; None
    when it has none."""
    from celerp.models.import_batch import ImportBatch

    if row.batch_id is None:
        return None
    return await session.get(ImportBatch, uuid.UUID(row.batch_id), with_for_update=True)


async def _locked_first_sync(session, company_id: str, connector_name: str):
    """A connection's first-sync record under its company's lock, taken first as every
    writer of the company's connector rows does; None when it is gone."""
    from celerp.models.connector_first_sync import ConnectorFirstSync

    await lock_company(session, uuid.UUID(company_id))
    return await session.get(ConnectorFirstSync, (company_id, connector_name), with_for_update=True,
                             populate_existing=True)


def _resource_of(entity) -> str:
    from celerp.connectors.execution import execution_resource

    return execution_resource("entity", getattr(entity, "value", entity))


async def fail_first_sync(company_id, connector_name: str, exc: BaseException) -> None:
    """Record that a connection's first sync stopped on ``exc``: it needs attention,
    with the store's reason (a locale key for a key the store will not let write, the
    store's own text otherwise)."""
    import httpx

    from celerp.db import get_session_ctx

    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    reason = {"key": "connectors.err_read_only_key"} if status in (401, 403) else {"detail": str(exc)}
    async with get_session_ctx() as session:
        row = await _locked_first_sync(session, str(company_id), connector_name)
        if row is not None and row.state in ("starting", "running"):
            row.state = "failed"
            row.error = reason
        await session.commit()


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
    records none, so it never replaces the list. Locking it takes the company
    first (lock order: companies before sync_runs)."""
    import sqlalchemy as sa

    if for_update:
        await lock_company(session, company_id)
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


async def _begin_run(company_id: str, connector: str, entity: str, direction_str: str, started_at):
    """Insert an in-progress SyncRun (status=running, finished_at=None) so the UI can
    observe an active sync, and return its id. The execution's lease is the
    concurrency guard; this row is only the record."""
    from celerp.db import get_session_ctx

    async with get_session_ctx() as session:
        await lock_company(session, uuid.UUID(str(company_id)))
        run = SyncRun(
            company_id=company_id, connector=connector, entity=entity,
            direction=direction_str, started_at=started_at, finished_at=None,
            created_count=0, updated_count=0, skipped_count=0,
            errors_json=None, status="running",
        )
        session.add(run)
        await session.commit()
        return run.id


async def _finish_run(run_id, company_id, connector, entity, result, started_at, finished_at, status):
    """Update the in-progress row with the final outcome."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx

    direction_str = result.direction.value if hasattr(result.direction, "value") else str(result.direction)
    errors_json = json.dumps(result.errors) if result.errors else None
    attention_json = json.dumps(result.attention) if result.attention is not None else None
    try:
        async with get_session_ctx() as session:
            await lock_company(session, uuid.UUID(str(company_id)))
            await session.execute(
                sa.update(SyncRun).where(SyncRun.id == run_id).values(
                    direction=direction_str, finished_at=finished_at,
                    created_count=result.created, updated_count=result.updated,
                    skipped_count=result.skipped, errors_json=errors_json,
                    attention_json=attention_json, status=status,
                )
            )
            await session.commit()
    except Exception as exc:
        log.warning("Failed to record SyncRun: %s", exc)


async def record_refused_run(company_id, connector_name: str, entity: str, exc: ModuleRefused) -> None:
    """Record on the store's sync history a run of *entity* that *exc* refused before
    anything was fetched or changed, in the refusal's own words."""
    direction = entity_direction(entity)
    started_at = datetime.now(timezone.utc)
    run_id = await _begin_run(str(company_id), connector_name, entity, direction.value, started_at)
    result = SyncResult(entity=entity, direction=direction, errors=[str(exc)], busy=isinstance(exc, ModuleBusy),
                        final=not isinstance(exc, ModuleBusy))
    await _finish_run(run_id, str(company_id), connector_name, entity, result, started_at,
                      datetime.now(timezone.utc), "failed")


async def run_sync(
    connector: ConnectorBase,
    company_id,
    entity: str,
    *,
    purpose: str,
    since: datetime | None = None,
    use_watermark: bool = True,
    reconcile: bool = False,
    first_sync: FirstSync | None = None,
) -> SyncResult:
    """Run one connector entity sync in an execution over its resource (joined when
    the caller already runs one). ``reconcile`` reaches sync methods that take it:
    the connector decides what its reconciliation pass checks beyond the
    incremental pull. ExecutionError subclasses reach the caller."""
    import sqlalchemy as sa

    from celerp.connectors.execution import ExecutionError, connector_execution
    from celerp.connectors.ownership import ensure_store_bound
    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    method_name = _SYNC_METHODS.get(entity)
    if method_name is None:
        raise ValueError(f"Unknown entity: {entity}")

    sync_method = getattr(connector, method_name, None)
    if sync_method is None:
        raise ValueError(f"{connector.name} has no method {method_name}")

    company_id = str(company_id)
    effective_direction = entity_direction(entity)
    async with connector_execution(company_id, connector, purpose, [_resource_of(entity)]) as ex:
        started_at = datetime.now(timezone.utc)
        run_id = await _begin_run(company_id, connector.name, entity, effective_direction.value, started_at)
        try:
            async with get_session_ctx() as session:
                config = await session.scalar(sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == company_id, ConnectorConfig.connector == connector.name))
                directions = dict(config.entity_directions or {})
                orders_from = config.orders_from
            if not (entity_allowed(connector, entity, directions)
                    or _links_products(connector, entity, directions, first_sync)):
                try:
                    entity_enum = SyncEntity(entity)
                except ValueError:
                    entity_enum = entity
                result = SyncResult(
                    entity=entity_enum,
                    direction=effective_direction,
                    errors=[f"{entity} sync blocked by direction setting"],
                    final=True,
                )
            else:
                if since is None and entity not in OUTBOUND_ENTITIES and use_watermark:
                    since = await _last_success_watermark(company_id, connector.name, entity)
                # Its own short transaction: the sync below runs holding no lock on the company.
                await ensure_store_bound(company_id, connector, ex.ctx)
                if entity in OUTBOUND_ENTITIES:
                    result = await sync_method(ex.ctx)
                else:
                    accepts = inspect.signature(sync_method).parameters
                    options: dict = {}
                    if "attention" in accepts:
                        options["attention"] = await attention_entries(company_id, connector.name, entity)
                    if reconcile and "reconcile" in accepts:
                        options["reconcile"] = True
                    if "orders_from" in accepts:
                        options["orders_from"] = orders_from
                    if first_sync is not None and "first_sync" in accepts:
                        options["first_sync"] = first_sync
                    result = await sync_method(ex.ctx, since=since, **options)
        except ExecutionError:
            raise
        except (ConnectorStoreChangedError, ConnectorUpgradeRequired, ConnectRequired, ModuleRefused) as exc:
            result = SyncResult(entity=entity, direction=effective_direction, errors=[str(exc)],
                                busy=isinstance(exc, ModuleBusy), final=not isinstance(exc, ModuleBusy))
        except NotImplementedError:
            result = SyncResult(
                entity=entity,
                direction=effective_direction,
                errors=[f"{connector.name} does not support {entity} sync"],
                final=True,
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
            company_id,
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
            company_id,
            status,
            result.created,
            result.updated,
            result.skipped,
            len(result.errors or []),
        )

        return result

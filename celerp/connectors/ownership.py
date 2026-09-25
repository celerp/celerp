# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Connector configuration ownership."""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import ensure_instance_id
from celerp.models.company import Company
from celerp.models.connector_config import ConnectorConfig


class ConnectorOwnershipError(RuntimeError):
    """Connector configuration is unavailable for this company."""


class ConnectorOwnershipAmbiguousError(ConnectorOwnershipError):
    """Connector ownership is inconsistent."""


class ConnectorBusyError(ConnectorOwnershipError):
    """Ownership cannot change while connector work holds the shared fence."""


class ConnectorStoreChangedError(ConnectorOwnershipError):
    """The store differs from the one this company's imported records came from."""


# Bounded wait for the exclusive ownership fence. Syncs and webhooks hold the
# fence shared for as long as their remote calls take; an ownership change
# waits this long behind them, then reports busy instead of stalling a request.
OWNER_LOCK_TIMEOUT_MS = 5000
_LOCK_NOT_AVAILABLE = "55P03"


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def _resolve_connector_owner(
    rows: list[ConnectorConfig], company_id
) -> ConnectorConfig | None:
    """Resolve one configured owner."""
    company_id = str(company_id)
    legacy_id = ensure_instance_id()
    legacy = [row for row in rows if str(row.company_id) == legacy_id]
    owners = {
        str(row.company_id)
        for row in rows
        if str(row.company_id) != legacy_id
    }
    if len(owners) > 1 or (legacy and owners):
        raise ConnectorOwnershipAmbiguousError(
            "This connector is linked to more than one company; disconnect it in Settings before retrying"
        )
    if owners:
        owner_id = next(iter(owners))
        if owner_id != company_id:
            raise ConnectorOwnershipError(
                "Connector is not connected to the current company"
            )
        return next(row for row in rows if str(row.company_id) == company_id)
    if legacy:
        raise ConnectorOwnershipAmbiguousError(
            "This connector is linked to more than one company; disconnect it in Settings before retrying"
        )
    return None


async def lock_connector_runtime(session: AsyncSession) -> None:
    """Join the shared connector-runtime fence for this transaction."""
    if session.get_bind().dialect.name == "sqlite":
        return
    await session.execute(
        sa.text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:k, 0))"),
        {"k": "connector-runtime"},
    )


async def lock_connector_maintenance(session: AsyncSession) -> None:
    """Wait for connector work to finish and exclude new work."""
    if session.get_bind().dialect.name == "sqlite":
        return
    await session.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": "connector-runtime"},
    )


@asynccontextmanager
async def connector_maintenance_guard():
    """Exclude connector work across a multi-transaction maintenance operation."""
    from celerp.db import LifecycleSessionLocal

    async with LifecycleSessionLocal() as session:
        if session.get_bind().dialect.name == "sqlite":
            yield
            return
        await session.execute(
            sa.text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"),
            {"k": "connector-runtime"},
        )
        try:
            yield
        finally:
            await session.execute(
                sa.text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"),
                {"k": "connector-runtime"},
            )
            await session.rollback()


async def _lock_active_company(
    session: AsyncSession, company_id, *, for_update: bool = True
) -> Company:
    try:
        cid = uuid.UUID(str(company_id))
    except (TypeError, ValueError) as exc:
        raise ConnectorOwnershipError("Connector company is invalid") from exc
    company = await session.get(
        Company, cid, with_for_update=for_update, populate_existing=True
    )
    if company is None or not company.is_active:
        raise ConnectorOwnershipError("Connector company is inactive")
    return company


async def _connector_rows(
    session: AsyncSession, connector: str, *, for_update: bool
) -> list[ConnectorConfig]:
    query = sa.select(ConnectorConfig).where(ConnectorConfig.connector == connector)
    if for_update:
        query = query.with_for_update()
    return list((await session.execute(query)).scalars().all())


async def lock_connector_key(
    session: AsyncSession, connector: str, *, exclusive: bool = False
) -> None:
    """Join the per-connector ownership fence for this transaction.

    Connector work (syncs, webhooks, outbound pushes, settings) holds the fence
    SHARED, so it runs concurrently and only excludes ownership changes.
    Connecting and disconnecting hold it EXCLUSIVE behind a bounded wait
    and raise ConnectorBusyError when work is still running."""
    await lock_connector_runtime(session)
    if session.get_bind().dialect.name == "sqlite":
        return
    key = {"k": f"connector-owner:{connector}"}
    if not exclusive:
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:k, 0))"), key
        )
        return
    await session.execute(sa.text(f"SET LOCAL lock_timeout = '{OWNER_LOCK_TIMEOUT_MS}ms'"))
    try:
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), key
        )
    except DBAPIError as exc:
        if _sqlstate(exc) == _LOCK_NOT_AVAILABLE:
            raise ConnectorBusyError(
                f"{connector} is busy syncing; try again in a moment"
            ) from exc
        raise
    await session.execute(sa.text("SET LOCAL lock_timeout = DEFAULT"))


async def lock_connector_operation(
    session: AsyncSession,
    company_id,
    connector: str,
    *,
    require_owner: bool = False,
    exclusive: bool = False,
) -> ConnectorConfig | None:
    """Resolve the current company's connector row behind the ownership fence.

    Shared (default) for connector work; exclusive for callers that go on to
    change ownership in the same transaction."""
    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY
    from celerp.models.sync_run import SyncRun

    company_id = str(company_id)
    await lock_connector_key(session, connector, exclusive=exclusive)
    await _lock_active_company(session, company_id, for_update=exclusive)
    rows = await _connector_rows(session, connector, for_update=exclusive)
    current = _resolve_connector_owner(rows, company_id)
    if current is not None:
        return current
    if require_owner:
        raise ConnectorOwnershipError(
            f"{connector} is not connected to the current company"
        )
    reset_at = await session.scalar(
        sa.select(sa.func.max(SyncRun.started_at)).where(
            SyncRun.company_id == company_id,
            SyncRun.connector == connector,
            SyncRun.entity == CONNECTOR_RESET_ENTITY,
        )
    )
    if reset_at is not None:
        raise ConnectorOwnershipError(
            f"{connector} is not connected to the current company"
        )
    return None


async def claim_connector_ownership(
    session: AsyncSession,
    company_id,
    connector: str,
    *,
    create: bool = True,
    default_sync_frequency: str | None = None,
    report_created: bool = False,
):
    """Connect one connector to one company."""
    company_id = str(company_id)
    legacy_id = ensure_instance_id()
    await lock_connector_key(session, connector, exclusive=True)
    await _lock_active_company(session, company_id)
    rows = await _connector_rows(session, connector, for_update=True)
    others = {
        str(row.company_id)
        for row in rows
        if str(row.company_id) not in {company_id, legacy_id}
    }
    if others:
        raise ConnectorOwnershipError(
            f"{connector} is already connected to another company"
        )

    current = next((r for r in rows if str(r.company_id) == company_id), None)
    legacy = next((r for r in rows if str(r.company_id) == legacy_id), None)
    if current is not None and legacy is not None and current is not legacy:
        if (
            current.webhook_secret
            and legacy.webhook_secret
            and current.webhook_secret != legacy.webhook_secret
        ):
            raise ConnectorOwnershipError(
                f"{connector} has conflicting legacy webhook secrets; manual reconciliation is required"
            )
        merged_ids = list(dict.fromkeys(current.webhook_ids + legacy.webhook_ids))
        current.webhook_ids_json = json.dumps(merged_ids) if merged_ids else None
        if not current.webhook_secret:
            current.webhook_secret = legacy.webhook_secret
        if (
            legacy.last_daily_sync_at is not None
            and (
                current.last_daily_sync_at is None
                or legacy.last_daily_sync_at > current.last_daily_sync_at
            )
        ):
            current.last_daily_sync_at = legacy.last_daily_sync_at
        await session.delete(legacy)
    elif current is None and legacy is not None:
        legacy.company_id = company_id
        current = legacy

    created = current is None and legacy is None and create
    if current is None and create:
        current = ConnectorConfig(
            company_id=company_id,
            connector=connector,
            **(
                {"sync_frequency": default_sync_frequency}
                if default_sync_frequency is not None else {}
            ),
        )
        session.add(current)

    if current is not None:
        await session.flush()
    return (current, created) if report_created else current


OWNERSHIP_OWNED = "owned"
OWNERSHIP_NONE = "none"
OWNERSHIP_OTHER = "other"
OWNERSHIP_AMBIGUOUS = "ambiguous"


async def connector_ownership_state(
    session: AsyncSession, company_id, connector: str
) -> str:
    """Unlocked read of where this connector's configuration stands for one
    company: owned, none, other (another company uses it) or ambiguous
    (linked to more than one company)."""
    rows = await _connector_rows(session, connector, for_update=False)
    try:
        current = _resolve_connector_owner(rows, company_id)
    except ConnectorOwnershipAmbiguousError:
        return OWNERSHIP_AMBIGUOUS
    except ConnectorOwnershipError:
        return OWNERSHIP_OTHER
    return OWNERSHIP_OWNED if current is not None else OWNERSHIP_NONE


async def connector_owned_by_company(
    session: AsyncSession, company_id, connector: str
) -> bool:
    """Check whether a connector configuration belongs to this company."""
    company = await session.get(Company, uuid.UUID(str(company_id)))
    if company is None or not company.is_active:
        return False
    return await connector_ownership_state(session, company_id, connector) == OWNERSHIP_OWNED


RESET_STATUS_DISCONNECTED = "reset"
RESET_STATUS_DEACTIVATED = "deactivated"


async def company_has_connector_claim(
    session: AsyncSession, company_id, connector: str
) -> bool:
    """Whether this company holds its own configuration row for the connector."""
    return await session.scalar(
        sa.select(sa.exists().where(
            ConnectorConfig.connector == connector,
            ConnectorConfig.company_id == str(company_id),
        ))
    ) is True


async def connectors_awaiting_reconnect(session: AsyncSession, company_id) -> list[str]:
    """Connectors whose latest reset for this company came from a deactivation
    and that have not been reconnected since."""
    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY
    from celerp.models.sync_run import SyncRun

    company_id = str(company_id)
    latest = (
        sa.select(SyncRun.connector, sa.func.max(SyncRun.started_at).label("started_at"))
        .where(
            SyncRun.company_id == company_id,
            SyncRun.entity == CONNECTOR_RESET_ENTITY,
        )
        .group_by(SyncRun.connector)
        .subquery()
    )
    rows = await session.execute(
        sa.select(SyncRun.connector)
        .join(latest, sa.and_(
            SyncRun.connector == latest.c.connector,
            SyncRun.started_at == latest.c.started_at,
        ))
        .where(
            SyncRun.company_id == company_id,
            SyncRun.entity == CONNECTOR_RESET_ENTITY,
            SyncRun.status == RESET_STATUS_DEACTIVATED,
            sa.not_(sa.exists().where(
                ConnectorConfig.connector == SyncRun.connector,
                ConnectorConfig.company_id == company_id,
            )),
        )
        .order_by(SyncRun.connector)
    )
    return sorted({row[0] for row in rows})


def record_connector_reset(
    session: AsyncSession, company_id, connector: str, *,
    status: str = RESET_STATUS_DISCONNECTED,
) -> None:
    """Fence config-less work until this connector is explicitly reconnected.
    The status records why: an explicit disconnect, or a company deactivation."""
    from datetime import datetime, timezone

    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY
    from celerp.models.sync_run import SyncRun

    now = datetime.now(timezone.utc)
    session.add(SyncRun(
        company_id=str(company_id),
        connector=connector,
        entity=CONNECTOR_RESET_ENTITY,
        direction="inbound",
        started_at=now,
        finished_at=now,
        created_count=0,
        updated_count=0,
        skipped_count=0,
        errors_json=None,
        status=status,
    ))


async def connector_release_scope(
    session: AsyncSession, company_id, connector: str
) -> list[ConnectorConfig]:
    """The rows a disconnect by this company releases: its own row plus any
    legacy row with no company. Raises when this company holds no row."""
    company_id = str(company_id)
    legacy_id = ensure_instance_id()
    rows = await _connector_rows(session, connector, for_update=True)
    mine = [row for row in rows if str(row.company_id) in {company_id, legacy_id}]
    if not any(str(row.company_id) == company_id for row in mine):
        raise ConnectorOwnershipError(
            f"{connector} is not connected to the current company"
        )
    return mine


async def release_connector_ownership(
    session: AsyncSession, company_id, connector: str
) -> None:
    """Release one company's connector state: its own row plus any legacy
    row with no company, so a disconnect also works while ownership is ambiguous."""
    from celerp.models.connector_config import OutboundQueue

    company_id = str(company_id)
    legacy_id = ensure_instance_id()
    await lock_connector_key(session, connector, exclusive=True)
    mine = await connector_release_scope(session, company_id, connector)

    await session.execute(
        sa.delete(OutboundQueue).where(
            OutboundQueue.company_id.in_({company_id, legacy_id}),
            OutboundQueue.connector == connector,
        )
    )
    record_connector_reset(session, company_id, connector)
    for row in mine:
        await session.delete(row)
    await session.flush()


# Imported records a connector reads back from the store to confirm it is the
# store they came from.
STORE_PROOF_SAMPLE = 20


async def bind_connector_store(session: AsyncSession, company_id, connector, ctx) -> None:
    """Tie a company's imported records from one connector to the store they
    came from. Stores that number orders, customers and products per store
    would overwrite those records, so a store is accepted only when no records
    exist yet or when it still holds a sample of them. Each new connection is
    checked once, even when the store address is unchanged. Caller commits."""
    from datetime import datetime, timezone

    from celerp.models.connector_source import ConnectorSource
    from celerp.models.projections import Projection

    store_handle = ctx.store_handle
    if not connector.store_scoped_ids or not store_handle:
        return
    await _lock_active_company(session, company_id)
    cid = uuid.UUID(str(company_id))
    key = (str(cid), connector.name)
    source = await session.get(ConnectorSource, key, populate_existing=True)
    config_id = await session.scalar(sa.select(ConnectorConfig.id).where(
        ConnectorConfig.company_id == key[0],
        ConnectorConfig.connector == connector.name,
    ))
    if (source is not None and source.store_handle == store_handle
            and source.config_id == config_id):
        return

    async def sample(*conditions) -> list[dict]:
        return list((await session.scalars(
            sa.select(Projection.state)
            .where(Projection.company_id == cid, *conditions)
            .order_by(Projection.created_at.desc().nulls_last())
            .limit(STORE_PROOF_SAMPLE)
        )).all())

    # Orders and invoices prove the store best, then customers, then products.
    prefix = f"{connector.name}:%"
    samples = (
        await sample(Projection.entity_id.like(f"doc:{prefix}"))
        or await sample(Projection.entity_id.like(f"contact:{prefix}"))
        or await sample(
            Projection.entity_type == "item",
            Projection.state.op("->>")("idempotency_key").like(prefix),
        )
    )
    if samples:
        origin = f" from {source.store_handle}" if source is not None else ""
        try:
            confirmed = await connector.same_store(ctx, samples)
        except Exception as exc:
            raise ConnectorStoreChangedError(
                f"Could not read {store_handle} to confirm this company's "
                f"{connector.display_name} records came from it. Try again."
            ) from exc
        if not confirmed:
            raise ConnectorStoreChangedError(
                f"This company already has {connector.display_name} records"
                f"{origin}, and {store_handle} does not have them. Connect the store they "
                f"came from, or use a separate company for {store_handle}."
            )

    now = datetime.now(timezone.utc)
    if source is None:
        session.add(ConnectorSource(
            company_id=key[0], connector=key[1], store_handle=store_handle,
            bound_at=now, config_id=config_id,
        ))
    else:
        source.store_handle = store_handle
        source.bound_at = now
        source.config_id = config_id

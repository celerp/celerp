# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Canonical ownership for installation-scoped connector credentials."""
from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import ensure_instance_id
from celerp.models.connector_config import ConnectorConfig


class ConnectorOwnershipError(RuntimeError):
    """The installation-scoped platform credential is not owned by this company."""


class ConnectorOwnershipAmbiguousError(ConnectorOwnershipError):
    """Connector ownership is inconsistent and must be reconciled before use."""


def _resolve_connector_owner(
    rows: list[ConnectorConfig], company_id
) -> ConnectorConfig | None:
    """Resolve one installation-scoped owner, failing closed on legacy/corrupt ambiguity."""
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
            "Connector ownership is ambiguous; reconcile the installation before retrying"
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
            "Connector ownership is ambiguous; reconcile the installation before retrying"
        )
    return None


async def _lock_connector_key(session: AsyncSession, connector: str) -> None:
    await session.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"connector-owner:{connector}"},
    )


async def lock_connector_operation(
    session: AsyncSession,
    company_id,
    connector: str,
    *,
    require_owner: bool = False,
) -> ConnectorConfig | None:
    """Serialize a connector operation with ownership changes."""
    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY
    from celerp.models.sync_run import SyncRun

    company_id = str(company_id)
    await _lock_connector_key(session, connector)
    rows = (await session.execute(
        sa.select(ConnectorConfig)
        .where(ConnectorConfig.connector == connector)
        .with_for_update()
    )).scalars().all()
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
    """Atomically claim one installation/platform credential for one ERP company."""
    company_id = str(company_id)
    legacy_id = ensure_instance_id()
    await _lock_connector_key(session, connector)
    rows = (await session.execute(
        sa.select(ConnectorConfig)
        .where(ConnectorConfig.connector == connector)
        .with_for_update()
    )).scalars().all()
    others = {
        str(row.company_id)
        for row in rows
        if str(row.company_id) not in {company_id, legacy_id}
    }
    if others:
        raise ConnectorOwnershipError(
            f"{connector} is already connected to another company on this installation"
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


async def connector_owned_by_company(
    session: AsyncSession, company_id, connector: str
) -> bool:
    """Fail-closed ownership check for relay credential reads/revocation."""
    rows = (await session.execute(
        sa.select(ConnectorConfig).where(ConnectorConfig.connector == connector)
    )).scalars().all()
    try:
        return _resolve_connector_owner(rows, company_id) is not None
    except ConnectorOwnershipError:
        return False


async def release_connector_ownership(
    session: AsyncSession, company_id, connector: str
) -> None:
    """Release one company's connector state after remote revocation is confirmed."""
    from datetime import datetime, timezone

    from celerp.models.connector_config import OutboundQueue
    from celerp.models.sync_run import SyncRun
    from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY

    company_id = str(company_id)
    await _lock_connector_key(session, connector)
    rows = (await session.execute(
        sa.select(ConnectorConfig)
        .where(ConnectorConfig.connector == connector)
        .with_for_update()
    )).scalars().all()
    current = _resolve_connector_owner(rows, company_id)
    if current is None:
        raise ConnectorOwnershipError(
            f"{connector} is not connected to the current company"
        )

    await session.execute(
        sa.delete(OutboundQueue).where(
            OutboundQueue.company_id == company_id,
            OutboundQueue.connector == connector,
        )
    )
    now = datetime.now(timezone.utc)
    session.add(SyncRun(
        company_id=company_id,
        connector=connector,
        entity=CONNECTOR_RESET_ENTITY,
        direction="inbound",
        started_at=now,
        finished_at=now,
        created_count=0,
        updated_count=0,
        skipped_count=0,
        errors_json=None,
        status="reset",
    ))
    await session.delete(current)
    await session.flush()

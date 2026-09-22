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
    """The installation-scoped platform credential already belongs elsewhere."""


async def claim_connector_ownership(
    session: AsyncSession,
    company_id,
    connector: str,
    *,
    create: bool = True,
    default_sync_frequency: str | None = None,
) -> ConnectorConfig | None:
    """Atomically claim one installation/platform credential for one ERP company."""
    company_id = str(company_id)
    legacy_id = ensure_instance_id()
    await session.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"connector-owner:{connector}"},
    )
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
    return current


async def connector_owned_by_company(
    session: AsyncSession, company_id, connector: str
) -> bool:
    """Fail-closed ownership check for relay credential reads/revocation."""
    company_id = str(company_id)
    legacy_id = ensure_instance_id()
    company_ids = (await session.execute(
        sa.select(ConnectorConfig.company_id).where(
            ConnectorConfig.connector == connector
        )
    )).scalars().all()
    owners = {str(value) for value in company_ids if str(value) != legacy_id}
    return owners == {company_id}

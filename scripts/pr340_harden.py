#!/usr/bin/env python3
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path.cwd()


def read(path: str) -> str:
    return (ROOT / path).read_text()


def write(path: str, content: str) -> None:
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def replace_once(path: str, old: str, new: str) -> None:
    s = read(path)
    n = s.count(old)
    if n != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {n}: {old[:120]!r}")
    write(path, s.replace(old, new, 1))


def regex_once(path: str, pattern: str, repl: str, flags: int = re.S) -> None:
    s = read(path)
    out, n = re.subn(pattern, repl, s, count=1, flags=flags)
    if n != 1:
        raise SystemExit(f"{path}: expected exactly one regex match, found {n}: {pattern[:120]!r}")
    write(path, out)


# ---------------------------------------------------------------------------
# Connector ownership: one installation/platform credential -> one ERP company.
# ---------------------------------------------------------------------------
write("celerp/connectors/ownership.py", r'''# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Local ownership lifecycle for installation-scoped connector credentials."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from celerp.config import ensure_instance_id
from celerp.db import get_session_ctx
from celerp.models.company import Company
from celerp.models.connector_config import ConnectorConfig

log = logging.getLogger(__name__)


class ConnectorOwnershipError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _lock(session, connector: str) -> None:
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"connector-owner:{connector}"},
        )


async def _real_company_ids(session) -> set[str]:
    return {str(v) for v in (await session.execute(sa.select(Company.id))).scalars().all()}


def _merge_metadata(current: ConnectorConfig, legacy: ConnectorConfig) -> None:
    if (
        current.webhook_secret
        and legacy.webhook_secret
        and current.webhook_secret != legacy.webhook_secret
    ):
        raise ConnectorOwnershipError(
            f"{current.connector} has conflicting legacy webhook secrets; "
            "manual connector reconciliation is required"
        )
    current.webhook_secret = current.webhook_secret or legacy.webhook_secret
    current.webhook_ids = list(dict.fromkeys(current.webhook_ids + legacy.webhook_ids))
    current.claimed_at = current.claimed_at or legacy.claimed_at or _now()
    current.activated_at = current.activated_at or legacy.activated_at


async def claim_connector(
    company_id: str,
    connector: str,
    *,
    sync_frequency: str | None = None,
) -> ConnectorConfig:
    """Atomically claim one installation-scoped platform for exactly one ERP company."""
    company_id = str(company_id)
    legacy_id = ensure_instance_id()
    async with get_session_ctx() as session:
        await _lock(session, connector)
        try:
            company_uuid = uuid.UUID(company_id)
        except ValueError as exc:
            raise ConnectorOwnershipError("Invalid company id") from exc
        if await session.get(Company, company_uuid) is None:
            raise ConnectorOwnershipError("Company not found")

        rows = (await session.execute(
            sa.select(ConnectorConfig).where(ConnectorConfig.connector == connector)
        )).scalars().all()
        real_ids = await _real_company_ids(session)
        other = [
            r for r in rows
            if str(r.company_id) in real_ids and str(r.company_id) != company_id
        ]
        if other:
            raise ConnectorOwnershipError(
                "This connector is already owned by another company on this installation"
            )

        current = next((r for r in rows if str(r.company_id) == company_id), None)
        legacy = next((r for r in rows if str(r.company_id) == legacy_id), None)

        if current is not None and legacy is not None and legacy is not current:
            _merge_metadata(current, legacy)
            await session.delete(legacy)

        if current is None and legacy is not None:
            legacy.company_id = company_id
            # An explicit adoption is a new responsibility boundary. It remains
            # pending until credential/webhook setup succeeds.
            legacy.claimed_at = _now()
            legacy.activated_at = None
            current = legacy

        if current is None:
            current = ConnectorConfig(
                company_id=company_id,
                connector=connector,
                sync_frequency=sync_frequency or "realtime",
                claimed_at=_now(),
                activated_at=None,
            )
            session.add(current)
        else:
            current.claimed_at = current.claimed_at or _now()
            current.activated_at = None
            if sync_frequency and not current.sync_frequency:
                current.sync_frequency = sync_frequency

        await session.commit()
        await session.refresh(current)
        return current


async def activate_connector(company_id: str, connector: str) -> ConnectorConfig:
    """Mark a claimed connector safe for autonomous sync/webhook/queue work."""
    company_id = str(company_id)
    async with get_session_ctx() as session:
        await _lock(session, connector)
        row = await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == company_id,
            ConnectorConfig.connector == connector,
        ).limit(1))
        if row is None:
            raise ConnectorOwnershipError("Connector has not been claimed by this company")
        real_ids = await _real_company_ids(session)
        other = await session.scalar(sa.select(ConnectorConfig.id).where(
            ConnectorConfig.connector == connector,
            ConnectorConfig.activated_at.is_not(None),
            ConnectorConfig.company_id.in_(real_ids - {company_id}),
        ).limit(1)) if real_ids - {company_id} else None
        if other is not None:
            raise ConnectorOwnershipError(
                "This connector is already active for another company on this installation"
            )
        row.claimed_at = row.claimed_at or _now()
        row.activated_at = row.activated_at or _now()
        await session.commit()
        await session.refresh(row)
        return row


async def get_connector_config(
    company_id: str, connector: str, *, adopt_single_company: bool = True
) -> ConnectorConfig | None:
    company_id = str(company_id)
    async with get_session_ctx() as session:
        row = await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == company_id,
            ConnectorConfig.connector == connector,
        ).limit(1))
        if row is not None:
            return row
    if adopt_single_company:
        await adopt_single_company_legacy_configs(connector=connector)
        async with get_session_ctx() as session:
            return await session.scalar(sa.select(ConnectorConfig).where(
                ConnectorConfig.company_id == company_id,
                ConnectorConfig.connector == connector,
            ).limit(1))
    return None


async def get_active_connector_config(company_id: str, connector: str) -> ConnectorConfig | None:
    async with get_session_ctx() as session:
        return await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id),
            ConnectorConfig.connector == connector,
            ConnectorConfig.activated_at.is_not(None),
        ).limit(1))


async def get_active_connector_owner(connector: str) -> ConnectorConfig | None:
    """Resolve one active real-company owner, failing closed on corrupt duplicates."""
    async with get_session_ctx() as session:
        real_ids = await _real_company_ids(session)
        if not real_ids:
            return None
        rows = (await session.execute(sa.select(ConnectorConfig).where(
            ConnectorConfig.connector == connector,
            ConnectorConfig.activated_at.is_not(None),
            ConnectorConfig.company_id.in_(real_ids),
        ))).scalars().all()
        if len(rows) > 1:
            raise ConnectorOwnershipError(
                f"{connector} is active for multiple companies; autonomous sync is disabled"
            )
        return rows[0] if rows else None


async def active_company_ids() -> list[str]:
    async with get_session_ctx() as session:
        real_ids = await _real_company_ids(session)
        if not real_ids:
            return []
        rows = (await session.execute(
            sa.select(ConnectorConfig.company_id)
            .where(
                ConnectorConfig.activated_at.is_not(None),
                ConnectorConfig.company_id.in_(real_ids),
            )
            .distinct()
        )).scalars().all()
        return [str(v) for v in rows]


async def delete_connector_config(company_id: str, connector: str) -> None:
    async with get_session_ctx() as session:
        await _lock(session, connector)
        await session.execute(sa.delete(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id),
            ConnectorConfig.connector == connector,
        ))
        await session.commit()


async def adopt_single_company_legacy_configs(*, connector: str | None = None) -> None:
    """Adopt legacy installation-keyed rows only when the database has one company."""
    legacy_id = ensure_instance_id()
    async with get_session_ctx() as session:
        companies = (await session.execute(sa.select(Company.id).limit(2))).scalars().all()
    if len(companies) != 1:
        return
    company_id = str(companies[0])
    if company_id == legacy_id:
        return

    async with get_session_ctx() as session:
        query = sa.select(ConnectorConfig.connector).where(
            ConnectorConfig.company_id == legacy_id
        ).distinct()
        if connector:
            query = query.where(ConnectorConfig.connector == connector)
        names = [str(v) for v in (await session.execute(query)).scalars().all()]

    for name in names:
        try:
            async with get_session_ctx() as session:
                await _lock(session, name)
                legacy = await session.scalar(sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == legacy_id,
                    ConnectorConfig.connector == name,
                ).limit(1))
                if legacy is None:
                    continue
                current = await session.scalar(sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == company_id,
                    ConnectorConfig.connector == name,
                ).limit(1))
                if current is None:
                    legacy.company_id = company_id
                    legacy.claimed_at = legacy.claimed_at or _now()
                    legacy.activated_at = legacy.activated_at or _now()
                else:
                    _merge_metadata(current, legacy)
                    current.activated_at = current.activated_at or _now()
                    await session.delete(legacy)
                await session.commit()
        except ConnectorOwnershipError as exc:
            # Startup recovery must never destroy conflicting recovery metadata or
            # prevent the application from booting.
            log.error("connector legacy adoption blocked for %s: %s", name, exc)
''')

write("celerp/connectors/operation_lock.py", r'''# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Cross-process lease for remote connector operations."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import sqlalchemy as sa

from celerp.db import engine


class ConnectorBusy(RuntimeError):
    pass


_local_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def connector_operation(company_id: str, connector: str):
    """Try to own one company/platform remote operation for the duration of the block.

    PostgreSQL session advisory locks release automatically if the process/connection
    dies. The non-Postgres path is an in-process equivalent used by lightweight tests.
    This is deliberately a try-lock: background work should back off rather than pin a
    scarce DB connection waiting behind slow remote HTTP.
    """
    key = f"connector-op:{company_id}:{connector}"
    if lifecycle_engine.dialect.name != "postgresql":
        lock = _local_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise ConnectorBusy(f"{connector} sync is already in progress")
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            _local_locks.pop(key, None)
        return

    async with lifecycle_engine.connect() as conn:
        acquired = bool(await conn.scalar(
            sa.text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"),
            {"k": key},
        ))
        if not acquired:
            raise ConnectorBusy(f"{connector} sync is already in progress")
        try:
            yield
        finally:
            await conn.execute(
                sa.text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"),
                {"k": key},
            )
''')

ownership = read("celerp/connectors/ownership.py")
marker = '''async def get_connector_config(
'''
if marker not in ownership:
    raise SystemExit("ownership: get_connector_config marker missing")
ownership = ownership.replace(marker, '''async def mark_connector_pending(company_id: str, connector: str) -> None:
    """Suspend autonomous work without changing the original operational cutoff."""
    async with get_session_ctx() as session:
        await _lock(session, connector)
        row = await session.scalar(sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == str(company_id),
            ConnectorConfig.connector == connector,
        ).limit(1))
        if row is not None:
            row.activated_at = None
            await session.commit()


async def get_connector_config(
''', 1)
write("celerp/connectors/ownership.py", ownership)

# ConnectorConfig lifecycle timestamps.
replace_once(
    "celerp/models/connector_config.py",
    '    last_daily_sync_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)\n',
    '    last_daily_sync_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)\n'
    '    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)\n'
    '    activated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)\n',
)

# Migration from the actual candidate's single Alembic head.
down = os.environ.get("DOWN_REVISION", "").strip()
if not re.fullmatch(r"[0-9a-z]+", down):
    raise SystemExit(f"Invalid/missing DOWN_REVISION: {down!r}")
write("celerp/migrations/versions/c3d4e5f6a7b8_connector_ownership_lifecycle.py", f'''# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Add connector ownership lifecycle timestamps.

Revision ID: c3d4e5f6a7b8
Revises: {down}
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("connector_configs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("connector_configs", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
    # Existing connector rows predate the lifecycle distinction. Treat upgrade time
    # as their responsibility boundary; startup adoption later excludes unresolved
    # installation-keyed rows from autonomous real-company work.
    op.execute(
        "UPDATE connector_configs "
        "SET claimed_at = CURRENT_TIMESTAMP, activated_at = CURRENT_TIMESTAMP "
        "WHERE claimed_at IS NULL OR activated_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("connector_configs", "activated_at")
    op.drop_column("connector_configs", "claimed_at")
''')

# ---------------------------------------------------------------------------
# Connector sync runner: exact final implementation from the verified PR head.
# ---------------------------------------------------------------------------
write("celerp/connectors/sync_runner.py", "# Copyright (c) 2026 Noah Severs\n# SPDX-License-Identifier: BUSL-1.1\n\"\"\"Sync runner - wraps connector sync calls with audit trail recording.\"\"\"\nfrom __future__ import annotations\n\nimport json\nimport logging\nfrom datetime import datetime, timezone\n\nfrom celerp.connectors.base import (\n    ConnectorBase,\n    ConnectorContext,\n    SyncDirection,\n    SyncEntity,\n    SyncResult,\n    entity_allowed,\n)\nfrom celerp.models.sync_run import SyncRun\n\nlog = logging.getLogger(__name__)\n\n\nasync def _interrupt_abandoned_runs(company_id: str, connector: str) -> None:\n    \"\"\"Close audit rows left running after the process that owned the lease died.\"\"\"\n    import sqlalchemy as sa\n    from celerp.db import get_session_ctx\n\n    now = datetime.now(timezone.utc)\n    async with get_session_ctx() as session:\n        await session.execute(\n            sa.update(SyncRun)\n            .where(\n                SyncRun.company_id == company_id,\n                SyncRun.connector == connector,\n                SyncRun.finished_at.is_(None),\n            )\n            .values(\n                finished_at=now,\n                status=\"failed\",\n                errors_json=json.dumps([\"Interrupted before completion\"]),\n            )\n        )\n        await session.commit()\n\n_SYNC_METHODS = {\n    \"products\": \"sync_products\",\n    \"orders\": \"sync_orders\",\n    \"contacts\": \"sync_contacts\",\n    \"inventory\": \"sync_inventory\",\n    \"products_out\": \"sync_products_out\",\n    \"invoices_out\": \"sync_invoices_out\",\n    \"inventory_out\": \"sync_inventory_out\",\n}\n_OUTBOUND_ENTITIES = {\"products_out\", \"invoices_out\", \"inventory_out\"}\n_OUTBOUND_ENTITY_METHODS = {\n    \"products_out\": \"sync_products_out\",\n    \"invoices_out\": \"sync_invoices_out\",\n    \"inventory_out\": \"sync_inventory_out\",\n}\n\n\ndef supported_outbound(connector: ConnectorBase) -> list[str]:\n    \"\"\"Outbound entities implemented by this connector, in stable dispatch order.\"\"\"\n    return [\n        entity for entity, method in _OUTBOUND_ENTITY_METHODS.items()\n        if getattr(type(connector), method, None) is not getattr(ConnectorBase, method, None)\n    ]\n\n\ndef sync_plan(connector: ConnectorBase, direction: SyncDirection) -> list[str]:\n    \"\"\"One direction-aware plan shared by connect, manual, and reconciliation paths.\"\"\"\n    direction = direction if isinstance(direction, SyncDirection) else SyncDirection(direction)\n    plan: list[str] = []\n    if direction in (SyncDirection.INBOUND, SyncDirection.BOTH):\n        plan.extend(e.value for e in connector.supported_entities)\n    if direction in (SyncDirection.OUTBOUND, SyncDirection.BOTH):\n        plan.extend(supported_outbound(connector))\n    return plan\n\n\nasync def run_connector_sync(\n    connector: ConnectorBase,\n    ctx: ConnectorContext,\n    direction: SyncDirection,\n) -> list[SyncResult]:\n    \"\"\"Execute one canonical plan under the company/platform operation lease.\"\"\"\n    from celerp.connectors.operation_lock import ConnectorBusy, connector_operation\n\n    try:\n        async with connector_operation(ctx.company_id, connector.name):\n            await _interrupt_abandoned_runs(ctx.company_id, connector.name)\n            return [\n                await run_sync(\n                    connector, ctx, entity, direction=direction, _operation_locked=True\n                )\n                for entity in sync_plan(connector, direction)\n            ]\n    except ConnectorBusy:\n        first = sync_plan(connector, direction)\n        entity = first[0] if first else SyncEntity.PRODUCTS\n        return [SyncResult(\n            entity=entity,\n            direction=direction,\n            errors=[f\"{connector.name} sync already in progress\"],\n        )]\n\n\nasync def run_connector_activation(\n    connector: ConnectorBase,\n    ctx: ConnectorContext,\n) -> list[SyncResult]:\n    \"\"\"Pull a pending connector live without exposing an activation handoff race.\"\"\"\n    from celerp.connectors.operation_lock import ConnectorBusy, connector_operation\n    from celerp.connectors.ownership import (\n        activate_connector, get_connector_config, mark_connector_pending,\n    )\n\n    config = await get_connector_config(\n        ctx.company_id, connector.name, adopt_single_company=False\n    )\n    if config is None:\n        return [SyncResult(\n            entity=SyncEntity.PRODUCTS,\n            direction=SyncDirection.INBOUND,\n            errors=[f\"{connector.name} is not claimed by this company\"],\n        )]\n    if config.activated_at is not None:\n        return await run_connector_sync(connector, ctx, SyncDirection.INBOUND)\n\n    try:\n        async with connector_operation(ctx.company_id, connector.name):\n            await _interrupt_abandoned_runs(ctx.company_id, connector.name)\n            results = [\n                await run_sync(\n                    connector, ctx, entity,\n                    direction=SyncDirection.INBOUND, _operation_locked=True,\n                )\n                for entity in sync_plan(connector, SyncDirection.INBOUND)\n            ]\n            if any(result.errors for result in results):\n                return results\n\n            await activate_connector(ctx.company_id, connector.name)\n\n            if SyncEntity.ORDERS in connector.supported_entities:\n                catchup = await run_sync(\n                    connector, ctx, SyncEntity.ORDERS.value,\n                    direction=SyncDirection.INBOUND, _operation_locked=True,\n                )\n                results.append(catchup)\n                if catchup.errors:\n                    await mark_connector_pending(ctx.company_id, connector.name)\n            return results\n    except ConnectorBusy:\n        return [SyncResult(\n            entity=SyncEntity.PRODUCTS,\n            direction=SyncDirection.INBOUND,\n            errors=[f\"{connector.name} activation already in progress\"],\n        )]\n\n\nasync def _last_success_watermark(company_id: str, connector: str, entity: str):\n    \"\"\"The start time of the most recent FULLY successful sync for this\n    (company, connector, entity), used as the incremental `since`. Returns None on\n    the first sync or if it can't be read, which means a full pull.\n\n    Only fully-successful runs advance the cursor: a partial run left some records\n    errored, so the next run must re-pull from the last good point to retry them\n    rather than skipping past them.\"\"\"\n    import sqlalchemy as sa\n\n    from celerp.db import get_session_ctx\n\n    try:\n        async with get_session_ctx() as session:\n            return await session.scalar(\n                sa.select(sa.func.max(SyncRun.started_at)).where(\n                    SyncRun.company_id == company_id,\n                    SyncRun.connector == connector,\n                    SyncRun.entity == entity,\n                    SyncRun.status == \"success\",\n                )\n            )\n    except Exception as exc:\n        # A read failure degrades to a full re-pull (safe, dup-safe via idempotency\n        # keys) — but say so rather than silently widening every sync.\n        log.warning(\"Could not read sync watermark for %s.%s: %s — full pull\", connector, entity, exc)\n        return None\n\n\n# SyncRun is audit state only. The connector operation lease owns concurrency.\n_BUSY = \"busy\"\n\n\nasync def _begin_run(company_id: str, connector: str, entity: str, direction_str: str, started_at):\n    \"\"\"Insert an in-progress SyncRun (status=running, finished_at=None) so the UI can\n    observe an active sync, and return its id. Returns _BUSY if a recent unfinished run\n    for this (company, connector, entity) already exists (the concurrency guard). Returns\n    None if the row could not be written, in which case the sync still runs and a single\n    final row is written by _finish_run.\"\"\"\n    import sqlalchemy as sa\n\n    from celerp.db import get_session_ctx\n\n    try:\n        async with get_session_ctx() as session:\n            # Serialize check+insert itself. Without this lock, two workers can both\n            # observe no running row and each insert one.\n            await session.execute(\n                sa.text(\"SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))\"),\n                {\"k\": f\"sync-run:{company_id}:{connector}:{entity}\"},\n            )\n            existing = await session.scalar(\n                sa.select(SyncRun.id)\n                .where(\n                    SyncRun.company_id == company_id,\n                    SyncRun.connector == connector,\n                    SyncRun.entity == entity,\n                    SyncRun.finished_at.is_(None),\n                )\n                .limit(1)\n            )\n            if existing is not None:\n                return _BUSY\n            run = SyncRun(\n                company_id=company_id, connector=connector, entity=entity,\n                direction=direction_str, started_at=started_at, finished_at=None,\n                created_count=0, updated_count=0, skipped_count=0,\n                errors_json=None, status=\"running\",\n            )\n            session.add(run)\n            await session.commit()\n            await session.refresh(run)\n            return run.id\n    except Exception as exc:\n        log.warning(\"Failed to record in-progress SyncRun: %s\", exc)\n        return None\n\n\nasync def _finish_run(run_id, company_id, connector, entity, result, started_at, finished_at, status):\n    \"\"\"Update the in-progress row with the final outcome, or insert a final row if the\n    in-progress write failed (run_id is None).\"\"\"\n    import sqlalchemy as sa\n\n    from celerp.db import get_session_ctx\n\n    direction_str = result.direction.value if hasattr(result.direction, \"value\") else str(result.direction)\n    errors_json = json.dumps(result.errors) if result.errors else None\n    try:\n        async with get_session_ctx() as session:\n            if run_id is not None:\n                await session.execute(\n                    sa.update(SyncRun).where(SyncRun.id == run_id).values(\n                        direction=direction_str, finished_at=finished_at,\n                        created_count=result.created, updated_count=result.updated,\n                        skipped_count=result.skipped, errors_json=errors_json, status=status,\n                    )\n                )\n            else:\n                session.add(SyncRun(\n                    company_id=company_id, connector=connector, entity=entity,\n                    direction=direction_str, started_at=started_at, finished_at=finished_at,\n                    created_count=result.created, updated_count=result.updated,\n                    skipped_count=result.skipped, errors_json=errors_json, status=status,\n                ))\n            await session.commit()\n    except Exception as exc:\n        log.warning(\"Failed to record SyncRun: %s\", exc)\n\n\nasync def run_sync(\n    connector: ConnectorBase,\n    ctx: ConnectorContext,\n    entity: str,\n    since: datetime | None = None,\n    direction: SyncDirection | None = None,\n    _operation_locked: bool = False,\n) -> SyncResult:\n    \"\"\"Execute a sync operation and record a SyncRun audit entry.\n\n    If ``direction`` is provided, checks whether ``entity`` is allowed\n    for that direction before running. Returns a failed SyncResult if blocked.\n    \"\"\"\n    if not _operation_locked:\n        from celerp.connectors.operation_lock import ConnectorBusy, connector_operation\n        try:\n            async with connector_operation(ctx.company_id, connector.name):\n                await _interrupt_abandoned_runs(ctx.company_id, connector.name)\n                return await run_sync(\n                    connector, ctx, entity, since=since, direction=direction,\n                    _operation_locked=True,\n                )\n        except ConnectorBusy:\n            intended = direction if isinstance(direction, SyncDirection) else connector.direction\n            return SyncResult(\n                entity=entity,\n                direction=intended,\n                errors=[f\"{connector.name} sync already in progress\"],\n            )\n\n    # Direction gate\n    if direction and not entity_allowed(entity, direction):\n        try:\n            entity_enum = SyncEntity(entity)\n        except ValueError:\n            entity_enum = entity  # unknown entity, pass through\n        direction_enum = direction if isinstance(direction, SyncDirection) else SyncDirection(direction)\n        return SyncResult(\n            entity=entity_enum,\n            direction=direction_enum,\n            errors=[f\"{entity} sync blocked by direction={direction.value}\"],\n        )\n\n    method_name = _SYNC_METHODS.get(entity)\n    if method_name is None:\n        raise ValueError(f\"Unknown entity: {entity}\")\n\n    sync_method = getattr(connector, method_name, None)\n    if sync_method is None:\n        raise ValueError(f\"{connector.name} has no method {method_name}\")\n\n    started_at = datetime.now(timezone.utc)\n    intended_direction = direction if isinstance(direction, SyncDirection) else connector.direction\n    direction_str = intended_direction.value if hasattr(intended_direction, \"value\") else str(intended_direction)\n\n    # Mark this entity's sync as in progress (and refuse to start a second concurrent\n    # run for the same entity). The UI polls these rows for live status.\n    run_id = await _begin_run(ctx.company_id, connector.name, entity, direction_str, started_at)\n    if run_id == _BUSY:\n        return SyncResult(\n            entity=entity,\n            direction=intended_direction,\n            errors=[f\"{entity} sync already in progress\"],\n        )\n\n    # Incremental by default: pull only what changed since the last successful run\n    # for this entity. Idempotency keys make any overlap dup-safe.\n    if since is None and entity not in _OUTBOUND_ENTITIES:\n        since = await _last_success_watermark(ctx.company_id, connector.name, entity)\n\n    try:\n        if entity in _OUTBOUND_ENTITIES:\n            result = await sync_method(ctx)\n        else:\n            result = await sync_method(ctx, since=since)\n    except NotImplementedError:\n        result = SyncResult(\n            entity=entity,\n            direction=connector.direction,\n            errors=[f\"{connector.name} does not support {entity} sync\"],\n        )\n    except Exception as exc:\n        result = SyncResult(\n            entity=entity,\n            direction=connector.direction,\n            errors=[f\"Unexpected error: {exc}\"],\n        )\n\n    finished_at = datetime.now(timezone.utc)\n\n    if result.errors and result.created == 0 and result.updated == 0:\n        status = \"failed\"\n    elif result.errors:\n        status = \"partial\"\n    else:\n        status = \"success\"\n\n    await _finish_run(run_id, ctx.company_id, connector.name, entity, result, started_at, finished_at, status)\n\n    log.info(\n        \"sync_run %s.%s company=%s status=%s created=%d updated=%d skipped=%d errors=%d\",\n        connector.name, entity, ctx.company_id, status,\n        result.created, result.updated, result.skipped,\n        len(result.errors or []),\n    )\n\n    return result\n")

# ---------------------------------------------------------------------------
# Queue: no legacy ownership here; scope worker and serialize remote writes once.
# ---------------------------------------------------------------------------
regex_once(
    "celerp/connectors/outbound_queue.py",
    r'async def adopt_single_company_legacy_configs\(\) -> None:.*?\n\nasync def enqueue_item_change',
    'async def enqueue_item_change',
)
replace_once(
    "celerp/connectors/outbound_queue.py",
    "from celerp.models.company import Company\n",
    "",
)
replace_once(
    "celerp/connectors/outbound_queue.py",
    '''            ConnectorConfig.direction.in_([
                SyncDirection.OUTBOUND.value, SyncDirection.BOTH.value
            ]),
''',
    '''            ConnectorConfig.direction.in_([
                SyncDirection.OUTBOUND.value, SyncDirection.BOTH.value
            ]),
            ConnectorConfig.activated_at.is_not(None),
''',
)
replace_once(
    "celerp/connectors/outbound_queue.py",
    '''                OutboundQueue.status == "pending",
                sa.or_(
''',
    '''                OutboundQueue.connector == "woocommerce",
                OutboundQueue.entity_type == "inventory",
                OutboundQueue.status == "pending",
                sa.or_(
''',
)
# Replace per-identity transaction advisory lock with connector lease while preserving fresh row read.
regex_once(
    "celerp/connectors/outbound_queue.py",
    r'''    for company_id, connector_name, identity in identities:\n        async with get_session_ctx\(\) as session:\n            await session.execute\(\n                sa.text\("SELECT pg_advisory_xact_lock\(hashtextextended\(:k, 0\)\)"\),\n                \{"k": f"outbound:\{company_id\}:\{connector_name\}:\{identity\}"\},\n            \)\n\n            # Re-read only rows visible after we acquired the cross-process lock\.\n            # Rows inserted after this snapshot remain pending for the next pass\.\n            locked_rows = \(await session.execute\(''',
    '''    for company_id, connector_name, identity in identities:
        from celerp.connectors.operation_lock import ConnectorBusy, connector_operation
        try:
            operation = connector_operation(company_id, connector_name)
            await operation.__aenter__()
        except ConnectorBusy:
            continue
        try:
            async with get_session_ctx() as session:
                # Re-read only rows visible after the connector lease was acquired.
                # Rows inserted after this snapshot remain pending for the next pass.
                locked_rows = (await session.execute(''',
)
# Need indent rest of former block under async session until processed +=. Easier targeted manual indentation transform.
s = read("celerp/connectors/outbound_queue.py")
start = s.index("        try:\n            async with get_session_ctx() as session:\n", s.index("for company_id, connector_name, identity in identities:"))
end_marker = "            processed += len(ids)\n"
end = s.index(end_marker, start) + len(end_marker)
block = s[start:end]
# Existing body after async with was written at 12-space indentation from old structure.
# The regex replacement introduced one extra nesting level but did not reindent all lines.
lines = block.splitlines()
fixed = []
for idx, line in enumerate(lines):
    if idx <= 1:
        fixed.append(line)
    else:
        fixed.append("    " + line)
fixed_block = "\n".join(fixed) + ("\n" if block.endswith("\n") else "")
fixed_block += '''        finally:
            await operation.__aexit__(None, None, None)
'''
s = s[:start] + fixed_block + s[end:]
write("celerp/connectors/outbound_queue.py", s)
# Activated filter on worker config.
replace_once(
    "celerp/connectors/outbound_queue.py",
    '''                    ConnectorConfig.connector == connector_name,
                ).limit(1)
''',
    '''                    ConnectorConfig.connector == connector_name,
                    ConnectorConfig.activated_at.is_not(None),
                ).limit(1)
''',
)

# ---------------------------------------------------------------------------
# Scheduler and webhooks only run for active real-company ownership.
# ---------------------------------------------------------------------------
replace_once(
    "celerp/connectors/daily_scheduler.py",
    '''                ConnectorConfig.company_id == company_id,
            )
''',
    '''                ConnectorConfig.company_id == company_id,
                ConnectorConfig.activated_at.is_not(None),
            )
''',
)
regex_once(
    "celerp/connectors/daily_scheduler.py",
    r'async def _distinct_company_ids\(\) -> list\[str\]:.*?\n\nasync def scheduler_loop_all',
    '''async def _distinct_company_ids() -> list[str]:
    from celerp.connectors.ownership import active_company_ids
    return await active_company_ids()


async def scheduler_loop_all''',
)

# Woo webhook dispatch resolves one active owner.
regex_once(
    "celerp/connectors/webhooks.py",
    r'    connector = connector_registry.get\("woocommerce"\)\n\n    async with get_session_ctx\(\) as session:.*?\n    for company_id, secret, direction in configs:\n',
    '''    connector = connector_registry.get("woocommerce")
    from celerp.connectors.ownership import get_active_connector_owner

    config = await get_active_connector_owner("woocommerce")
    if config is None:
        return False
    configs = [(config.company_id, config.webhook_secret, config.direction)]

    for company_id, secret, direction in configs:
''',
)
# Direct product-deleted handler also needs the connector lease.
replace_once(
    "celerp/connectors/webhooks.py",
    '''    if event.platform == "woocommerce" and normalized_topic == "product.deleted":
        await connector.handle_product_deleted(ctx, event.payload or {})
        log.info("webhook: processed targeted WooCommerce product deletion for %s", ctx.company_id)
        return
''',
    '''    if event.platform == "woocommerce" and normalized_topic == "product.deleted":
        from celerp.connectors.operation_lock import ConnectorBusy, connector_operation
        try:
            async with connector_operation(ctx.company_id, connector.name):
                await connector.handle_product_deleted(ctx, event.payload or {})
        except ConnectorBusy:
            log.info("webhook: %s operation busy; daily reconciliation will catch up", connector.name)
            return
        log.info("webhook: processed targeted WooCommerce product deletion for %s", ctx.company_id)
        return
''',
)

# Shopify webhook routing: one active local owner, not every local config.
replace_once(
    "celerp/gateway/client.py",
    '''            async with get_session_ctx() as session:
                rows = await session.execute(
                    sa.select(ConnectorConfig.company_id).where(
                        ConnectorConfig.connector == "shopify"
                    )
                )
                configs = rows.all()

            event = WebhookEvent(platform="shopify", topic=topic, payload=data)
            want = _shop_key(shop)
            for (company_id,) in configs:
                ctx = await fetch_context(company_id, "shopify")
                if ctx is None:
                    continue
''',
    '''            from celerp.connectors.ownership import get_active_connector_owner
            config = await get_active_connector_owner("shopify")
            configs = [(config.company_id,)] if config is not None else []

            event = WebhookEvent(platform="shopify", topic=topic, payload=data)
            want = _shop_key(shop)
            for (company_id,) in configs:
                ctx = await fetch_context(company_id, "shopify")
                if ctx is None:
                    continue
''',
)

# ---------------------------------------------------------------------------
# Woo import: catalog metadata only, durable order cutoff, convergent webhooks.
# ---------------------------------------------------------------------------
replace_once(
    "celerp/connectors/woocommerce.py",
    '''                    sale_price=price,
                    quantity=float(stock_quantity) if stock_quantity is not None else None,
                    seed_quantity=(manage_stock is True),
                    link_fields={"manage_stock": manage_stock},
''',
    '''                    sale_price=price,
                    link_fields={"manage_stock": manage_stock},
''',
)
# Order cutoff block.
replace_once(
    "celerp/connectors/woocommerce.py",
    '''        params: dict = {}
        if since:
            params["modified_after"] = since.isoformat()
            params["dates_are_gmt"] = "true"  # our watermark is UTC; make Woo interpret it as UTC

        try:
            orders = await self._paginate(ctx, "/orders", params=params or None)
''',
    '''        from celerp.connectors.ownership import get_active_connector_config
        config = await get_active_connector_config(ctx.company_id, "woocommerce")
        if config is None or config.claimed_at is None:
            result.errors = ["WooCommerce ownership is not active for this company"]
            return result
        cutoff = config.claimed_at
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)

        params: dict = {
            "after": cutoff.isoformat(),
            "dates_are_gmt": "true",
        }
        if since:
            params["modified_after"] = since.isoformat()

        try:
            orders = await self._paginate(ctx, "/orders", params=params)
''',
)
replace_once(
    "celerp/connectors/woocommerce.py",
    '''        for order in orders:
            try:
                result.record(await _upsert.upsert_order_from_woocommerce(ctx.company_id, order))
''',
    '''        for order in orders:
            try:
                raw_created = order.get("date_created_gmt")
                if not raw_created:
                    raise ValueError("missing date_created_gmt; refusing operational import")
                created = datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    result.skipped += 1
                    continue
                result.record(await _upsert.upsert_order_from_woocommerce(ctx.company_id, order))
''',
)

# Convergent Woo webhooks, while preserving the legacy atomic registration API.
insert_at = read("celerp/connectors/woocommerce.py").index("    async def register_webhooks(")
woo = read("celerp/connectors/woocommerce.py")
new_methods = r'''    async def _owned_webhooks(
        self, ctx: ConnectorContext, webhook_url: str
    ) -> list[dict]:
        from celerp.config import ensure_instance_id
        base_url, auth = _base_url(ctx), _auth(ctx)
        name_prefix = f"Celerp {ensure_instance_id()} "
        async with RateLimitedClient() as client:
            page = 1
            out: list[dict] = []
            while True:
                resp = await client.get(
                    f"{base_url}/webhooks", auth=auth,
                    params={"per_page": 100, "page": page},
                )
                resp.raise_for_status()
                batch = resp.json()
                if not isinstance(batch, list):
                    raise ValueError("WooCommerce returned an invalid webhook list")
                out.extend(
                    h for h in batch
                    if str(h.get("delivery_url") or "").rstrip("/") == webhook_url.rstrip("/")
                    and str(h.get("name") or "").startswith(name_prefix)
                )
                if len(batch) < 100:
                    break
                page += 1
            return out

    async def reconcile_webhooks(
        self, ctx: ConnectorContext, webhook_url: str, secret: str,
        known_ids: list[str] | None = None,
    ) -> list[str]:
        """Converge Celerp-owned Woo hooks after retries or interrupted setup."""
        from celerp.config import ensure_instance_id
        base_url, auth = _base_url(ctx), _auth(ctx)
        name_prefix = f"Celerp {ensure_instance_id()} "
        existing = await self._owned_webhooks(ctx, webhook_url)
        by_topic: dict[str, list[dict]] = {}
        for hook in existing:
            by_topic.setdefault(str(hook.get("topic") or ""), []).append(hook)

        kept: list[str] = []
        async with RateLimitedClient() as client:
            for topic in self._WEBHOOK_TOPICS:
                matches = by_topic.pop(topic, [])
                body = {
                    "name": f"{name_prefix}{topic}",
                    "topic": topic,
                    "delivery_url": webhook_url,
                    "status": "active",
                    "secret": secret,
                }
                if matches:
                    chosen = matches[0]
                    hook_id = str(chosen.get("id") or "")
                    if not hook_id:
                        raise RuntimeError(f"WooCommerce webhook {topic} has no id")
                    resp = await client.put(
                        f"{base_url}/webhooks/{hook_id}", auth=auth, json=body
                    )
                    resp.raise_for_status()
                    kept.append(hook_id)
                    duplicates = matches[1:]
                else:
                    resp = await client.post(f"{base_url}/webhooks", auth=auth, json=body)
                    resp.raise_for_status()
                    hook_id = str(resp.json().get("id") or "")
                    if not hook_id:
                        raise RuntimeError(f"WooCommerce did not return a webhook id for {topic}")
                    kept.append(hook_id)
                    duplicates = []
                for duplicate in duplicates:
                    dup_id = str(duplicate.get("id") or "")
                    if dup_id:
                        cleanup = await client.delete(
                            f"{base_url}/webhooks/{dup_id}", auth=auth,
                            params={"force": "true"},
                        )
                        if cleanup.status_code not in (200, 204, 404):
                            cleanup.raise_for_status()

            # Any Celerp-owned hook at this delivery URL whose topic is no longer
            # canonical is stale and safe to remove.
            for leftovers in by_topic.values():
                for hook in leftovers:
                    hook_id = str(hook.get("id") or "")
                    if hook_id:
                        cleanup = await client.delete(
                            f"{base_url}/webhooks/{hook_id}", auth=auth,
                            params={"force": "true"},
                        )
                        if cleanup.status_code not in (200, 204, 404):
                            cleanup.raise_for_status()
        return kept

'''
woo = woo[:insert_at] + new_methods + woo[insert_at:]
write("celerp/connectors/woocommerce.py", woo)
# Extend deregistration with discovery.
replace_once(
    "celerp/connectors/woocommerce.py",
    '''    async def deregister_webhooks(
        self, ctx: ConnectorContext, webhook_ids: list[str]
    ) -> None:
        """Delete all known WooCommerce hooks; 404 means the hook is already gone."""
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        errors: list[str] = []
        async with RateLimitedClient() as client:
            for webhook_id in webhook_ids:
''',
    '''    async def deregister_webhooks(
        self, ctx: ConnectorContext, webhook_ids: list[str],
        webhook_url: str | None = None,
    ) -> None:
        """Delete cached and discoverable Celerp-owned hooks; 404 is already clean."""
        base_url = _base_url(ctx)
        auth = _auth(ctx)
        ids = {str(v) for v in webhook_ids if v not in (None, "")}
        if webhook_url:
            for hook in await self._owned_webhooks(ctx, webhook_url):
                if hook.get("id") not in (None, ""):
                    ids.add(str(hook["id"]))
        errors: list[str] = []
        async with RateLimitedClient() as client:
            for webhook_id in sorted(ids):
''',
)

# Wrap explicit Woo link/create in connector operation lease without duplicating body.
woo = read("celerp/connectors/woocommerce.py")
start = woo.index("    async def ensure_product_link(")
end = woo.index("\n    # -- Orders", start)
body = woo[start:end]
body = body.replace(
    '        """Enable one item, linking or creating a simple WooCommerce product safely."""\n',
    '        """Enable one item, linking or creating a simple WooCommerce product safely."""\n'
    '        from celerp.connectors.operation_lock import connector_operation\n'
    '        async with connector_operation(ctx.company_id, "woocommerce"):\n'
    '            return await self._ensure_product_link_locked(ctx, entity_id, actor_id=actor_id)\n\n'
    '    async def _ensure_product_link_locked(self, ctx: ConnectorContext, entity_id: str, actor_id=None) -> str:\n',
    1,
)
# The original function body following the docstring was indented 8 spaces; the new
# helper needs exactly that same indentation, so no further indentation change.
woo = woo[:start] + body + woo[end:]
write("celerp/connectors/woocommerce.py", woo)

# ---------------------------------------------------------------------------
# Catalog external identity can never silently jump between live remote products.
# ---------------------------------------------------------------------------
replace_once(
    "default_modules/celerp-inventory/celerp_inventory/services.py",
    '''    if len(candidates) == 1:
        return candidates[0]
''',
    '''    if len(candidates) == 1:
        candidate = candidates[0]
        existing_link = external_link_for_state(candidate.state or {}, platform)
        if (
            existing_link
            and existing_link.get("remote_deleted") is not True
            and not _same_external_identity(existing_link, str(product_id), variation_id)
        ):
            raise ValueError(
                f"SKU {sku!r} is already linked to a different live {platform} product"
            )
        return candidate
''',
)
# set_external_link acquires the same external identity lock as inbound product upsert.
old = '''    cid = uuid.UUID(str(company_id))
    row = await session.get(Projection, {"company_id": cid, "entity_id": entity_id}, with_for_update=True)
    if row is None or row.entity_type != "item":
        raise ValueError(f"Item {entity_id!r} not found")
    state = dict(row.state or {})
'''
new = '''    cid = uuid.UUID(str(company_id))
    product_id = str(link.get("product_id") or "")
    variation_id = (
        str(link.get("variation_id"))
        if link.get("variation_id") not in (None, "") else None
    )
    identity = f"{platform}:{product_id}" + (f":{variation_id}" if variation_id else "")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))")
        if session.bind.dialect.name == "postgresql" else text("SELECT 1"),
        {"k": f"external-product:{cid}:{identity}"} if session.bind.dialect.name == "postgresql" else {},
    )
    row = await session.get(
        Projection, {"company_id": cid, "entity_id": entity_id},
        with_for_update=True,
    )
    if row is None or row.entity_type != "item":
        raise ValueError(f"Item {entity_id!r} not found")
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
        Projection.entity_id != entity_id,
    ))).scalars().all()
    for other in rows:
        other_link = external_link_for_state(other.state or {}, platform)
        if _same_external_identity(other_link, product_id, variation_id):
            raise ValueError(
                f"{platform} product {product_id}"
                + (f" variation {variation_id}" if variation_id else "")
                + " is already linked to another Celerp item"
            )
    state = dict(row.state or {})
'''
replace_once("default_modules/celerp-inventory/celerp_inventory/services.py", old, new)
# Remove pre-row lock from set_external_link_state to keep identity-lock -> row-lock order.
regex_once(
    "default_modules/celerp-inventory/celerp_inventory/services.py",
    r'''(async def set_external_link_state\(.*?cid = uuid\.UUID\(str\(company_id\)\)\n)    row = await session\.get\(Projection, \{"company_id": cid, "entity_id": entity_id\}, with_for_update=True\)''',
    r'''\1    row = await session.get(Projection, {"company_id": cid, "entity_id": entity_id})''',
)

# ---------------------------------------------------------------------------
# Canonical sales stock allocation lock: short DB-only critical section.
# ---------------------------------------------------------------------------
append = r'''

async def lock_sales_stock_allocation(session, company_id) -> None:
    """Serialize sales reserve/fulfill allocation within one company.

    Cross-lot picks can touch multiple SKUs/lots. One company-scoped transaction
    lock is deliberately coarser than per-SKU locking but has deterministic lock
    order and prevents two documents from validating the same stock snapshot.
    """
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"sales-stock:{company_id}"},
        )
'''
with open(ROOT / "celerp/services/pick.py", "a") as f:
    f.write(append)

replace_once(
    "default_modules/celerp-docs/celerp_docs/routes.py",
    '''    state = row.state
    _validate_line_entity_ids_subset(line_entity_ids, state)
''',
    '''    from celerp.services.pick import lock_sales_stock_allocation
    await lock_sales_stock_allocation(session, row.company_id)
    state = row.state
    _validate_line_entity_ids_subset(line_entity_ids, state)
''',
)
replace_once(
    "default_modules/celerp-docs/celerp_docs/routes.py",
    '''    row = await _get_doc(session, company_id, entity_id, for_update=True)
    state = row.state
    doc_type = state.get("doc_type", "")
''',
    '''    row = await _get_doc(session, company_id, entity_id, for_update=True)
    from celerp.services.pick import lock_sales_stock_allocation
    await lock_sales_stock_allocation(session, company_id)
    state = row.state
    doc_type = state.get("doc_type", "")
''',
)

# ---------------------------------------------------------------------------
# Woo order lifecycle: immutable material snapshot, legacy quarantine, actor reuse.
# ---------------------------------------------------------------------------
# Free-text fallback identity in fingerprint + material address/contact fields.
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    '''        key = (
            str(li.get("product_id") or ""),
            str(li.get("variation_id") or ""),
            str(li.get("sku") or "").strip().casefold(),
        )
''',
    '''        product_id = str(li.get("product_id") or "")
        variation_id = str(li.get("variation_id") or "")
        sku = str(li.get("sku") or "").strip().casefold()
        fallback = str(li.get("id") or "") if not (product_id or variation_id or sku) else ""
        key = (product_id, variation_id, sku, fallback)
''',
)
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    '''        "total_tax": _f(order.get("total_tax"), 0),
        "total": _f(order.get("total"), 0),
    }
''',
    '''        "total_tax": _f(order.get("total_tax"), 0),
        "total": _f(order.get("total"), 0),
        "customer_id": int(order.get("customer_id") or 0),
        "billing": order.get("billing") or {},
        "shipping_address": order.get("shipping") or {},
    }
''',
)
# Imports: use canonical active owner and sales lock.
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    "    from celerp.models.accounting import UserCompany\n",
    "",
)
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    '''    from celerp.services.pick import consolidate_sales_lots, plan_lot_draws, resolve_pick_method
''',
    '''    from celerp.services.pick import (
        consolidate_sales_lots, lock_sales_stock_allocation,
        plan_lot_draws, resolve_pick_method,
    )
''',
)
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    '''        apply_doc_payment,
    )
''',
    '''        apply_doc_payment,
    )
    from celerp_docs.routes_payments import _company_owner_id
''',
)
# Lock sales allocation alongside order lock.
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    '''        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"woocommerce-order:{cid}:{order_id}"},
        )

        existing = await session.get(
''',
    '''        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))")
            if session.bind.dialect.name == "postgresql" else text("SELECT 1"),
            {"k": f"woocommerce-order:{cid}:{order_id}"}
            if session.bind.dialect.name == "postgresql" else {},
        )
        await lock_sales_stock_allocation(session, cid)

        existing = await session.get(
''',
)
# Legacy quarantine before finalized branch.
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    '''        if existing is not None and (existing.state or {}).get("finalized"):
''',
    '''        if (
            existing is not None
            and not (existing.state or {}).get("woocommerce_source_fingerprint")
        ):
            # Pre-hardening Woo imports were informational snapshots. Never promote
            # them implicitly into inventory/accounting side effects after upgrade.
            await session.commit()
            return "noop"

        if existing is not None and (existing.state or {}).get("finalized"):
''',
)
# Monotonic completed status.
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    '''            if wc_status in {"cancelled", "failed", "refunded"}:
''',
    '''            prior_wc_status = str((existing.state or {}).get("woocommerce_status") or "")
            if prior_wc_status == "completed" and wc_status != "completed":
                raise ValueError(
                    f"WooCommerce order {order.get('number') or order_id} regressed "
                    f"from completed to {wc_status}; manual reconciliation is required"
                )
            if wc_status in {"cancelled", "failed", "refunded"}:
''',
)
# Group free-text lines distinctly.
replace_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    '''                key = (
                    str(li.get("product_id") or ""),
                    str(li.get("variation_id") or ""),
                    str(li.get("sku") or "").strip().casefold(),
                )
''',
    '''                product_id_key = str(li.get("product_id") or "")
                variation_id_key = str(li.get("variation_id") or "")
                sku_key = str(li.get("sku") or "").strip().casefold()
                fallback_key = (
                    str(li.get("id") or "")
                    if not (product_id_key or variation_id_key or sku_key) else ""
                )
                key = (product_id_key, variation_id_key, sku_key, fallback_key)
''',
)
# Active owner primitive.
regex_once(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    r'''        owner_id = \(await session.execute\(\n            select\(UserCompany.user_id\).*?\n        \)\)\.scalar_one_or_none\(\)\n''',
    '''        owner_id = await _company_owner_id(session, cid)
''',
)
replace_once(
    "default_modules/celerp-docs/celerp_docs/routes_payments.py",
    '''        select(UserCompany.user_id).where(
            UserCompany.company_id == company_id, UserCompany.role == "owner")
''',
    '''        select(UserCompany.user_id).where(
            UserCompany.company_id == company_id,
            UserCompany.role == "owner",
            UserCompany.is_active.is_(True),
        )
''',
)

# ---------------------------------------------------------------------------
# UI ownership/activation lifecycle and pull-only activation.
# ---------------------------------------------------------------------------
# Replace local ownership helper implementations with service delegates.
regex_once(
    "ui/routes/settings_connectors.py",
    r'async def _get_connector_config\(company_id: str, connector: str\):.*?\n\nasync def _claim_connector_for_company',
    '''async def _get_connector_config(company_id: str, connector: str):
    from celerp.connectors.ownership import get_connector_config
    return await get_connector_config(company_id, connector)


async def _claim_connector_for_company''',
)
regex_once(
    "ui/routes/settings_connectors.py",
    r'async def _claim_connector_for_company\(company_id: str, connector: str\) -> bool:.*?\n\nasync def _ensure_connector_config',
    '''async def _claim_connector_for_company(company_id: str, connector: str) -> bool:
    from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector
    from celerp.connectors.registry import get as get_connector
    try:
        category = get_connector(connector).category.value
        await claim_connector(
            company_id, connector,
            sync_frequency=_DEFAULT_FREQUENCY.get(category, SyncFrequency.MANUAL).value,
        )
        return True
    except ConnectorOwnershipError:
        return False


async def _ensure_connector_config''',
)
regex_once(
    "ui/routes/settings_connectors.py",
    r'async def _ensure_connector_config\(company_id: str, connector: str, category: str\):.*?\n\nasync def _clear_connector_config',
    '''async def _ensure_connector_config(company_id: str, connector: str, category: str):
    from celerp.connectors.ownership import claim_connector
    return await claim_connector(
        company_id, connector,
        sync_frequency=_DEFAULT_FREQUENCY.get(category, SyncFrequency.MANUAL).value,
    )


async def _clear_connector_config''',
)
regex_once(
    "ui/routes/settings_connectors.py",
    r'async def _clear_connector_config\(company_id: str, connector: str\) -> None:.*?\n\nasync def _kickoff_connector_sync',
    '''async def _clear_connector_config(company_id: str, connector: str) -> None:
    from celerp.connectors.ownership import delete_connector_config
    await delete_connector_config(company_id, connector)


async def _kickoff_connector_sync''',
)
replace_once(
    "ui/routes/settings_connectors.py",
    'async def _kickoff_connector_sync(company_id: str, platform: str, token: str) -> None:\n',
    'async def _kickoff_connector_sync(\n    company_id: str, platform: str, token: str, *, activation: bool = False\n) -> None:\n',
)
replace_once(
    "ui/routes/settings_connectors.py",
    '''    config = await _get_connector_config(company_id, platform)
    direction = SyncDirection(config.direction if config else connector.direction.value)

    async def _do_sync():
''',
    '''    config = await _get_connector_config(company_id, platform)
    if config is None:
        raise RuntimeError("Connector is not owned by this company")
    if not activation and config.activated_at is None:
        raise RuntimeError("Connector activation is still pending")
    direction = SyncDirection(config.direction)

    async def _do_sync():
''',
)
replace_once(
    "ui/routes/settings_connectors.py",
    '''        await run_connector_sync(connector, ctx, direction=direction)
''',
    '''        if activation:
            from celerp.connectors.sync_runner import run_connector_activation
            await run_connector_activation(connector, ctx)
        else:
            await run_connector_sync(connector, ctx, direction=direction)
''',
)
replace_once(
    "ui/routes/settings_connectors.py",
    '        await _kickoff_connector_sync(company_id, platform, token)\n',
    '        await _kickoff_connector_sync(company_id, platform, token, activation=True)\n',
)
# Webhook setup uses reconciliation.
replace_once(
    "ui/routes/settings_connectors.py",
    '    ids = await connector.register_webhooks(ctx, delivery_url, secret=secret)\n',
    '    ids = await connector.reconcile_webhooks(ctx, delivery_url, secret)\n',
)
# No destructive rollback on local persistence failure; retry can reconcile because credentials remain.
regex_once(
    "ui/routes/settings_connectors.py",
    r'''    try:\n        async with get_session_ctx\(\) as session:.*?\n            await session.commit\(\)\n    except Exception:\n        try:\n            await connector.deregister_webhooks\(ctx, ids\)\n        except Exception:\n            log.warning\(\n                "failed to roll back WooCommerce webhooks after local persistence failure",\n                exc_info=True,\n            \)\n        raise\n''',
    '''    async with get_session_ctx() as session:
        await session.execute(
            sa.update(ConnectorConfig)
            .where(
                ConnectorConfig.company_id == company_id,
                ConnectorConfig.connector == "woocommerce",
            )
            .values(webhook_secret=secret, webhook_ids_json=json.dumps(ids))
        )
        await session.commit()
''',
)
# Catalog render: config ownership determines local connected state; pending OAuth activates only after relay says connected.
old = '''    # Load configs for all connected connectors
    configs: dict[str, object] = {}
    for c in catalog:
        if c.get("connected"):
            cfg = await _ensure_connector_config(company_id, c["id"], c.get("category", "website"))
            configs[c["id"]] = cfg

    # Auto-sync a freshly connected store that has never synced (e.g. just returned from
    # OAuth) so the merchant's data appears without a manual step - the activation moment.
    # Idempotent: run_sync's in-progress row + concurrency guard prevent re-triggering on
    # re-render, and once any run exists this branch no longer fires.
    for c in catalog:
        if c.get("connected") and last_runs.get(c["id"]) is None:
            spawn_background(_autosync_once(company_id, c["id"], token))
'''
new = '''    # Relay connected is installation-scoped. A pending local owner may finish
    # activation, but it is not exposed to autonomous work until that pull succeeds.
    configs: dict[str, object] = {}
    owned_catalog: list[dict] = []
    pending_activation: list[str] = []
    for raw in catalog:
        c = dict(raw)
        cfg = await _get_connector_config(company_id, c["id"])
        relay_connected = bool(raw.get("connected"))
        if (
            cfg is not None
            and cfg.activated_at is None
            and relay_connected
            and c.get("auth_type") == "oauth"
        ):
            pending_activation.append(c["id"])
        local_connected = bool(
            relay_connected and cfg is not None and cfg.activated_at is not None
        )
        c["connected"] = local_connected
        if cfg is not None:
            configs[c["id"]] = cfg
        owned_catalog.append(c)
    catalog = owned_catalog

    for platform in pending_activation:
        spawn_background(_autosync_once(company_id, platform, token))
    for c in catalog:
        if c.get("connected") and last_runs.get(c["id"]) is None:
            spawn_background(_autosync_once(company_id, c["id"], token))
'''
replace_once("ui/routes/settings_connectors.py", old, new)
# OAuth ownership is enforced in the API authorize-url boundary.
replace_once(
    "ui/routes/settings_connectors.py",
    '''        lang = get_lang(request)

        from ui.api_client import APIError, get_connector_authorize_url
''',
    '''        lang = get_lang(request)
        if (err := _validate_platform(platform)):
            return err

        from ui.api_client import APIError, get_connector_authorize_url
''',
)
# API key success: activate after webhook health; retain recoverable pending state on webhook failure.
replace_once(
    "ui/routes/settings_connectors.py",
    '        from ui.api_client import delete_connector_credentials, store_connector_credentials\n',
    '        from ui.api_client import store_connector_credentials\n',
)
regex_once(
    "ui/routes/settings_connectors.py",
    r'''            except Exception as exc:\n                log.warning\("woocommerce webhook registration failed", exc_info=True\)\n                try:\n                    await delete_connector_credentials\(token, platform\)\n                except Exception:\n                    log.warning\("failed to roll back WooCommerce relay credentials", exc_info=True\)\n                await _clear_connector_config\(company_id, platform\)\n                return Div\(''',
    '''            except Exception as exc:
                log.warning("woocommerce webhook registration failed", exc_info=True)
                # Keep the pending ownership claim and credentials so the same
                # company can retry reconciliation. Autonomous work is still off.
                return Div(''',
)
replace_once(
    "ui/routes/settings_connectors.py",
    '''        # Auto-sync on connect so the merchant's data appears without a manual step
        # (the activation moment). Best-effort: a failure here doesn't block the connect.
        try:
            await _kickoff_connector_sync(company_id, platform, token)
''',
    '''        from celerp.connectors.ownership import activate_connector
        config = await activate_connector(company_id, platform)

        # Activation is pull-only. Ordinary manual/scheduled sync later honors the
        # configured direction and may write outbound.
        try:
            await _kickoff_connector_sync(company_id, platform, token, activation=True)
''',
)
# Disconnect is intentionally thin. The API credential boundary owns serialized
# webhook cleanup, relay revocation, and local ownership deletion.
start = read("ui/routes/settings_connectors.py").index('    @app.delete("/settings/connectors/{platform}/disconnect")')
end = read("ui/routes/settings_connectors.py").index('    @app.post("/settings/connectors/{platform}/sync")', start)
block = read("ui/routes/settings_connectors.py")[start:end]
cleanup_start = block.index("        cleanup_warning =")
tail_start = block.index("        if request.query_params.get", cleanup_start)
prefix = block[:cleanup_start]
tail = block[tail_start:]
core = r'''        from ui.api_client import delete_connector_credentials
        try:
            result = await delete_connector_credentials(token, platform)
        except Exception as exc:
            return Div(
                Span(f"✗ {exc}", cls="flash flash--warning"),
                id=f"connector-card-{platform}", cls="connector-card",
            )
        if not result.get("ok", True):
            return Div(
                Span(result.get("detail") or result.get("error") or "Disconnect failed",
                     cls="flash flash--warning"),
                id=f"connector-card-{platform}", cls="connector-card",
            )

'''
settings = read("ui/routes/settings_connectors.py")
settings = settings[:start] + prefix + core + tail + settings[end:]
write("ui/routes/settings_connectors.py", settings)

# ---------------------------------------------------------------------------
# Credential/OAuth/token API boundaries enforce the ownership invariant.
# ---------------------------------------------------------------------------
replace_once(
    "celerp/routers/health.py",
    "from celerp.services.auth import ROLE_LEVELS, get_current_role, get_current_user\\n",
    "from celerp.services.auth import ROLE_LEVELS, get_current_company_id, get_current_role, get_current_user\\n",
)
replace_once(
    "celerp/routers/health.py",
    'async def connector_authorize_url(platform: str, shop: str = "") -> dict:\\n',
    'async def connector_authorize_url(\\n    platform: str, shop: str = "", company_id: str = Depends(get_current_company_id)\\n) -> dict:\\n',
)
replace_once(
    "celerp/routers/health.py",
    '''    if r.status_code == 200:
        return {"authorize_url": r.json().get("authorize_url", "")}
''',
    '''    if r.status_code == 200:
        url = r.json().get("authorize_url", "")
        if not url:
            return {"error": "Relay returned an empty authorization URL."}
        from celerp.connectors.base import ConnectorCategory
        from celerp.connectors.operation_lock import ConnectorBusy, connector_operation
        from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector
        from celerp.connectors.registry import get as get_connector
        try:
            async with connector_operation(str(company_id), platform):
                connector = get_connector(platform)
                await claim_connector(
                    str(company_id), platform,
                    sync_frequency=(
                        "realtime" if connector.category == ConnectorCategory.WEBSITE
                        else "manual"
                    ),
                )
        except (ConnectorBusy, ConnectorOwnershipError) as exc:
            return {"error": str(exc)}
        return {"authorize_url": url}
''',
)

routes_path = "default_modules/celerp-connectors/celerp_connectors/routes.py"
routes = read(routes_path)
start = routes.index('@router.post("/{connector_name}/credentials")')
end = routes.index('\\n\\nclass ItemSyncRequest', start)
replacement = r'''@router.post("/{connector_name}/credentials")
async def store_credentials(
    connector_name: str,
    payload: ApiKeyCredentials,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
) -> dict:
    """Validate and store credentials only after atomically claiming the connector."""
    import httpx
    from celerp.connectors.base import ConnectorCategory
    from celerp.connectors.operation_lock import ConnectorBusy, connector_operation
    from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector
    from celerp.gateway.state import relay_http_url, relay_session_headers

    try:
        connector = connectors.get(connector_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if (err := _relay_https_error()) is not None:
        return err

    store_url = (payload.store_url or "").strip().rstrip("/")
    try:
        async with connector_operation(str(company_id), connector_name):
            try:
                await claim_connector(
                    str(company_id),
                    connector_name,
                    sync_frequency=(
                        "realtime"
                        if connector.category == ConnectorCategory.WEBSITE else "manual"
                    ),
                )
            except ConnectorOwnershipError as exc:
                return {"ok": False, "error": "ownership_conflict", "detail": str(exc)}

            if connector_name == "woocommerce":
                import os
                if not store_url:
                    return {"ok": False, "error": "store_unreachable",
                            "detail": "Store URL is required."}
                allow_http = (
                    store_url.startswith("http://")
                    and bool(os.environ.get("CELERP_ALLOW_HTTP_STORE"))
                )
                if not (store_url.startswith("https://") or allow_http):
                    return {"ok": False, "error": "store_unreachable",
                            "detail": "Store URL must use https:// (API keys are sent as Basic Auth)."}
                try:
                    async with httpx.AsyncClient(timeout=8.0) as c:
                        probe = await c.get(
                            f"{store_url}/wp-json/wc/v3/products",
                            params={"per_page": 1},
                            auth=(payload.consumer_key, payload.consumer_secret),
                        )
                    if probe.status_code == 401:
                        return {"ok": False, "error": "store_rejected",
                                "detail": "store rejected the consumer key/secret (401)"}
                    probe.raise_for_status()
                except Exception as exc:
                    return {"ok": False, "error": "store_unreachable", "detail": str(exc)}

            try:
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.post(
                        f"{relay_http_url()}/tokens/{connector_name}",
                        json={
                            "consumer_key": payload.consumer_key,
                            "consumer_secret": payload.consumer_secret,
                            "store_url": store_url or None,
                        },
                        headers=relay_session_headers(),
                    )
            except Exception as exc:
                return {"ok": False, "error": "relay_error", "detail": str(exc)}
    except ConnectorBusy as exc:
        return {"ok": False, "error": "connector_busy", "detail": str(exc)}

    if r.status_code == 402:
        return {"ok": False, "error": "subscription_required", "detail": ""}
    if r.status_code != 200:
        return {"ok": False, "error": "relay_error",
                "detail": f"relay returned {r.status_code}"}
    return {"ok": True}


@router.delete("/{connector_name}/credentials")
async def revoke_credentials(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
) -> dict:
    """Safely disconnect one owned connector, including Woo webhook cleanup."""
    import httpx
    from celerp.connectors.operation_lock import ConnectorBusy, connector_operation
    from celerp.connectors.ownership import delete_connector_config, get_connector_config
    from celerp.gateway.state import relay_http_url, relay_session_headers

    config = await get_connector_config(
        str(company_id), connector_name, adopt_single_company=True
    )
    if config is None:
        return {"ok": False, "error": "not_owned",
                "detail": "This connector is not owned by the current company."}

    try:
        async with connector_operation(str(company_id), connector_name):
            if connector_name == "woocommerce" and (
                config.webhook_ids or config.webhook_secret
            ):
                try:
                    async with httpx.AsyncClient(timeout=10.0) as c:
                        token_resp = await c.get(
                            f"{relay_http_url()}/tokens/{connector_name}/access-token",
                            headers=relay_session_headers(),
                        )
                except Exception as exc:
                    return {"ok": False, "error": "relay_error", "detail": str(exc)}
                if token_resp.status_code != 200:
                    return {
                        "ok": False,
                        "error": "webhook_cleanup_unavailable",
                        "detail": "WooCommerce credentials are required to clean up remote webhooks.",
                    }
                data = token_resp.json()
                from celerp.connectors.base import ConnectorContext
                from celerp.connectors.woocommerce import WooCommerceConnector
                ctx = ConnectorContext(
                    company_id=str(company_id),
                    access_token=data["access_token"],
                    store_handle=data.get("store_handle"),
                    extra=data.get("extra"),
                )
                delivery_url = f"{relay_http_url().rstrip('/')}/webhooks/woocommerce/events"
                try:
                    await WooCommerceConnector().deregister_webhooks(
                        ctx, config.webhook_ids, webhook_url=delivery_url
                    )
                except Exception as exc:
                    return {"ok": False, "error": "webhook_cleanup_failed",
                            "detail": str(exc)}

            try:
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.delete(
                        f"{relay_http_url()}/tokens/{connector_name}",
                        headers=relay_session_headers(),
                    )
            except Exception as exc:
                return {"ok": False, "error": "relay_error", "detail": str(exc)}
            if r.status_code not in (200, 404):
                return {"ok": False, "error": "relay_error",
                        "detail": f"relay returned {r.status_code}"}

            await delete_connector_config(str(company_id), connector_name)
            return {"ok": True}
    except ConnectorBusy as exc:
        return {"ok": False, "error": "connector_busy", "detail": str(exc)}


@router.get("/{connector_name}/access-token")
async def connector_access_token(
    connector_name: str,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
) -> dict:
    """Return a relay token only to the ERP company that owns this connector."""
    import httpx
    from celerp.connectors.ownership import get_connector_config
    from celerp.gateway.state import relay_http_url, relay_session_headers

    config = await get_connector_config(
        str(company_id), connector_name, adopt_single_company=True
    )
    if config is None:
        return {"error": "not_connected",
                "detail": f"{connector_name} is not owned by this company."}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{relay_http_url()}/tokens/{connector_name}/access-token",
                headers=relay_session_headers(),
            )
    except Exception as exc:
        return {"error": "relay_error", "detail": str(exc)}

    if r.status_code == 404:
        return {"error": "not_connected",
                "detail": f"No {connector_name} connection found. Connect the platform first."}
    if r.status_code == 401:
        return {"error": "session_invalid",
                "detail": f"{connector_name} token expired. Please reconnect."}
    if r.status_code == 402:
        return {"error": "subscription_required",
                "detail": "An active subscription is required to sync connectors."}
    if r.status_code != 200:
        return {"error": "relay_error", "detail": f"relay returned {r.status_code}"}
    return r.json()
'''
routes = routes[:start] + replacement + routes[end:]
write(routes_path, routes)

replace_once(
    "celerp/connectors/relay_token.py",
    '''    from celerp.connectors.base import ConnectorContext
    from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers

    if not get_session_token():
''',
    '''    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.ownership import get_active_connector_config
    from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers

    if await get_active_connector_config(company_id, connector_name) is None:
        return None
    if not get_session_token():
''',
)

# ---------------------------------------------------------------------------
# Local connector API and catalog UI require activated ownership; publishing new
# remote products additionally requires manage_integrations.
# ---------------------------------------------------------------------------
replace_once(
    "default_modules/celerp-connectors/celerp_connectors/routes.py",
    "from celerp.services.auth import get_current_company_id, get_current_user\n",
    "from celerp.services.auth import get_current_company_id, get_current_user, get_current_role\n",
)
replace_once(
    "default_modules/celerp-connectors/celerp_connectors/routes.py",
    "from celerp.services.permissions import require_permission\n",
    "from celerp.services.permissions import get_current_company_settings, require_permission, role_has_permission\n",
)
replace_once(
    "default_modules/celerp-connectors/celerp_connectors/routes.py",
    '''    user=Depends(get_current_user), _: None = require_permission("adjust_inventory"),
    session: AsyncSession = Depends(get_session),
''',
    '''    user=Depends(get_current_user), _: None = require_permission("adjust_inventory"),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
''',
)
replace_once(
    "default_modules/celerp-connectors/celerp_connectors/routes.py",
    '''        ConnectorConfig.connector == connector_name,
    ))).scalar_one_or_none()
''',
    '''        ConnectorConfig.connector == connector_name,
        ConnectorConfig.activated_at.is_not(None),
    ))).scalar_one_or_none()
''',
)
# Gate remote Woo creation/rebinding.
replace_once(
    "default_modules/celerp-connectors/celerp_connectors/routes.py",
    '''    else:
        from celerp.connectors.relay_token import fetch_context
''',
    '''    else:
        from celerp_inventory.services import external_link_for_state
        needs_publish = any(
            not external_link_for_state(anchor.state or {}, "woocommerce")
            or external_link_for_state(anchor.state or {}, "woocommerce").get("remote_deleted") is True
            for anchor in anchors.values()
        )
        if needs_publish and not role_has_permission(settings, role, "manage_integrations"):
            raise HTTPException(
                status_code=403,
                detail="Publishing or relinking a WooCommerce product requires manage_integrations",
            )
        from celerp.connectors.relay_token import fetch_context
''',
)

# Inventory channel contribution considers only activated configs.
replace_once(
    "ui/routes/inventory.py",
    '''            rows = await session.execute(sa.select(ConnectorConfig.connector).where(
                ConnectorConfig.company_id == str(company_id)
            ))
''',
    '''            rows = await session.execute(sa.select(ConnectorConfig.connector).where(
                ConnectorConfig.company_id == str(company_id),
                ConnectorConfig.activated_at.is_not(None),
            ))
''',
)
# UI avoids offering create button to roles that cannot manage integrations.
replace_once(
    "ui/routes/inventory.py",
    '''                    and (linked or bool(channel.get("can_create")))
                )
''',
    '''                    and (
                        linked
                        or (
                            bool(channel.get("can_create"))
                            and role_has_permission(_settings, role, "manage_integrations")
                        )
                    )
                )
''',
)

# ---------------------------------------------------------------------------
# Detail page also uses local active ownership, not relay installation state.
replace_once(
    "ui/routes/settings_connectors.py",
    '''        config = await _get_connector_config(company_id, platform)
        runs = await _entity_runs(company_id, platform)
''',
    '''        config = await _get_connector_config(company_id, platform)
        c = dict(c)
        c["connected"] = bool(
            c.get("connected") and config is not None and config.activated_at is not None
        )
        runs = await _entity_runs(company_id, platform)
''',
)

# Startup adoption service is best-effort, not a boot dependency.
# ---------------------------------------------------------------------------
replace_once(
    "celerp/main.py",
    '''    from celerp.connectors.outbound_queue import (
        adopt_single_company_legacy_configs,
        outbound_queue_loop,
    )
    await adopt_single_company_legacy_configs()
''',
    '''    from celerp.connectors.outbound_queue import outbound_queue_loop
    from celerp.connectors.ownership import adopt_single_company_legacy_configs
    try:
        await adopt_single_company_legacy_configs()
    except Exception:
        logging.getLogger(__name__).exception(
            "Connector legacy ownership adoption failed (non-fatal)"
        )
''',
)

# ---------------------------------------------------------------------------
# Tests updated/added around new invariants.
# ---------------------------------------------------------------------------
replace_once(
    "tests/test_services/test_sync_runner_lifecycle.py",
    '''async def test_concurrency_guard_refuses_second_run(_db_engine):
''',
    '''async def test_abandoned_run_is_closed_when_operation_lease_is_free(_db_engine):
''',
)
replace_once(
    "tests/test_services/test_sync_runner_lifecycle.py",
    '''    assert ran["v"] is False  # the sync body never ran - a run is already in progress
    assert res.errors and "already in progress" in res.errors[0]
    rows = await _rows(cid)
    assert len(rows) == 1  # no second row created
''',
    '''    assert ran["v"] is True
    assert not res.errors
    rows = await _rows(cid)
    assert len(rows) == 2
    old = min(rows, key=lambda row: row.id)
    new = max(rows, key=lambda row: row.id)
    assert old.status == "failed" and old.finished_at is not None
    assert "Interrupted" in (old.errors_json or "")
    assert new.status == "success"
''',
)

# Woo cutoff/product quantity unit tests: adapt existing connector unit file.
with open(ROOT / "tests/test_woocommerce_sync_guards.py", "a") as f:
    f.write(r'''

def test_commercial_fingerprint_distinguishes_free_text_lines_and_addresses():
    from celerp_docs.doc_service import _woocommerce_commercial_fingerprint
    base = {
        "currency": "USD", "total_tax": "0", "total": "10",
        "line_items": [
            {"id": 1, "name": "A", "quantity": 1, "total": "5"},
            {"id": 2, "name": "B", "quantity": 1, "total": "5"},
        ],
        "billing": {"first_name": "A", "address_1": "One"},
        "shipping": {"first_name": "A", "address_1": "One"},
    }
    changed_line = {**base, "line_items": [
        {"id": 1, "name": "A", "quantity": 1, "total": "4"},
        {"id": 2, "name": "B", "quantity": 1, "total": "6"},
    ]}
    changed_address = {**base, "shipping": {"first_name": "A", "address_1": "Two"}}
    fp = _woocommerce_commercial_fingerprint(base)
    assert _woocommerce_commercial_fingerprint(changed_line) != fp
    assert _woocommerce_commercial_fingerprint(changed_address) != fp
''')

write("tests/test_connector_operation_lock.py", r'''# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
from __future__ import annotations

import asyncio
import uuid

import pytest

from celerp.connectors.operation_lock import ConnectorBusy, connector_operation


@pytest.mark.asyncio
async def test_connector_operation_try_lock_is_exclusive(_db_engine):
    cid = str(uuid.uuid4())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with connector_operation(cid, "woocommerce"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    with pytest.raises(ConnectorBusy):
        async with connector_operation(cid, "woocommerce"):
            pass
    release.set()
    await task
    async with connector_operation(cid, "woocommerce"):
        pass
''')

# Basic ownership race using the real DB and real companies.
write("tests/test_connector_ownership.py", r'''# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
from __future__ import annotations

import asyncio
import uuid

import pytest

from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector
from celerp.db import get_session_ctx
from celerp.models.company import Company


@pytest.mark.asyncio
async def test_connector_claim_has_one_company_winner(_db_engine):
    suffix = uuid.uuid4().hex[:10]
    a, b = uuid.uuid4(), uuid.uuid4()
    async with get_session_ctx() as s:
        s.add_all([
            Company(id=a, name="Owner A", slug=f"owner-a-{suffix}", settings={}),
            Company(id=b, name="Owner B", slug=f"owner-b-{suffix}", settings={}),
        ])
        await s.commit()

    results = await asyncio.gather(
        claim_connector(str(a), "woocommerce"),
        claim_connector(str(b), "woocommerce"),
        return_exceptions=True,
    )
    assert sum(not isinstance(x, Exception) for x in results) == 1
    assert sum(isinstance(x, ConnectorOwnershipError) for x in results) == 1
''')

# Existing queue fixture represents a migrated, active connector.
replace_once(
    "tests/test_services/test_outbound_queue.py",
    '''    session.add(ConnectorConfig(
        company_id=str(company_id), connector="woocommerce", direction="both"
    ))
''',
    '''    session.add(ConnectorConfig(
        company_id=str(company_id), connector="woocommerce", direction="both",
        claimed_at=now, activated_at=now,
    ))
''',
)

# Legacy informational Woo docs are quarantined before CRM side effects.
doc_path = "default_modules/celerp-docs/celerp_docs/doc_service.py"
doc = read(doc_path)
needle = '''    # Registered customers are independent CRM records. Import them first so the
    # document can carry a stable contact link; guest orders still keep snapshots.
'''
guard = '''    # Quarantine pre-hardening informational Woo documents before any independent
    # CRM side effect. The locked check below repeats this inside the main transaction.
    cid = __import__("uuid").UUID(str(company_id))
    async with SessionLocal() as probe:
        legacy = await probe.get(
            Projection, {"company_id": cid, "entity_id": entity_id}
        )
        if legacy is not None and not (legacy.state or {}).get(
            "woocommerce_source_fingerprint"
        ):
            return "noop"

'''
if needle not in doc:
    raise SystemExit("doc_service: legacy preflight insertion point missing")
doc = doc.replace(needle, guard + needle, 1)
doc = doc.replace(
    '        cid = __import__("uuid").UUID(str(company_id))\\n'
    '        # One external order may arrive simultaneously',
    '        # One external order may arrive simultaneously',
    1,
)
old = '''        current_status = str((doc.state or {}).get("woocommerce_status") or "")
        if current_status != wc_status:
            fields = {"woocommerce_status": {"old": current_status, "new": wc_status}}
            if order.get("transaction_id") != (doc.state or {}).get("woocommerce_transaction_id"):
                fields["woocommerce_transaction_id"] = {
                    "old": (doc.state or {}).get("woocommerce_transaction_id"),
                    "new": order.get("transaction_id"),
                }
            await emit_event(
                session, company_id=cid, entity_id=entity_id, entity_type="doc",
                event_type="doc.updated", data={"fields_changed": fields},
                actor_id=owner_id, location_id=None, source="connector",
                idempotency_key=f"{idem_key}:status:{wc_status}", metadata_={},
            )
            changed = True
'''
new = '''        current_status = str((doc.state or {}).get("woocommerce_status") or "")
        current_tx = (doc.state or {}).get("woocommerce_transaction_id")
        incoming_tx = order.get("transaction_id")
        fields = {}
        if current_status != wc_status:
            fields["woocommerce_status"] = {"old": current_status, "new": wc_status}
        if incoming_tx != current_tx:
            fields["woocommerce_transaction_id"] = {
                "old": current_tx, "new": incoming_tx,
            }
        if fields:
            await emit_event(
                session, company_id=cid, entity_id=entity_id, entity_type="doc",
                event_type="doc.updated", data={"fields_changed": fields},
                actor_id=owner_id, location_id=None, source="connector",
                idempotency_key=(
                    f"{idem_key}:status:{wc_status}:{incoming_tx or '-'}"
                ),
                metadata_={},
            )
            changed = True
'''
if old not in doc:
    raise SystemExit("doc_service: status update block missing")
doc = doc.replace(old, new, 1)
write(doc_path, doc)


# ---------------------------------------------------------------------------
# Final invariant appendix. Keep these at the end so earlier source rewrites
# cannot supersede them.
# ---------------------------------------------------------------------------

# Long remote connector operations must not occupy the request DB pool.
op_path = "celerp/connectors/operation_lock.py"
op = read(op_path)
if "from celerp.db import engine" in op:
    op = op.replace("from celerp.db import engine", "from celerp.db import lifecycle_engine", 1)
    op = op.replace("    if engine.dialect.name != \"postgresql\":", "    if lifecycle_engine.dialect.name != \"postgresql\":", 1)
    op = op.replace("    async with engine.connect() as conn:", "    async with lifecycle_engine.connect() as conn:", 1)
if "from celerp.db import lifecycle_engine" not in op:
    raise SystemExit("operation_lock: lifecycle engine normalization failed")
write(op_path, op)

# Any direct relay token fetch must have a local owner. Pending ownership is
# allowed because activation itself needs credentials before activated_at is set.
relay_path = "celerp/connectors/relay_token.py"
relay = read(relay_path)
fetch_guard = '''async def fetch_context(company_id: str, connector_name: str) -> "ConnectorContext | None":
    import httpx

    from celerp.connectors.base import ConnectorContext
'''
fetch_guard_new = '''async def fetch_context(company_id: str, connector_name: str) -> "ConnectorContext | None":
    import httpx

    from celerp.connectors.base import ConnectorContext
    from celerp.connectors.ownership import get_connector_config

    if await get_connector_config(
        company_id, connector_name, adopt_single_company=False
    ) is None:
        return None
'''
if fetch_guard in relay:
    relay = relay.replace(fetch_guard, fetch_guard_new, 1)
elif "adopt_single_company=False" not in relay:
    raise SystemExit("relay_token: fetch_context ownership guard missing")
write(relay_path, relay)

# Manual connector sync is a material integration operation now that inbound
# Woo orders post accounting and inventory side effects.
routes_path = "default_modules/celerp-connectors/celerp_connectors/routes.py"
routes = read(routes_path)
old_sig = '''async def trigger_sync(
    connector_name: str,
    payload: SyncRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    session: AsyncSession = Depends(get_session),
) -> SyncResponse:
'''
new_sig = '''async def trigger_sync(
    connector_name: str,
    payload: SyncRequest,
    company_id: Annotated[str, Depends(get_current_company_id)],
    _: None = require_permission("manage_integrations"),
    session: AsyncSession = Depends(get_session),
) -> SyncResponse:
'''
if old_sig in routes:
    routes = routes.replace(old_sig, new_sig, 1)
elif 'trigger_sync(' in routes and 'require_permission("manage_integrations")' not in routes[
    routes.index("async def trigger_sync"):routes.index("# ── Credential management", routes.index("async def trigger_sync"))
]:
    raise SystemExit("connectors routes: trigger_sync permission transform failed")

trigger_anchor = '''    try:
        connector = connectors.get(connector_name)
'''
trigger_guard = '''    from celerp.connectors.ownership import get_active_connector_config
    if await get_active_connector_config(company_id, connector_name) is None:
        raise HTTPException(
            status_code=409, detail="Connector is not active for this company"
        )

    try:
        connector = connectors.get(connector_name)
'''
trigger_slice = routes[routes.index("async def trigger_sync"):routes.index("# ── Credential management")]
if "Connector is not active for this company" not in trigger_slice:
    local = routes.index("async def trigger_sync")
    pos = routes.index(trigger_anchor, local)
    routes = routes[:pos] + trigger_guard + routes[pos + len(trigger_anchor):]
write(routes_path, routes)

# Pending ownership must never light up catalog controls.
inv_ui_path = "ui/routes/inventory.py"
inv_ui = read(inv_ui_path)
old_connected = '''            rows = await session.execute(sa.select(ConnectorConfig.connector).where(
                ConnectorConfig.company_id == str(company_id)
            ))
'''
new_connected = '''            rows = await session.execute(sa.select(ConnectorConfig.connector).where(
                ConnectorConfig.company_id == str(company_id),
                ConnectorConfig.activated_at.is_not(None),
            ))
'''
if old_connected in inv_ui:
    inv_ui = inv_ui.replace(old_connected, new_connected, 1)
elif "ConnectorConfig.activated_at.is_not(None)" not in inv_ui:
    raise SystemExit("inventory UI: active connector filter missing")
write(inv_ui_path, inv_ui)

# Serialize customer-stock allocation and invalidate any inventory objects loaded
# before the lease, so validation cannot use Woo's earlier in-session snapshot.
write("celerp/services/inventory_allocation.py", r'''# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Serialization for customer-stock allocation decisions."""
from __future__ import annotations

import sqlalchemy as sa


async def lock_sales_allocation(session, company_id) -> None:
    """Serialize reserve/fulfill availability decisions for one company."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"sales-allocation:{company_id}"},
        )
''')

doc_routes_path = "default_modules/celerp-docs/celerp_docs/routes.py"
doc_routes = read(doc_routes_path)
if "await lock_sales_allocation(session, row.company_id)" not in doc_routes:
    reserve_pattern = re.compile(
        r'(async def _reserve_lines_impl\(.*?)(    unit_map = await _get_unit_map\(session, row\.company_id\)\n)',
        re.S,
    )
    doc_routes, n = reserve_pattern.subn(
        r'\1    from celerp.services.inventory_allocation import lock_sales_allocation\n'
        r'    await lock_sales_allocation(session, row.company_id)\n'
        r'    session.expire_all()\n'
        r'\2',
        doc_routes, count=1,
    )
    if n != 1:
        raise SystemExit("docs routes: reserve allocation boundary missing")

if "await lock_sales_allocation(session, company_id)" not in doc_routes:
    fulfill_pattern = re.compile(
        r'(async def _fulfill_lines_impl\(.*?)(    _unit_map = await _get_unit_map\(session, company_id\)\n)',
        re.S,
    )
    doc_routes, n = fulfill_pattern.subn(
        r'\1    from celerp.services.inventory_allocation import lock_sales_allocation\n'
        r'    await lock_sales_allocation(session, company_id)\n'
        r'    session.expire_all()\n'
        r'\2',
        doc_routes, count=1,
    )
    if n != 1:
        raise SystemExit("docs routes: fulfillment allocation boundary missing")
write(doc_routes_path, doc_routes)

# Discoverable Woo hooks are installation-specific. Legacy generic hooks are
# cleaned only by their persisted IDs, never by a broad name match.
woo_path = "celerp/connectors/woocommerce.py"
woo = read(woo_path)
if "from celerp.config import ensure_instance_id" not in woo:
    import_anchor = "import httpx\n"
    if import_anchor not in woo:
        raise SystemExit("woocommerce: import anchor missing")
    woo = woo.replace(import_anchor, import_anchor + "from celerp.config import ensure_instance_id\n", 1)
woo = woo.replace(
    '"name": f"Celerp {topic}",',
    '"name": f"Celerp {ensure_instance_id()} {topic}",',
)
write(woo_path, woo)

# Reconciliation must retire cached pre-namespace webhook IDs before replacing
# local state, while discovery handles crash-created namespaced hooks.
settings_path = "ui/routes/settings_connectors.py"
settings_src = read(settings_path)
old_reconcile = "    ids = await connector.reconcile_webhooks(ctx, delivery_url, secret)\n"
if old_reconcile in settings_src:
    settings_src = settings_src.replace(
        old_reconcile,
        '''    config = await _get_connector_config(company_id, "woocommerce")
    ids = await connector.reconcile_webhooks(
        ctx, delivery_url, secret,
        known_ids=(config.webhook_ids if config else []),
    )
''',
        1,
    )
if "known_ids=(config.webhook_ids if config else [])" not in settings_src:
    raise SystemExit("settings connectors: known Woo webhook IDs not passed to reconciliation")
write(settings_path, settings_src)

# Final executable regression contracts for the owning boundaries.
contract_path = "tests/test_connector_hardening_contract.py"
contract = read(contract_path) if (ROOT / contract_path).exists() else ""
extra = r'''

def test_generic_sync_requires_active_owned_integration():
    source = _text("default_modules/celerp-connectors/celerp_connectors/routes.py")
    trigger = source[source.index("async def trigger_sync"):source.index("# ── Credential management")]
    assert 'require_permission("manage_integrations")' in trigger
    assert "Connector is not active for this company" in trigger


def test_catalog_controls_ignore_pending_connector_claims():
    source = _text("ui/routes/inventory.py")
    assert "ConnectorConfig.activated_at.is_not(None)" in source


def test_sales_allocation_refreshes_preloaded_inventory():
    source = _text("default_modules/celerp-docs/celerp_docs/routes.py")
    assert source.count("await lock_sales_allocation(") >= 2
    assert source.count("session.expire_all()") >= 2


def test_relay_context_requires_local_connector_owner():
    source = _text("celerp/connectors/relay_token.py")
    assert "adopt_single_company=False" in source


def test_woo_webhook_discovery_is_installation_namespaced():
    source = _text("celerp/connectors/woocommerce.py")
    assert 'Celerp {ensure_instance_id()}' in source
'''
if "test_generic_sync_requires_active_owned_integration" not in contract:
    contract += extra
write(contract_path, contract)


print("PR340 hardening patch applied")

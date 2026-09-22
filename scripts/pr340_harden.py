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
    if engine.dialect.name != "postgresql":
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

    async with engine.connect() as conn:
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
# Connector operation lease becomes concurrency authority; SyncRun stays audit.
# ---------------------------------------------------------------------------
replace_once(
    "celerp/connectors/sync_runner.py",
    "from datetime import datetime, timedelta, timezone\n",
    "from datetime import datetime, timedelta, timezone\n",
)
# Insert helpers after log.
replace_once(
    "celerp/connectors/sync_runner.py",
    "log = logging.getLogger(__name__)\n\n_SYNC_METHODS",
    '''log = logging.getLogger(__name__)


async def _interrupt_abandoned_runs(company_id: str, connector: str) -> None:
    """Close audit rows left running after a process died.

    The caller owns the connector operation lease, so no live operation for this
    company/platform can still own these rows.
    """
    import sqlalchemy as sa
    from celerp.db import get_session_ctx

    now = datetime.now(timezone.utc)
    async with get_session_ctx() as session:
        await session.execute(
            sa.update(SyncRun)
            .where(
                SyncRun.company_id == company_id,
                SyncRun.connector == connector,
                SyncRun.finished_at.is_(None),
            )
            .values(
                finished_at=now,
                status="failed",
                errors_json=json.dumps(["Interrupted before completion"]),
            )
        )
        await session.commit()


_SYNC_METHODS''',
)
# Replace run_connector_sync.
regex_once(
    "celerp/connectors/sync_runner.py",
    r'async def run_connector_sync\(\n    connector: ConnectorBase,\n    ctx: ConnectorContext,\n    direction: SyncDirection,\n\) -> list\[SyncResult\]:\n    """Execute a connector\'s canonical plan through the audited per-entity runner\."""\n    return \[\n        await run_sync\(connector, ctx, entity, direction=direction\)\n        for entity in sync_plan\(connector, direction\)\n    \]\n',
    '''async def run_connector_sync(
    connector: ConnectorBase,
    ctx: ConnectorContext,
    direction: SyncDirection,
) -> list[SyncResult]:
    """Execute one canonical plan under the company/platform operation lease."""
    from celerp.connectors.operation_lock import ConnectorBusy, connector_operation

    try:
        async with connector_operation(ctx.company_id, connector.name):
            await _interrupt_abandoned_runs(ctx.company_id, connector.name)
            return [
                await run_sync(
                    connector, ctx, entity, direction=direction, _operation_locked=True
                )
                for entity in sync_plan(connector, direction)
            ]
    except ConnectorBusy:
        first = sync_plan(connector, direction)
        entity = first[0] if first else SyncEntity.PRODUCTS
        return [SyncResult(
            entity=entity,
            direction=direction,
            errors=[f"{connector.name} sync already in progress"],
        )]
''',
)
# Add internal param + direct lease wrapper to run_sync.
replace_once(
    "celerp/connectors/sync_runner.py",
    "    direction: SyncDirection | None = None,\n) -> SyncResult:\n",
    "    direction: SyncDirection | None = None,\n    _operation_locked: bool = False,\n) -> SyncResult:\n",
)
needle = '''    # Direction gate
    if direction and not entity_allowed(entity, direction):
'''
insert = '''    if not _operation_locked:
        from celerp.connectors.operation_lock import ConnectorBusy, connector_operation
        try:
            async with connector_operation(ctx.company_id, connector.name):
                await _interrupt_abandoned_runs(ctx.company_id, connector.name)
                return await run_sync(
                    connector, ctx, entity, since=since, direction=direction,
                    _operation_locked=True,
                )
        except ConnectorBusy:
            intended = direction if isinstance(direction, SyncDirection) else connector.direction
            return SyncResult(
                entity=entity,
                direction=intended,
                errors=[f"{connector.name} sync already in progress"],
            )

    # Direction gate
    if direction and not entity_allowed(entity, direction):
'''
replace_once("celerp/connectors/sync_runner.py", needle, insert)

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
        base_url, auth = _base_url(ctx), _auth(ctx)
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
                    and str(h.get("name") or "").startswith("Celerp ")
                )
                if len(batch) < 100:
                    break
                page += 1
            return out

    async def reconcile_webhooks(
        self, ctx: ConnectorContext, webhook_url: str, secret: str
    ) -> list[str]:
        """Converge Celerp-owned Woo hooks after retries or interrupted setup."""
        base_url, auth = _base_url(ctx), _auth(ctx)
        existing = await self._owned_webhooks(ctx, webhook_url)
        by_topic: dict[str, list[dict]] = {}
        for hook in existing:
            by_topic.setdefault(str(hook.get("topic") or ""), []).append(hook)

        kept: list[str] = []
        async with RateLimitedClient() as client:
            for topic in self._WEBHOOK_TOPICS:
                matches = by_topic.pop(topic, [])
                body = {
                    "name": f"Celerp {topic}",
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
    '''    from celerp.connectors.ownership import get_active_connector_config
    config = await get_active_connector_config(company_id, platform)
    if config is None:
        raise RuntimeError("Connector is not active for this company")
    direction = (
        SyncDirection.INBOUND
        if activation else SyncDirection(config.direction)
    )

    async def _do_sync():
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
new = '''    # Relay connected is installation-scoped. Local ownership is authoritative for
    # whether this ERP company may operate the connector.
    from celerp.connectors.ownership import activate_connector
    configs: dict[str, object] = {}
    owned_catalog: list[dict] = []
    for raw in catalog:
        c = dict(raw)
        cfg = await _get_connector_config(company_id, c["id"])
        if (
            cfg is not None
            and cfg.activated_at is None
            and raw.get("connected")
            and c.get("auth_type") == "oauth"
        ):
            # The explicit pre-OAuth claim identifies the company; relay connected
            # now proves authorization completed.
            cfg = await activate_connector(company_id, c["id"])
        local_connected = bool(
            raw.get("connected") and cfg is not None and cfg.activated_at is not None
        )
        c["connected"] = local_connected
        if local_connected:
            configs[c["id"]] = cfg
        owned_catalog.append(c)
    catalog = owned_catalog

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

print("PR340 hardening patch applied")

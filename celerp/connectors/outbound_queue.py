# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Durable near-real-time outbound stock delivery for website connectors."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from celerp.connectors.base import SyncDirection
from celerp.db import get_session_ctx
from celerp.models.company import Company
from celerp.models.connector_config import ConnectorConfig, OutboundQueue
from celerp.models.projections import Projection


log = logging.getLogger(__name__)


def _woo_link(state: dict) -> dict:
    links = state.get("external_links") or {}
    link = links.get("woocommerce") if isinstance(links, dict) else None
    if isinstance(link, dict) and link.get("product_id") not in (None, ""):
        return dict(link)
    idem = str(state.get("idempotency_key") or "")
    parts = idem.split(":")
    if len(parts) >= 2 and parts[0] == "woocommerce":
        out = {"product_id": parts[1], "sync_enabled": True}
        if len(parts) >= 3 and parts[2]:
            out["variation_id"] = parts[2]
        return out
    return {}


def _identity(link: dict) -> str:
    product_id = str(link.get("product_id") or "")
    variation_id = link.get("variation_id")
    return product_id + (f":{variation_id}" if variation_id not in (None, "") else "")


async def adopt_legacy_connector_configs() -> None:
    """Adopt installation-scoped legacy rows only when ownership is unambiguous."""
    from celerp.config import ensure_instance_id
    from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector_ownership

    legacy_id = ensure_instance_id()
    async with get_session_ctx() as session:
        company_ids = {
            str(value)
            for value in (await session.execute(sa.select(Company.id))).scalars().all()
        }
        legacy_connectors = set((await session.execute(
            sa.select(ConnectorConfig.connector).where(
                ConnectorConfig.company_id == legacy_id
            )
        )).scalars().all())

        for connector in legacy_connectors:
            owner_ids = {
                str(value)
                for value in (await session.execute(
                    sa.select(ConnectorConfig.company_id).where(
                        ConnectorConfig.connector == connector,
                        ConnectorConfig.company_id != legacy_id,
                    )
                )).scalars().all()
            }
            invalid_owners = owner_ids - company_ids
            if invalid_owners:
                log.error(
                    "connector legacy adoption blocked for %s: invalid owner rows %s",
                    connector, sorted(invalid_owners),
                )
                continue
            if len(owner_ids) == 1:
                company_id = next(iter(owner_ids))
            elif not owner_ids and len(company_ids) == 1:
                company_id = next(iter(company_ids))
            else:
                log.warning(
                    "connector legacy adoption deferred for %s: ownership is ambiguous",
                    connector,
                )
                continue
            try:
                await claim_connector_ownership(
                    session, company_id, connector, create=False
                )
            except ConnectorOwnershipError as exc:
                log.error(
                    "connector legacy adoption blocked for %s: %s", connector, exc
                )
        await session.commit()


async def enqueue_item_change(session, entry, *, previous_state: dict | None = None) -> None:
    """Queue every affected enabled Woo identity in the caller's transaction."""
    if entry.entity_type != "item" or entry.source == "connector":
        return
    company_id = str(entry.company_id)
    config = await session.scalar(
        sa.select(ConnectorConfig).where(
            ConnectorConfig.company_id == company_id,
            ConnectorConfig.connector == "woocommerce",
            ConnectorConfig.direction.in_([
                SyncDirection.OUTBOUND.value, SyncDirection.BOTH.value
            ]),
        ).limit(1)
    )
    if config is None:
        return

    row = await session.get(
        Projection, {"company_id": entry.company_id, "entity_id": entry.entity_id}
    )
    if row is None or row.entity_type != "item":
        return

    current_state = dict(row.state or {})
    states = [state for state in (previous_state, current_state) if state]
    anchor_ids = {
        str(state.get("catalog_item_id"))
        for state in states
        if state.get("catalog_item_id")
    }
    direct_linked = any(_woo_link(state) for state in states)
    if direct_linked:
        anchor_ids.add(str(entry.entity_id))

    identities: set[str] = set()
    for state in states:
        link = _woo_link(state)
        if (
            link
            and link.get("sync_enabled") is not False
            and link.get("remote_deleted") is not True
            and link.get("inventory_sync_paused") is not True
        ):
            key = _identity(link)
            if key:
                identities.add(key)

    for anchor_id in anchor_ids:
        candidate = await session.get(
            Projection,
            {"company_id": entry.company_id, "entity_id": anchor_id},
        )
        if candidate is None or candidate.entity_type != "item":
            continue
        link = _woo_link(candidate.state or {})
        if (
            link
            and link.get("sync_enabled") is not False
            and link.get("remote_deleted") is not True
            and link.get("inventory_sync_paused") is not True
        ):
            key = _identity(link)
            if key:
                identities.add(key)

    if not anchor_ids and not direct_linked:
        sku_keys = {
            str(state.get("sku") or "").strip().casefold()
            for state in states
            if str(state.get("sku") or "").strip()
        }
        if sku_keys:
            sku_expr = sa.func.lower(Projection.state.op("->>")("sku"))
            linked = (await session.execute(
                sa.select(Projection).where(
                    Projection.company_id == entry.company_id,
                    Projection.entity_type == "item",
                    sku_expr.in_(sku_keys),
                    sa.text(
                        "("
                        "NULLIF(state -> 'external_links' -> 'woocommerce' ->> 'product_id', '') "
                        "IS NOT NULL OR state ->> 'idempotency_key' LIKE 'woocommerce:%'"
                        ")"
                    ),
                )
            )).scalars().all()
            for candidate in linked:
                link = _woo_link(candidate.state or {})
                if (
                    link
                    and link.get("sync_enabled") is not False
                    and link.get("remote_deleted") is not True
                    and link.get("inventory_sync_paused") is not True
                ):
                    key = _identity(link)
                    if key:
                        identities.add(key)

    for identity in identities:
        session.add(OutboundQueue(
            company_id=company_id,
            connector="woocommerce",
            entity_type="inventory",
            entity_id=identity,
            status="pending",
            retry_count=0,
        ))


async def process_outbound_queue_once(limit: int = 100) -> int:
    """Process queued identities, serialized across every API worker.

    The advisory transaction lock spans the fresh Celerp stock read and remote
    WooCommerce write. A newer queue row inserted while that lock is held is not
    deleted with the current snapshot and runs afterward, so an older HTTP write
    cannot complete after a newer one for the same remote product.
    """
    now = datetime.now(timezone.utc)
    async with get_session_ctx() as session:
        rows = (await session.execute(
            sa.select(OutboundQueue)
            .where(
                OutboundQueue.status == "pending",
                sa.or_(
                    OutboundQueue.next_retry_at.is_(None),
                    OutboundQueue.next_retry_at <= now,
                ),
            )
            .order_by(OutboundQueue.id)
            .limit(limit)
        )).scalars().all()

    identities = list(dict.fromkeys(
        (row.company_id, row.connector, row.entity_id) for row in rows
    ))
    processed = 0

    for company_id, connector_name, identity in identities:
        async with get_session_ctx() as session:
            from celerp.connectors.ownership import (
                ConnectorOwnershipAmbiguousError,
                ConnectorOwnershipError,
                lock_connector_operation,
            )
            try:
                await lock_connector_operation(
                    session, company_id, connector_name, require_owner=True
                )
            except ConnectorOwnershipAmbiguousError as exc:
                retry_at = datetime.now(timezone.utc) + timedelta(minutes=1)
                pending = (await session.execute(
                    sa.select(OutboundQueue).where(
                        OutboundQueue.company_id == company_id,
                        OutboundQueue.connector == connector_name,
                        OutboundQueue.entity_id == identity,
                        OutboundQueue.status == "pending",
                    )
                )).scalars().all()
                for row in pending:
                    row.next_retry_at = retry_at
                    row.error_message = str(exc)[:2000]
                await session.commit()
                continue
            except ConnectorOwnershipError:
                await session.execute(
                    sa.delete(OutboundQueue).where(
                        OutboundQueue.company_id == company_id,
                        OutboundQueue.connector == connector_name,
                        OutboundQueue.entity_id == identity,
                    )
                )
                await session.commit()
                continue

            await session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"outbound:{company_id}:{connector_name}:{identity}"},
            )

            # Re-read every pending row visible after acquiring the cross-process
            # identity lock. A future retry on any generation defers newer rows too.
            locked_rows = (await session.execute(
                sa.select(OutboundQueue)
                .where(
                    OutboundQueue.company_id == company_id,
                    OutboundQueue.connector == connector_name,
                    OutboundQueue.entity_id == identity,
                    OutboundQueue.status == "pending",
                )
                .order_by(OutboundQueue.id)
            )).scalars().all()
            if not locked_rows:
                continue

            retry_now = datetime.now(timezone.utc)
            blocked_until = max(
                (
                    row.next_retry_at
                    for row in locked_rows
                    if row.next_retry_at is not None and row.next_retry_at > retry_now
                ),
                default=None,
            )
            if blocked_until is not None:
                keeper = locked_rows[-1]
                stale_ids = [row.id for row in locked_rows[:-1]]
                if stale_ids:
                    await session.execute(
                        sa.delete(OutboundQueue).where(OutboundQueue.id.in_(stale_ids))
                    )
                keeper.retry_count = max(row.retry_count for row in locked_rows)
                keeper.next_retry_at = blocked_until
                keeper.error_message = next(
                    (row.error_message for row in reversed(locked_rows) if row.error_message),
                    None,
                )
                await session.commit()
                continue

            ids = [row.id for row in locked_rows]
            config = await session.scalar(
                sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == company_id,
                    ConnectorConfig.connector == connector_name,
                ).limit(1)
            )
            if config is None or config.direction == SyncDirection.INBOUND.value:
                await session.execute(
                    sa.delete(OutboundQueue).where(OutboundQueue.id.in_(ids))
                )
                await session.commit()
                processed += len(ids)
                continue

            try:
                from celerp.connectors.registry import get as get_connector
                from celerp.connectors.relay_token import fetch_context

                ctx = await fetch_context(company_id, connector_name)
                if ctx is None:
                    raise RuntimeError("connector credentials are temporarily unavailable")
                connector = get_connector(connector_name)
                push_one = getattr(connector, "sync_inventory_identity_out", None)
                if push_one is None:
                    await session.execute(
                        sa.delete(OutboundQueue).where(OutboundQueue.id.in_(ids))
                    )
                else:
                    result = await push_one(ctx, identity)
                    if result.errors:
                        raise RuntimeError("; ".join(result.errors))
                    await session.execute(
                        sa.delete(OutboundQueue).where(OutboundQueue.id.in_(ids))
                    )
                await session.commit()
            except Exception as exc:
                retry_now = datetime.now(timezone.utc)
                retry_count = max((row.retry_count for row in locked_rows), default=0) + 1
                delay = min(3600, 5 * (2 ** min(retry_count, 9)))
                retry_at = retry_now + timedelta(seconds=delay)
                keeper = locked_rows[-1]
                stale_ids = [row.id for row in locked_rows[:-1]]
                if stale_ids:
                    await session.execute(
                        sa.delete(OutboundQueue).where(OutboundQueue.id.in_(stale_ids))
                    )
                keeper.retry_count = retry_count
                keeper.next_retry_at = retry_at
                keeper.error_message = str(exc)[:2000]
                keeper.status = "pending"
                await session.commit()

            processed += len(ids)
    return processed


async def outbound_queue_loop() -> None:
    """Continuously drain durable outbound work; daily reconciliation remains the backstop."""
    while True:
        try:
            processed = await process_outbound_queue_once()
            await asyncio.sleep(1 if processed else 5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("outbound queue iteration failed: %s", exc)
            await asyncio.sleep(5)

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Durable near-real-time outbound stock delivery for website connectors."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from celerp.connectors.base import SyncDirection
from celerp.db import get_session_ctx
from celerp.models.company import Company
from celerp.models.connector_config import ConnectorConfig, OutboundQueue
from celerp.models.projections import Projection


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


async def adopt_single_company_legacy_configs() -> None:
    """Self-heal old installation-scoped connector rows when ownership is unambiguous."""
    from celerp.config import ensure_instance_id

    legacy_id = ensure_instance_id()
    async with get_session_ctx() as session:
        companies = (await session.execute(sa.select(Company.id).limit(2))).scalars().all()
        if len(companies) != 1:
            return
        company_id = str(companies[0])
        if company_id == legacy_id:
            return
        legacy_rows = (await session.execute(
            sa.select(ConnectorConfig).where(ConnectorConfig.company_id == legacy_id)
        )).scalars().all()
        changed = False
        for legacy in legacy_rows:
            current = await session.scalar(
                sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == company_id,
                    ConnectorConfig.connector == legacy.connector,
                ).limit(1)
            )
            if current is None:
                legacy.company_id = company_id
            else:
                await session.delete(legacy)
            changed = True
        if changed:
            await session.commit()


async def enqueue_item_change(session, entry) -> None:
    """Queue affected enabled Woo product identities in the caller's transaction."""
    if entry.entity_type != "item" or entry.source in {"connector", "connector_ui"}:
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
    sku = str((row.state or {}).get("sku") or "").strip()
    if not sku:
        return

    family = (await session.execute(
        sa.select(Projection).where(
            Projection.company_id == entry.company_id,
            Projection.entity_type == "item",
            sa.func.lower(Projection.state["sku"].as_string()) == sku.casefold(),
        )
    )).scalars().all()
    identities: set[str] = set()
    for candidate in family:
        link = _woo_link(candidate.state or {})
        if (
            link
            and link.get("sync_enabled") is not False
            and link.get("remote_deleted") is not True
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


async def _finish(ids: list[int], *, error: str | None = None) -> None:
    async with get_session_ctx() as session:
        if error is None:
            await session.execute(
                sa.delete(OutboundQueue).where(OutboundQueue.id.in_(ids))
            )
        else:
            rows = (await session.execute(
                sa.select(OutboundQueue).where(OutboundQueue.id.in_(ids))
            )).scalars().all()
            now = datetime.now(timezone.utc)
            for row in rows:
                row.retry_count += 1
                delay = min(3600, 5 * (2 ** min(row.retry_count, 9)))
                row.next_retry_at = now + timedelta(seconds=delay)
                row.error_message = error[:2000]
                row.status = "pending"
        await session.commit()


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
            await session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"outbound:{company_id}:{connector_name}:{identity}"},
            )

            # Re-read only rows visible after we acquired the cross-process lock.
            # Rows inserted after this snapshot remain pending for the next pass.
            locked_rows = (await session.execute(
                sa.select(OutboundQueue)
                .where(
                    OutboundQueue.company_id == company_id,
                    OutboundQueue.connector == connector_name,
                    OutboundQueue.entity_id == identity,
                    OutboundQueue.status == "pending",
                    sa.or_(
                        OutboundQueue.next_retry_at.is_(None),
                        OutboundQueue.next_retry_at <= datetime.now(timezone.utc),
                    ),
                )
                .order_by(OutboundQueue.id)
            )).scalars().all()
            if not locked_rows:
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
                for row in locked_rows:
                    row.retry_count += 1
                    delay = min(3600, 5 * (2 ** min(row.retry_count, 9)))
                    row.next_retry_at = retry_now + timedelta(seconds=delay)
                    row.error_message = str(exc)[:2000]
                    row.status = "pending"
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
        except Exception:
            await asyncio.sleep(5)

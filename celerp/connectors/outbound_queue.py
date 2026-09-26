# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Durable outbound delivery: website stock levels and accounting invoices."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from celerp.connectors.base import SyncDirection, SyncResult
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
    """Adopt legacy connector rows only when ownership is unambiguous."""
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
            str(state.get("sku") or "").strip().lower()
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


class OutboundRejected(Exception):
    """The platform refused the change outright; retrying cannot succeed."""


class OutboundNeedsReconciliation(Exception):
    """The platform holds a record that conflicts with this change. The work is
    parked until the user resolves it; it is never retried as a new create."""


@dataclass
class OutboundOperation:
    """One queued identity. `state` is what a handler persisted on an earlier
    attempt (for example the frozen request of an external create)."""

    row_id: int
    identity: str
    state: dict = field(default_factory=dict)

    async def save(self, state: dict) -> None:
        """Commit `state` before the caller contacts the platform, so a crash
        after the call still finds it."""
        async with get_session_ctx() as session:
            saved = await session.execute(
                sa.update(OutboundQueue)
                .where(OutboundQueue.id == self.row_id)
                .values(payload_json=json.dumps(state))
            )
            await session.commit()
        if saved.rowcount != 1:
            raise RuntimeError("the queued change was removed")
        self.state = state


@dataclass
class OutboundOutcome:
    rows: int = 0
    result: SyncResult | None = None
    error: str | None = None


_OUTBOUND_HANDLERS = {
    ("woocommerce", "inventory"):
        lambda connector, ctx, op: connector.sync_inventory_identity_out(ctx, op.identity),
    ("xero", "invoice"):
        lambda connector, ctx, op: connector.sync_invoice_identity_out(ctx, op),
}


async def enqueue_outbound(
    company_id: str, connector: str, entity_type: str, entity_id: str
) -> None:
    """Queue one identity unless it is already queued. Concurrent enqueues may
    both insert; the processor keeps one row per identity."""
    async with get_session_ctx() as session:
        existing = await session.scalar(
            sa.select(OutboundQueue.id).where(
                OutboundQueue.company_id == company_id,
                OutboundQueue.connector == connector,
                OutboundQueue.entity_type == entity_type,
                OutboundQueue.entity_id == entity_id,
            ).limit(1)
        )
        if existing is None:
            session.add(OutboundQueue(
                company_id=company_id,
                connector=connector,
                entity_type=entity_type,
                entity_id=entity_id,
                status="pending",
                retry_count=0,
            ))
            await session.commit()


async def process_outbound_identity(
    company_id: str,
    connector_name: str,
    entity_type: str,
    identity: str,
    *,
    ctx=None,
) -> OutboundOutcome:
    """Deliver one queued identity, serialized across every API worker.

    The advisory transaction lock spans the fresh Celerp read and the remote
    write. A newer queue row inserted while that lock is held is not deleted
    with the current snapshot and runs afterward, so an older write cannot
    complete after a newer one for the same remote record.

    Without `ctx` (the background queue) this takes the connector's ownership
    lock, honours retry backoff and loads the connection. With `ctx` the caller
    is a sync run that already holds that lock and has the connection; parked
    rows are included so a manual sync rechecks them.
    """
    from celerp.connectors.ownership import (
        ConnectorOwnershipAmbiguousError,
        ConnectorOwnershipError,
        lock_connector_operation,
    )

    identity_rows = (
        OutboundQueue.company_id == company_id,
        OutboundQueue.connector == connector_name,
        OutboundQueue.entity_type == entity_type,
        OutboundQueue.entity_id == identity,
    )
    statuses = ("pending",) if ctx is None else ("pending", "blocked")

    async with get_session_ctx() as session:
        if ctx is None:
            try:
                await lock_connector_operation(
                    session, company_id, connector_name, require_owner=True
                )
            except ConnectorOwnershipAmbiguousError as exc:
                retry_at = datetime.now(timezone.utc) + timedelta(minutes=1)
                pending = (await session.execute(
                    sa.select(OutboundQueue).where(
                        *identity_rows, OutboundQueue.status == "pending"
                    )
                )).scalars().all()
                for row in pending:
                    row.next_retry_at = retry_at
                    row.error_message = str(exc)[:2000]
                await session.commit()
                return OutboundOutcome(error=str(exc))
            except ConnectorOwnershipError as exc:
                await session.execute(sa.delete(OutboundQueue).where(*identity_rows))
                await session.commit()
                return OutboundOutcome(error=str(exc))

        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"outbound:{company_id}:{connector_name}:{entity_type}:{identity}"},
        )

        # Re-read every row visible after acquiring the cross-process identity
        # lock. A future retry on any generation defers newer rows too.
        locked_rows = (await session.execute(
            sa.select(OutboundQueue)
            .where(*identity_rows, OutboundQueue.status.in_(statuses))
            .order_by(OutboundQueue.id)
        )).scalars().all()
        if not locked_rows:
            return OutboundOutcome()

        # A row carrying an operation's state is the operation; the others are
        # later requests for the same identity.
        keeper = next((row for row in locked_rows if row.payload_json), locked_rows[-1])
        stale_ids = [row.id for row in locked_rows if row is not keeper]
        ids = [row.id for row in locked_rows]

        async def retain(**changes) -> None:
            if stale_ids:
                await session.execute(
                    sa.delete(OutboundQueue).where(OutboundQueue.id.in_(stale_ids))
                )
            for name, value in changes.items():
                setattr(keeper, name, value)
            await session.commit()

        async def clear() -> None:
            await session.execute(sa.delete(OutboundQueue).where(OutboundQueue.id.in_(ids)))
            await session.commit()

        if ctx is None:
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
                await retain(
                    retry_count=max(row.retry_count for row in locked_rows),
                    next_retry_at=blocked_until,
                    error_message=next(
                        (row.error_message for row in reversed(locked_rows) if row.error_message),
                        None,
                    ),
                )
                return OutboundOutcome()

            config = await session.scalar(
                sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == company_id,
                    ConnectorConfig.connector == connector_name,
                ).limit(1)
            )
            if config is None or config.direction == SyncDirection.INBOUND.value:
                await clear()
                return OutboundOutcome(rows=len(ids))

        handler = _OUTBOUND_HANDLERS.get((connector_name, entity_type))
        if handler is None:
            await clear()
            return OutboundOutcome(rows=len(ids))

        operation = OutboundOperation(
            row_id=keeper.id,
            identity=identity,
            state=json.loads(keeper.payload_json) if keeper.payload_json else {},
        )
        try:
            from celerp.connectors.registry import get as get_connector

            if ctx is None:
                from celerp.connectors.relay_token import fetch_context

                ctx = await fetch_context(company_id, connector_name)
                if ctx is None:
                    raise RuntimeError("connector credentials are temporarily unavailable")
            result = await handler(get_connector(connector_name), ctx, operation)
            if result.errors:
                raise RuntimeError("; ".join(result.errors))
        except OutboundRejected as exc:
            await clear()
            return OutboundOutcome(rows=len(ids), error=str(exc))
        except OutboundNeedsReconciliation as exc:
            await retain(status="blocked", next_retry_at=None, error_message=str(exc)[:2000])
            return OutboundOutcome(rows=len(ids), error=str(exc))
        except Exception as exc:
            retry_count = max((row.retry_count for row in locked_rows), default=0) + 1
            delay = min(3600, 5 * (2 ** min(retry_count, 9)))
            await retain(
                retry_count=retry_count,
                next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
                error_message=str(exc)[:2000],
                status="pending",
            )
            return OutboundOutcome(rows=len(ids), error=str(exc))
        await clear()
        return OutboundOutcome(rows=len(ids), result=result)


async def process_outbound_queue_once(limit: int = 100) -> int:
    """Process due queued identities; returns the number of rows handled."""
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
        (row.company_id, row.connector, row.entity_type, row.entity_id) for row in rows
    ))
    processed = 0
    for company_id, connector_name, entity_type, identity in identities:
        outcome = await process_outbound_identity(
            company_id, connector_name, entity_type, identity
        )
        processed += outcome.rows
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

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from celerp.connectors.outbound_queue import enqueue_item_change
from celerp.models.company import Company
from celerp.models.connector_config import ConnectorConfig, OutboundQueue
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection


@pytest.mark.asyncio
async def test_local_linked_item_change_enqueues_woocommerce_stock(session):
    company_id = uuid.uuid4()
    session.add(Company(
        id=company_id,
        name="Queue Test",
        slug=f"queue-test-{company_id.hex[:8]}",
        settings={},
    ))
    session.add(ConnectorConfig(
        company_id=str(company_id), connector="woocommerce", direction="both"
    ))
    item_id = "item:q"
    now = datetime.now(timezone.utc)
    session.add(Projection(
        company_id=company_id,
        entity_id=item_id,
        entity_type="item",
        version=1,
        created_at=now,
        updated_at=now,
        state={
            "sku": "Q-1",
            "quantity": 4,
            "status": "available",
            "external_links": {
                "woocommerce": {
                    "product_id": "17",
                    "sync_enabled": True,
                    "manage_stock": True,
                }
            },
        },
    ))
    await session.flush()
    entry = LedgerEntry(
        company_id=company_id,
        entity_id=item_id,
        entity_type="item",
        event_type="item.quantity.adjusted",
        data={"old_quantity": 3, "new_quantity": 4},
        source="ui",
        idempotency_key=f"t-{uuid.uuid4()}",
    )
    await enqueue_item_change(session, entry)
    await session.flush()
    queued = (await session.execute(
        sa.select(OutboundQueue).where(OutboundQueue.company_id == str(company_id))
    )).scalars().all()
    assert len(queued) == 1
    assert queued[0].connector == "woocommerce"
    assert queued[0].entity_id == "17"


@pytest.mark.asyncio
async def test_connector_origin_item_change_does_not_requeue(session):
    company_id = uuid.uuid4()
    entry = LedgerEntry(
        company_id=company_id,
        entity_id="item:x",
        entity_type="item",
        event_type="item.updated",
        data={"fields_changed": {}},
        source="connector",
        idempotency_key=f"t-{uuid.uuid4()}",
    )
    await enqueue_item_change(session, entry)
    queued = (await session.execute(
        sa.select(OutboundQueue).where(OutboundQueue.company_id == str(company_id))
    )).scalars().all()
    assert queued == []

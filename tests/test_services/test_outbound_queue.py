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


@pytest.mark.asyncio
async def test_legacy_connector_adoption_uses_explicit_owner_in_multi_company_db(monkeypatch):
    from celerp.connectors.outbound_queue import adopt_legacy_connector_configs
    from celerp.db import get_session_ctx

    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    legacy_id = f"inst-{uuid.uuid4().hex[:10]}"
    connector = f"legacy-owner-{uuid.uuid4().hex[:10]}"
    async with get_session_ctx() as seed:
        seed.add_all([
            Company(id=owner_id, name="Owner", slug=f"owner-{owner_id.hex[:8]}", settings={}),
            Company(id=other_id, name="Other", slug=f"other-{other_id.hex[:8]}", settings={}),
            ConnectorConfig(
                company_id=str(owner_id), connector=connector,
                webhook_ids_json='["1"]', webhook_secret=None,
            ),
            ConnectorConfig(
                company_id=legacy_id, connector=connector,
                webhook_ids_json='["2"]', webhook_secret="legacy-secret",
            ),
        ])
        await seed.commit()

    monkeypatch.setattr("celerp.config.ensure_instance_id", lambda: legacy_id)
    monkeypatch.setattr("celerp.connectors.ownership.ensure_instance_id", lambda: legacy_id)
    await adopt_legacy_connector_configs()

    async with get_session_ctx() as check:
        rows = (await check.execute(sa.select(ConnectorConfig).where(
            ConnectorConfig.connector == connector
        ))).scalars().all()
        assert len(rows) == 1
        assert rows[0].company_id == str(owner_id)
        assert set(rows[0].webhook_ids) == {"1", "2"}
        assert rows[0].webhook_secret == "legacy-secret"

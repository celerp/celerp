# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from celerp.connectors.outbound_queue import enqueue_item_change, process_outbound_queue_once
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


@pytest.mark.asyncio
async def test_connector_ui_enable_enqueues_current_woocommerce_identity(session):
    company_id = uuid.uuid4()
    session.add(ConnectorConfig(
        company_id=str(company_id), connector="woocommerce", direction="both"
    ))
    now = datetime.now(timezone.utc)
    session.add(Projection(
        company_id=company_id,
        entity_id="item:ui-link",
        entity_type="item",
        version=1,
        created_at=now,
        updated_at=now,
        state={
            "sku": "UI-1",
            "quantity": 2,
            "status": "available",
            "external_links": {
                "woocommerce": {
                    "product_id": "88",
                    "sync_enabled": True,
                    "manage_stock": True,
                }
            },
        },
    ))
    await session.flush()
    entry = LedgerEntry(
        company_id=company_id,
        entity_id="item:ui-link",
        entity_type="item",
        event_type="item.updated",
        data={"fields_changed": {}},
        source="connector_ui",
        idempotency_key=f"t-{uuid.uuid4()}",
    )
    await enqueue_item_change(
        session,
        entry,
        previous_state={
            "sku": "UI-1",
            "external_links": {
                "woocommerce": {
                    "product_id": "88",
                    "sync_enabled": False,
                }
            },
        },
    )
    await session.flush()
    queued = (await session.execute(
        sa.select(OutboundQueue).where(
            OutboundQueue.company_id == str(company_id),
            OutboundQueue.entity_id == "88",
        )
    )).scalars().all()
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_sku_change_invalidates_old_and_new_woocommerce_families(session):
    company_id = uuid.uuid4()
    session.add(ConnectorConfig(
        company_id=str(company_id), connector="woocommerce", direction="both"
    ))
    now = datetime.now(timezone.utc)
    session.add_all([
        Projection(
            company_id=company_id,
            entity_id="item:a",
            entity_type="item",
            version=1,
            created_at=now,
            updated_at=now,
            state={
                "sku": "NEW",
                "quantity": 1,
                "status": "available",
                "external_links": {
                    "woocommerce": {"product_id": "10", "sync_enabled": True}
                },
            },
        ),
        Projection(
            company_id=company_id,
            entity_id="item:b",
            entity_type="item",
            version=1,
            created_at=now,
            updated_at=now,
            state={
                "sku": "NEW",
                "quantity": 1,
                "status": "available",
                "external_links": {
                    "woocommerce": {"product_id": "20", "sync_enabled": True}
                },
            },
        ),
    ])
    await session.flush()
    entry = LedgerEntry(
        company_id=company_id,
        entity_id="item:a",
        entity_type="item",
        event_type="item.updated",
        data={"fields_changed": {"sku": {"old": "OLD", "new": "NEW"}}},
        source="api",
        idempotency_key=f"t-{uuid.uuid4()}",
    )
    await enqueue_item_change(
        session,
        entry,
        previous_state={
            "sku": "OLD",
            "external_links": {
                "woocommerce": {"product_id": "10", "sync_enabled": True}
            },
        },
    )
    await session.flush()
    identities = set((await session.execute(
        sa.select(OutboundQueue.entity_id).where(
            OutboundQueue.company_id == str(company_id)
        )
    )).scalars().all())
    assert {"10", "20"} <= identities


@pytest.mark.asyncio
async def test_identity_backoff_applies_to_newer_rows():
    from celerp.db import get_session_ctx

    company_id = uuid.uuid4()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    async with get_session_ctx() as seed:
        seed.add(Company(
            id=company_id,
            name="Backoff Co",
            slug=f"backoff-{company_id.hex[:8]}",
            settings={},
        ))
        seed.add(ConnectorConfig(
            company_id=str(company_id),
            connector="woocommerce",
            direction="both",
        ))
        seed.add_all([
            OutboundQueue(
                company_id=str(company_id),
                connector="woocommerce",
                entity_type="inventory",
                entity_id="77",
                status="pending",
                retry_count=3,
                next_retry_at=deadline,
            ),
            OutboundQueue(
                company_id=str(company_id),
                connector="woocommerce",
                entity_type="inventory",
                entity_id="77",
                status="pending",
                retry_count=0,
                next_retry_at=None,
            ),
        ])
        await seed.commit()

    await process_outbound_queue_once()

    async with get_session_ctx() as check:
        rows = (await check.execute(
            sa.select(OutboundQueue).where(
                OutboundQueue.company_id == str(company_id),
                OutboundQueue.entity_id == "77",
            )
        )).scalars().all()
        assert len(rows) == 2
        assert all(row.next_retry_at is not None for row in rows)
        assert min(row.next_retry_at for row in rows) >= deadline


@pytest.mark.asyncio
async def test_ambiguous_connector_ownership_fails_closed(session, monkeypatch):
    from celerp.connectors.ownership import (
        ConnectorOwnershipAmbiguousError,
        lock_connector_operation,
    )

    legacy_id = f"inst-{uuid.uuid4().hex}"
    monkeypatch.setattr(
        "celerp.connectors.ownership.ensure_instance_id",
        lambda: legacy_id,
    )
    company_a = str(uuid.uuid4())
    company_b = str(uuid.uuid4())
    connector = f"ambiguous-{uuid.uuid4().hex[:8]}"
    session.add_all([
        ConnectorConfig(company_id=company_a, connector=connector),
        ConnectorConfig(company_id=company_b, connector=connector),
    ])
    await session.flush()
    with pytest.raises(ConnectorOwnershipAmbiguousError):
        await lock_connector_operation(
            session, company_a, connector, require_owner=True
        )

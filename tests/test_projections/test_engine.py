# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid

import pytest

from celerp.events.engine import emit_event
from celerp.models.projections import Projection
from celerp.projections.engine import ProjectionEngine


@pytest.mark.asyncio
async def test_projection_apply_and_rebuild(session):
    company_id = uuid.uuid4()

    e1 = await emit_event(
        session,
        company_id=company_id,
        entity_id="item:1",
        entity_type="item",
        event_type="item.created",
        data={"sku": "S", "name": "A", "quantity": 1},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()

    proj = await session.get(Projection, {"company_id": company_id, "entity_id": "item:1"})
    assert proj is not None
    assert proj.state["name"] == "A"
    assert proj.version == e1.id

    await emit_event(
        session,
        company_id=company_id,
        entity_id="item:1",
        entity_type="item",
        event_type="item.updated",
        data={"fields_changed": {"name": {"old": "A", "new": "B"}}},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )
    await session.commit()

    proj2 = await session.get(Projection, {"company_id": company_id, "entity_id": "item:1"})
    assert proj2.state["name"] == "B"

    # rebuild is deterministic
    await ProjectionEngine.rebuild(session, company_id=company_id)
    await session.commit()
    proj3 = await session.get(Projection, {"company_id": company_id, "entity_id": "item:1"})
    assert proj3.state["name"] == "B"

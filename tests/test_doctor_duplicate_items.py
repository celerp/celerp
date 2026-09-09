# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Read-only doctor check for historical duplicate non-splittable document items.

The check scans doc projections for a non-splittable item appearing more than
once in the same doc's line_items and reports the affected documents. It never
mutates: auto_fixable is False, fixed is 0.
"""
from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import select

from celerp.events.engine import emit_event
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.routers.doctor import _CHECK_FNS, ALL_CHECKS


async def _register(client) -> str:
    addr = f"admin-{_uuid.uuid4().hex[:8]}@doctor.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Doctor Co", "email": addr, "name": "A", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _item(client, t, sku, *, allow_splitting: bool) -> str:
    r = await client.post(
        "/items",
        headers=_h(t),
        json={
            "status": "available", "sku": sku, "name": sku, "quantity": 5,
            "sell_by": "piece", "allow_splitting": allow_splitting,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_doctor_check_is_registered():
    """The new check is wired into both ALL_CHECKS and the dispatch map."""
    assert "duplicate_non_splittable_document_items" in ALL_CHECKS
    assert "duplicate_non_splittable_document_items" in _CHECK_FNS


@pytest.mark.asyncio
async def test_doctor_reports_duplicate_non_splittable_document_items(client, session):
    """Seed a doc projection holding a duplicate non-splittable item directly (the
    historical corrupt shape the guard now prevents on new writes), run the check,
    and assert it is reported read-only with nothing mutated."""
    t = await _register(client)
    cid = (await session.execute(select(Company))).scalars().first().id
    user_id = None  # the read-only check never reads user_id

    item = await _item(client, t, "DOC-NS", allow_splitting=False)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"

    # Write the corrupt projection directly (bypass the guard, as a legacy row).
    proj = Projection(
        company_id=cid,
        entity_id=doc_id,
        entity_type="doc",
        state={
            "entity_type": "doc",
            "doc_type": "invoice",
            "line_items": [
                {"item_id": item, "sku": "DOC-NS", "quantity": 1},
                {"item_id": item, "sku": "DOC-NS", "quantity": 1},
            ],
        },
        version=1,
    )
    from datetime import datetime, timezone
    proj.updated_at = datetime.now(timezone.utc)
    session.add(proj)
    await session.flush()

    fn = _CHECK_FNS["duplicate_non_splittable_document_items"]
    result = await fn(session, cid, user_id, fix=False)

    assert result["check"] == "duplicate_non_splittable_document_items"
    assert result["auto_fixable"] is False
    assert result["fixed"] == 0
    details = result["details"]
    assert any(d.get("entity_id") == doc_id for d in details)

    # Nothing was mutated: the projection still holds both duplicate lines.
    reloaded = (await session.execute(
        select(Projection).where(
            Projection.company_id == cid, Projection.entity_id == doc_id
        )
    )).scalars().first()
    assert len(reloaded.state["line_items"]) == 2


@pytest.mark.asyncio
async def test_doctor_ignores_splittable_repeats(client, session):
    """A splittable item repeated in a doc is NOT reported (splittable may repeat)."""
    t = await _register(client)
    cid = (await session.execute(select(Company))).scalars().first().id
    user_id = None  # the read-only check never reads user_id

    item = await _item(client, t, "DOC-SP", allow_splitting=True)
    doc_id = f"doc:{_uuid.uuid4().hex[:12]}"
    from datetime import datetime, timezone
    proj = Projection(
        company_id=cid, entity_id=doc_id, entity_type="doc",
        state={
            "entity_type": "doc", "doc_type": "invoice",
            "line_items": [
                {"item_id": item, "sku": "DOC-SP", "quantity": 1},
                {"item_id": item, "sku": "DOC-SP", "quantity": 1},
            ],
        },
        version=1,
    )
    proj.updated_at = datetime.now(timezone.utc)
    session.add(proj)
    await session.flush()

    fn = _CHECK_FNS["duplicate_non_splittable_document_items"]
    result = await fn(session, cid, user_id, fix=False)
    assert all(d.get("entity_id") != doc_id for d in result["details"])

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Doctor's missing-JE repair posts the full finalize JE, COGS legs included."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from celerp.models.projections import Projection


async def _register(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "Doctor Co", "email": f"doc-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "pw",
    })
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_doctor_finalize_repair_includes_cogs(client, session):
    """An imported old-style invoice repaired by the doctor gets one finalize JE
    carrying revenue AND the COGS pair, identical to a live finalize."""
    token = await _register(client)
    entity_id = f"doc:doctor-cogs-{uuid.uuid4().hex[:8]}"

    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.created",
        "data": {"doc_type": "invoice", "total": 110, "subtotal": 100, "tax": 10,
                 "status": "draft",
                 "line_items": [{"description": "Parcel", "quantity": 1,
                                 "item_id": "item:doctor-parcel",
                                 "unit_price": 100, "line_total": 100}]},
        "source": "import:test", "idempotency_key": f"idem-dc-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    r = await client.post("/docs/import", headers=_h(token), json={
        "entity_id": entity_id, "event_type": "doc.finalized", "data": {},
        "source": "import:test", "idempotency_key": f"idem-df-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200

    doc_proj = (await session.execute(select(Projection).where(
        Projection.entity_id == entity_id,
        Projection.entity_type == "doc"))).scalar_one()
    company_id = doc_proj.company_id

    # The stock parcel behind the line item: unit cost 20 (40 total over qty 2).
    session.add(Projection(
        company_id=company_id, entity_id="item:doctor-parcel", entity_type="item",
        state={"cost_total": 40.0, "quantity": 2.0}, version=1,
        updated_at=datetime.now(timezone.utc)))
    await session.flush()

    r = await client.post("/admin/doctor?checks=missing_jes&fix=true", headers=_h(token))
    data = r.json()
    missing = next(c for c in data["results"] if c["check"] == "missing_jes")
    assert missing["found"] == 1
    assert missing["fixed"] == 1

    jes = (await session.execute(select(Projection).where(
        Projection.company_id == company_id,
        Projection.entity_type == "journal_entry"))).scalars().all()
    prefix = f"je:auto:{entity_id}:"
    posted = {p.entity_id: p.state for p in jes
              if p.entity_id.startswith(prefix) and p.state.get("status") == "posted"}
    assert posted, "doctor fix posted no JE"

    entries = [e for st in posted.values() for e in st.get("entries", [])]
    cogs_debits = [float(e.get("debit") or 0) for e in entries
                   if e.get("account") == "5100" and float(e.get("debit") or 0) > 0]
    inv_credits = [float(e.get("credit") or 0) for e in entries
                   if e.get("account") == "1130-P" and float(e.get("credit") or 0) > 0]
    assert cogs_debits == [20.0], (
        f"repaired JE is missing the 5100 COGS debit: {entries}")
    assert inv_credits == [20.0], (
        f"repaired JE is missing the 1130-P credit: {entries}")

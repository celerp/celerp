# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.asyncio


def _u() -> str:
    return str(uuid.uuid4())


async def _create_contact(journey_api) -> str:
    r = await journey_api.post(
        "/crm/contacts",
        json={
            "name": f"ITest Contact {_u()[:8]}",
            "email": f"itest-{_u()[:8]}@example.com",
            "phone": "000",
            "idempotency_key": _u(),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_deal_pipeline_stage_and_close_won(journey_api):
    contact_id = await _create_contact(journey_api)

    deal = await journey_api.post(
        "/crm/deals",
        json={
            "name": f"ITest Deal {_u()[:8]}",
            "contact_id": contact_id,
            "value": 25000.0,
            "stage": "new",
            "idempotency_key": _u(),
        },
    )
    assert deal.status_code == 200, deal.text
    deal_id = deal.json()["id"]

    stage = await journey_api.patch(
        f"/crm/deals/{deal_id}/stage",
        json={"new_stage": "qualified", "idempotency_key": _u()},
    )
    assert stage.status_code == 200, stage.text

    won = await journey_api.post(
        f"/crm/deals/{deal_id}/won",
        json={"notes": "Signed", "idempotency_key": _u()},
    )
    assert won.status_code == 200, won.text

    deals = await journey_api.get("/crm/deals")
    assert deals.status_code == 200, deals.text
    found = [d for d in (deals.json().get("items") or []) if d.get("id") == deal_id]
    assert found
    assert found[0].get("status") == "won"


async def test_contact_tags_add_and_remove(journey_api):
    contact_id = await _create_contact(journey_api)

    add = await journey_api.post(
        f"/crm/contacts/{contact_id}/tags",
        json={"tags": ["vip", "wholesale"], "idempotency_key": _u()},
    )
    assert add.status_code == 200, add.text

    got = await journey_api.get(f"/crm/contacts/{contact_id}")
    assert got.status_code == 200, got.text
    assert set(got.json().get("tags") or []) >= {"vip", "wholesale"}

    # No untag endpoint yet; ensure tagging is persisted.

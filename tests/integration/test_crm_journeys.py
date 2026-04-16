# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

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


async def test_memo_lifecycle_items_approve_convert_return_cancel(journey_api):
    contact_id = await _create_contact(journey_api)

    memo = await journey_api.post(
        "/crm/memos",
        json={
            "contact_id": contact_id,
            "notes": "Consignment",
            "idempotency_key": _u(),
        },
    )
    assert memo.status_code == 200, memo.text
    memo_id = memo.json()["id"]

    items = await journey_api.get("/items", params={"limit": 1})
    assert items.status_code == 200, items.text
    first = (items.json().get("items") or [None])[0]
    assert first and first.get("id"), "Expected at least one item in dev dataset"
    item_id = first["id"]

    add_item = await journey_api.post(
        f"/crm/memos/{memo_id}/items",
        json={"item_id": item_id, "quantity": 2, "idempotency_key": _u()},
    )
    assert add_item.status_code == 200, add_item.text

    approve = await journey_api.post(f"/crm/memos/{memo_id}/approve", json={"idempotency_key": _u()})
    assert approve.status_code == 200, approve.text

    returned = await journey_api.post(
        f"/crm/memos/{memo_id}/return",
        json={"items": [{"item_id": item_id, "quantity": 1, "condition": "good"}], "idempotency_key": _u()},
    )
    assert returned.status_code == 200, returned.text

    convert = await journey_api.post(f"/crm/memos/{memo_id}/convert-to-invoice", json={"idempotency_key": _u()})
    assert convert.status_code == 200, convert.text
    invoice_id = convert.json()["doc_id"]

    inv = await journey_api.get(f"/docs/{invoice_id}")
    assert inv.status_code == 200, inv.text
    assert inv.json()["doc_type"] == "invoice"

    cancel = await journey_api.post(f"/crm/memos/{memo_id}/cancel", json={"reason": "Close memo", "idempotency_key": _u()})
    assert cancel.status_code in {200, 409}

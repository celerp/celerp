# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.asyncio


def _u() -> str:
    return str(uuid.uuid4())


async def test_list_duplicate_then_convert_to_doc(journey_api):
    created = await journey_api.post(
        "/lists",
        json={
            "name": f"ITest List {_u()[:8]}",
            "items": [
                {"sku": "L-SKU", "name": "List Item", "quantity": 1, "unit_price": 123.0},
            ],
            "idempotency_key": _u(),
        },
    )
    assert created.status_code == 200, created.text
    list_id = created.json()["id"]

    dup = await journey_api.post(f"/lists/{list_id}/duplicate", json={"idempotency_key": _u()})
    assert dup.status_code == 200, dup.text
    dup_id = dup.json()["id"]

    conv = await journey_api.post(
        f"/lists/{dup_id}/convert",
        json={"target_type": "invoice", "idempotency_key": _u()},
    )
    assert conv.status_code == 200, conv.text
    doc_id = conv.json()["target_doc_id"]

    doc = await journey_api.get(f"/docs/{doc_id}")
    assert doc.status_code == 200, doc.text
    assert doc.json()["doc_type"] == "invoice"

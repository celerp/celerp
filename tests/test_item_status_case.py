# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An item status is stored in lower case however it was typed, by every status writer."""
from __future__ import annotations

import pytest

from test_cost_restatement import _item, auth, ids  # noqa: F401  (auth and ids are fixtures)


def _single(client, auth, lot, status):
    return client.post(f"/items/{lot}/status", headers=auth["headers"], json={"new_status": status})


def _bulk(client, auth, lot, status):
    return client.post("/items/bulk/status", headers=auth["headers"], json={"entity_ids": [lot], "status": status})


def _patch(client, auth, lot, status):
    return client.patch(f"/items/{lot}", headers=auth["headers"],
                        json={"fields_changed": {"status": {"old": None, "new": status}}})


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [_single, _bulk, _patch], ids=["single", "bulk", "patch"])
async def test_a_status_typed_in_any_case_is_stored_in_lower_case(client, auth, write):
    lot = await _item(client, auth, 10.0)
    for typed, stored in (("Archived", "archived"), ("AVAILABLE", "available")):
        r = await write(client, auth, lot, typed)
        assert r.status_code == 200, r.text
        r = await client.get(f"/items/{lot}", headers=auth["headers"])
        assert r.json()["status"] == stored, r.json()["status"]

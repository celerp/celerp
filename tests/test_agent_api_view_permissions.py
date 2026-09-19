# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression coverage for independently revocable view permissions on agent-candidate APIs."""
from __future__ import annotations

import pytest

from test_helpers import grant_permission, perm_setup


@pytest.mark.asyncio
async def test_agent_candidate_reads_enforce_current_view_permissions(client, session):
    s = await perm_setup(client, session)
    owner = s["admin_h"]
    operator = s["operator_h"]
    item_id = s["item_id"]

    for permission in ("view_inventory", "view_contacts", "view_documents"):
        await grant_permission(client, owner, permission, "manager")

    denied = [
        ("/items", 403),
        (f"/items/{item_id}", 403),
        (f"/items/{item_id}/reorder-suggestion", 403),
        ("/contacts", 403),
        ("/contacts/contact:not-real", 403),
        ("/docs", 403),
        ("/docs/doc:not-real", 403),
    ]
    for path, status in denied:
        response = await client.get(path, headers=operator)
        assert response.status_code == status, (path, response.status_code, response.text)

    for permission in ("view_inventory", "view_contacts", "view_documents"):
        await grant_permission(client, owner, permission, "operator")

    allowed = [
        ("/items", 200),
        (f"/items/{item_id}", 200),
        (f"/items/{item_id}/reorder-suggestion", 200),
        ("/contacts", 200),
        ("/contacts/contact:not-real", 404),
        ("/docs", 200),
        ("/docs/doc:not-real", 404),
    ]
    for path, status in allowed:
        response = await client.get(path, headers=operator)
        assert response.status_code == status, (path, response.status_code, response.text)

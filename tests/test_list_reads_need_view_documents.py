# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every List read requires view_documents, as a document read does. The walk covers every GET
route on the Lists router, so a new List read cannot skip the permission."""
from __future__ import annotations

import pytest

from celerp.ai.tools import _route_permissions
from celerp_docs.routes import lists_router
from test_helpers import grant_permission, perm_setup


def test_every_list_read_route_requires_view_documents():
    reads = [r for r in lists_router.routes if "GET" in getattr(r, "methods", ())]
    assert reads
    unguarded = sorted(r.path for r in reads if "view_documents" not in _route_permissions(r))
    assert unguarded == []


@pytest.mark.asyncio
async def test_a_role_without_view_documents_cannot_read_a_list(client, session):
    s = await perm_setup(client, session)
    owner, operator = s["admin_h"], s["operator_h"]
    r = await client.post("/lists", headers=owner, json={"list_type": "quotation"})
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    paths = [f"/lists/{lid}", f"/lists/{lid}/page", f"/lists/{lid}/old-rule-total", f"/lists/{lid}/notes"]

    await grant_permission(client, owner, "view_documents", "manager")
    for path in paths:
        response = await client.get(path, headers=operator)
        assert response.status_code == 403, (path, response.status_code, response.text)

    await grant_permission(client, owner, "view_documents", "operator")
    for path in paths:
        response = await client.get(path, headers=operator)
        assert response.status_code == 200, (path, response.status_code, response.text)

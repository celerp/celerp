# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A write tool whose way back is ``reason:no_record_change`` really changes no business record.

Every route so classified runs against a demo company; each business table must hold the same
rows with the same content afterwards. The tables a check may still write are listed once in
``NOT_BUSINESS`` with the reason each is left out."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import inspect, text

from celerp.ai import reverse

# Tables a route classified no_record_change may write, each with why that is not a business record
# (a log, audit or cache table). None does today.
NOT_BUSINESS: dict[str, str] = {}

_SAMPLE = "00000000-0000-4000-8000-000000000001"


def _requests(item_id: str) -> dict[tuple[str, str], tuple[str, dict | None]]:
    """The request each no_record_change route gets: its real path and a body of the shape it reads."""
    key = uuid.uuid4().hex
    return {
        ("POST", "/api/labels/bulk-print"): ("/api/labels/bulk-print", {"entity_ids": [item_id]}),
        ("POST", "/api/labels/print-source"): ("/api/labels/print-source", {"source": "items", "ids": [item_id]}),
        ("POST", "/api/labels/print/{entity_id}"): (f"/api/labels/print/{item_id}", None),
        ("POST", "/backup/trigger"): ("/backup/trigger", None),
        ("POST", "/companies/me/modules/marketplace-download"): (
            "/companies/me/modules/marketplace-download", {"slug": "sample-module"}),
        ("POST", "/migrations/export-requests/{request_id}/add"): (
            f"/migrations/export-requests/{_SAMPLE}/add", {}),
        ("POST", "/migrations/scan"): ("/migrations/scan", None),
        ("POST", "/migrations/scan/decisions"): ("/migrations/scan/decisions", {"scan_token": key}),
        ("POST", "/migrations/scan/read"): ("/migrations/scan/read", {"scan_token": key}),
        ("POST", "/migrations/several/{file_id}/open"): (f"/migrations/several/{_SAMPLE}/open", None),
        ("POST", "/payments/connect"): ("/payments/connect", None),
        ("POST", "/system/update/check"): ("/system/update/check", None),
    }


async def _business_state(session) -> dict[str, tuple[int, str]]:
    """Row count and content checksum of every table not in NOT_BUSINESS."""
    names = await session.run_sync(lambda s: inspect(s.connection()).get_table_names())
    state = {}
    for name in sorted(set(names) - set(NOT_BUSINESS)):
        rows = sorted(repr(tuple(r)) for r in (await session.execute(text(f'SELECT * FROM "{name}"'))).all())
        state[name] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest())
    return state


@pytest.mark.asyncio
async def test_every_no_record_change_route_leaves_business_tables_as_they_were(client, session):
    r = await client.post("/auth/register", json={
        "company_name": "DemoCo", "email": f"nrc-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.post("/companies/me/business-type", json={"vertical": "gemstones"}, headers=h)).status_code == 200
    items = (await client.get("/items", headers=h, params={"limit": 1})).json()["items"]
    requests = _requests(items[0]["id"])

    classified = {route for route, back in reverse.WAY_BACK.items() if back == "reason:no_record_change"}
    assert set(requests) == classified, "every no_record_change route needs a request here"

    changed = {}
    for (method, _), (path, body) in sorted(requests.items()):
        before = await _business_state(session)
        resp = await client.request(method, path, json=body, headers=h)
        # The handler ran: the route exists and the body passed validation (a handler's own
        # refusal is a dict or text, FastAPI's validation error a list).
        unvalidated = resp.status_code == 422 and isinstance(resp.json().get("detail"), list)
        assert resp.status_code != 405 and not unvalidated, (path, resp.text)
        after = await _business_state(session)
        tables = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        if tables:
            changed[path] = (resp.status_code, tables)
    assert changed == {}

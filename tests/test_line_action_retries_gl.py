# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Sending a line action twice books it once.

A taxed invoice is finalized, shipped, taken back, shipped again and set as available,
and every request is sent twice with the same key (finalize, which takes no key, is
sent twice as is). After each pair the books hold exactly one posting of cost of goods
sold, revenue and tax. These guard behavior that already holds: they pass before and
after the line action changes they sit beside.
"""
from __future__ import annotations

import uuid

import pytest

from gl_support import gl_totals
from line_actions_support import line, line_ids, lot
from test_helpers import company_auth

pytestmark = pytest.mark.asyncio

COGS, REVENUE, TAX, RECEIVABLE = "5100", "4100", "2120", "1120"


@pytest.fixture
async def auth(session):
    return await company_auth(session, uuid.uuid4(), uuid.uuid4())


async def _twice(client, path: str, hd, body: dict | None = None) -> None:
    for _ in range(2):
        r = await client.post(path, headers=hd, json=body or {})
        assert r.status_code == 200, r.text


async def _books(session, auth) -> tuple:
    gl = await gl_totals(session, auth["company_id"])
    return gl.get(COGS), gl.get(REVENUE), gl.get(TAX), gl.get(RECEIVABLE)


async def test_repeated_requests_book_cost_revenue_and_tax_once(client, session, auth):
    hd = auth["headers"]
    sku = f"RT-{uuid.uuid4().hex[:6]}"
    lot_id = await lot(client, hd, sku, 1, cost=100)
    r = await client.post("/docs", headers=hd, json={"doc_type": "invoice", "subtotal": 300.0, "tax": 30.0, "total": 330.0, "line_items": [
        line(lot_id, 1, price=300.0, sku=sku, line_total=300.0, taxes=[{"code": "VAT", "rate": 10}])]})
    assert r.status_code == 200, r.text
    inv = r.json()["id"]
    sold = (100.0, -300.0, -30.0, 330.0)
    taken_back = (None, -300.0, -30.0, 330.0)

    assert (await client.post(f"/docs/{inv}/finalize", headers=hd)).status_code == 200
    again = await client.post(f"/docs/{inv}/finalize", headers=hd)
    assert again.status_code in (200, 409), again.text
    assert await _books(session, auth) == sold
    lines = {"line_ids": await line_ids(client, hd, inv)}

    await _twice(client, f"/docs/{inv}/fulfill-lines", hd, {**lines, "idempotency_key": "ship-1"})
    assert await _books(session, auth) == sold

    await _twice(client, f"/docs/{inv}/revert-lines", hd, {**lines, "idempotency_key": "back-1"})
    assert await _books(session, auth) == taken_back

    await _twice(client, f"/docs/{inv}/fulfill-lines", hd, {**lines, "idempotency_key": "ship-2"})
    assert await _books(session, auth) == sold

    await _twice(client, f"/docs/{inv}/set-available", hd, {**lines, "idempotency_key": "free-1"})
    assert await _books(session, auth) == taken_back

    await _twice(client, f"/docs/{inv}/fulfill-lines", hd, {**lines, "idempotency_key": "ship-3"})
    assert await _books(session, auth) == sold

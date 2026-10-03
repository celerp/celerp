# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Two cost corrections of one sold lot at the same moment, on real PostgreSQL.

The second waits for the first and is judged against what the first committed, so the
invoice's cost of goods sold ends equal to the lot's final cost, booked once. The same
correction sent twice at once posts one adjustment.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from test_cost_restatement import _doc_cogs, _fulfil, _invoice, _item, _state, company_auth
from test_fresh_authority_races_pg import _app_client, _race

pytestmark = pytest.mark.asyncio


def _patch(client, auth, item_id: str, cost: float, key: str | None = None):
    body = {"fields_changed": {"cost_total": {"old": None, "new": cost}}}
    if key:
        body["idempotency_key"] = key
    return lambda held, pending: _send(pending, held, client.patch(f"/items/{item_id}", headers=auth["headers"], json=body))


async def _send(pending: list, held, request):
    if held is not None:
        pending.append(held)
    return await request


async def _sold_lot(client, factory) -> tuple[dict, str, str]:
    async with factory() as s:
        auth = await company_auth(s, uuid.uuid4(), uuid.uuid4())
    item = await _item(client, auth, None)
    async with factory() as s:
        doc = await _invoice(client, s, auth, item)
    await _fulfil(client, doc, auth, item)
    return auth, item, doc


@pytest.mark.parametrize("first_cost, second_cost", [(100.0, 130.0), (130.0, 100.0)])
async def test_two_corrections_at_once_book_the_last_cost_once(committed_engine, first_cost, second_cost):
    factory = async_sessionmaker(committed_engine, class_=AsyncSession, expire_on_commit=False)
    async with _app_client(factory) as (client, pending):
        auth, item, doc = await _sold_lot(client, factory)
        first, second = await _race(
            committed_engine,
            lambda held: _patch(client, auth, item, first_cost)(held, pending),
            lambda held: _patch(client, auth, item, second_cost)(held, pending),
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["cost_correction"]["cogs_adjusted"][0]["amount"] == round(second_cost - first_cost, 2)
        async with factory() as s:
            assert (await _state(s, auth, item))["cost_total"] == second_cost
            assert await _doc_cogs(s, auth, doc) == second_cost


async def test_the_same_correction_sent_twice_at_once_posts_once(committed_engine):
    factory = async_sessionmaker(committed_engine, class_=AsyncSession, expire_on_commit=False)
    async with _app_client(factory) as (client, pending):
        auth, item, doc = await _sold_lot(client, factory)
        key = f"restate-{uuid.uuid4().hex}"
        first, second = await _race(
            committed_engine,
            lambda held: _patch(client, auth, item, 100.0, key)(held, pending),
            lambda held: _patch(client, auth, item, 100.0, key)(held, pending),
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json() == first.json()
        async with factory() as s:
            assert await _doc_cogs(s, auth, doc) == 100.0

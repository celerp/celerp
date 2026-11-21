# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One AI service operation is one charge row of its company, attributed to the user who
sent it first, on real PostgreSQL connections against committed data.

Two users can share one service operation (a bank suggestion step is named by the
company, its request key and what the model sees, so two users retrying the same
request send the same operation). The service charged it once, so the usage record
holds it once: the second user's send and settle find the first user's row and add no
row of their own."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from celerp.ai import deadlines, usage

pytestmark = pytest.mark.asyncio


async def _seed(engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    company, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        for user in (first, second):
            await conn.execute(text(
                "INSERT INTO users (id, email, name, is_active, is_install_owner, created_at) "
                "VALUES (:id, :email, 'User', true, false, now())"), {"id": user, "email": f"{user.hex}@example.com"})
        await conn.execute(text(
            "INSERT INTO companies (id, name, slug, settings, is_active, created_at) "
            "VALUES (:id, 'Co', :slug, CAST('{}' AS json), true, now())"), {"id": company, "slug": f"co-{company.hex[:8]}"})
    return company, first, second


async def _rows(engine, company) -> list[tuple]:
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(
            "SELECT user_id, operation_id, credits, pending FROM ai_usage WHERE company_id = :c"),
            {"c": company})).all()]


async def test_two_users_sending_one_operation_record_one_charge_row_attributed_to_the_first(
        committed_engine, monkeypatch):
    async def _limits():
        return deadlines.CURRENT_RELAY_DEFAULTS
    monkeypatch.setattr(deadlines, "limits", _limits)
    company, first, second = await _seed(committed_engine)
    op = uuid.uuid4()
    maker = async_sessionmaker(bind=committed_engine, expire_on_commit=False)

    async with maker() as a, maker() as b:
        await usage.record_usage(a, company_id=company, user_id=first, operation_id=op, send_bytes=10)
        # The second user's send waits on the company lock the first send holds.
        later = asyncio.ensure_future(
            usage.record_usage(b, company_id=company, user_id=second, operation_id=op, send_bytes=10))
        await asyncio.sleep(0.2)
        assert not later.done()
        await a.commit()
        await later
        await b.commit()
        await usage.record_usage(a, company_id=company, user_id=first, operation_id=op, credits=3, known=True)
        await a.commit()
        # The second user writes last: the row still names the first sender.
        await usage.record_usage(b, company_id=company, user_id=second, operation_id=op, credits=3, known=True)
        await b.commit()

    assert await _rows(committed_engine, company) == [(first, op, 3, False)]

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The opening inventory entry follows every change of the gap it books.

Each restatement (void the live entry, post the new one) is keyed on the entry's
position in the ledger, so returning to an earlier amount is a new transition that
posts again, never a replay of the first one that silently does nothing.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from celerp.services import auto_je
from celerp.services.company_lock import locked_company
from test_helpers import company_auth

pytestmark = pytest.mark.asyncio


def _ob_id(cid) -> str:
    return f"je:auto:opening-inventory:{cid}"


async def _book(session, auth, amount: str) -> None:
    await locked_company(session, auth["company_id"])
    await auto_je.book_opening_inventory(session, company_id=auth["company_id"], user_id=auth["user_id"],
                                         in_production=Decimal(amount))
    await session.commit()


async def _live(session, cid) -> float:
    """The amount the live opening entry books, 0 when none is posted."""
    session.expire_all()
    row = await session.get(Projection, {"company_id": cid, "entity_id": _ob_id(cid)})
    if row is None or row.state.get("status") != "posted":
        return 0.0
    debits = sum(float(e.get("debit") or 0) for e in row.state["entries"])
    credits = sum(float(e.get("credit") or 0) for e in row.state["entries"])
    assert debits == pytest.approx(credits)
    return debits


async def _events(session, cid) -> int:
    return await session.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == cid, LedgerEntry.entity_id == _ob_id(cid)))


async def test_a_b_a_posts_the_first_amount_again(session, auth):
    await _book(session, auth, "10")
    assert await _live(session, auth["company_id"]) == pytest.approx(10)
    await _book(session, auth, "15")
    assert await _live(session, auth["company_id"]) == pytest.approx(15)
    await _book(session, auth, "10")
    assert await _live(session, auth["company_id"]) == pytest.approx(10)


async def test_a_zero_a_posts_again_after_the_gap_closed(session, auth):
    await _book(session, auth, "10")
    await _book(session, auth, "0")
    assert await _live(session, auth["company_id"]) == 0
    await _book(session, auth, "10")
    assert await _live(session, auth["company_id"]) == pytest.approx(10)


async def test_same_value_is_a_no_op(session, auth):
    await _book(session, auth, "10")
    before = await _events(session, auth["company_id"])
    await _book(session, auth, "10")
    await _book(session, auth, "10")
    assert await _events(session, auth["company_id"]) == before
    assert await _live(session, auth["company_id"]) == pytest.approx(10)


async def test_a_rolled_back_attempt_retries_to_one_live_entry(session, auth):
    """A response lost after the work rolled back: the retry books it exactly once."""
    await _book(session, auth, "10")
    await locked_company(session, auth["company_id"])
    await auto_je.book_opening_inventory(session, company_id=auth["company_id"], user_id=auth["user_id"],
                                         in_production=Decimal("15"))
    await session.rollback()
    await _book(session, auth, "15")
    await _book(session, auth, "15")
    assert await _live(session, auth["company_id"]) == pytest.approx(15)


async def test_period_lock_on_either_half_changes_nothing(session, auth):
    from fastapi import HTTPException

    await _book(session, auth, "10")
    before = await _events(session, auth["company_id"])
    company = await locked_company(session, auth["company_id"])
    company.settings = {**company.settings, "lock_date": "2999-12-31"}
    await session.commit()
    with pytest.raises(HTTPException):
        await _book(session, auth, "15")
    await session.rollback()
    assert await _events(session, auth["company_id"]) == before
    assert await _live(session, auth["company_id"]) == pytest.approx(10)


async def test_booking_requires_the_company_lock(session, auth):
    with pytest.raises(RuntimeError, match="company lock"):
        await auto_je.book_opening_inventory(session, company_id=auth["company_id"], user_id=auth["user_id"],
                                             in_production=Decimal("10"))


async def test_concurrent_bookings_leave_one_live_entry(committed_engine):
    factory = async_sessionmaker(committed_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        auth = await company_auth(s, uuid.uuid4(), uuid.uuid4())

    async def one(amount: str) -> None:
        async with factory() as s:
            await _book(s, auth, amount)

    await one("10")
    await asyncio.gather(one("15"), one("15"))
    async with factory() as s:
        assert await _live(s, auth["company_id"]) == pytest.approx(15)
    await asyncio.gather(one("10"), one("10"))
    async with factory() as s:
        assert await _live(s, auth["company_id"]) == pytest.approx(10)

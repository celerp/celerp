# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A posting and a chart change meet on real PostgreSQL connections.

A posting reads the accounts it posts to more than once in a transaction, in no fixed
order; a chart change locks the accounts it edits. The company lock orders the two: the
change waits for the posting (or the posting for the change), then both commit. Neither
is aborted as a deadlock."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from test_company_gate_concurrency_pg import _race
from test_import_preflight_invariants import _factory, _seed_race_company

pytestmark = pytest.mark.asyncio


async def _company_with_accounts(factory) -> uuid.UUID:
    """A committed company with two top-level asset accounts, 1100 and 1200."""
    from celerp_accounting.models import Account

    company_id, _ = await _seed_race_company(factory)
    async with factory() as s:
        for code in ("1100", "1200"):
            s.add(Account(id=uuid.uuid4(), company_id=company_id, code=code, name=f"Cash {code}",
                          account_type="asset", is_active=True))
        await s.commit()
    return company_id


async def _parent(factory, company_id, code: str) -> str | None:
    from sqlalchemy import select

    from celerp_accounting.models import Account

    async with factory() as s:
        return await s.scalar(select(Account.parent_code).where(
            Account.company_id == company_id, Account.code == code))


async def _move_1100_under_1200(factory, company_id) -> None:
    from celerp_accounting.chart_rules import change_account

    async with factory() as s:
        await change_account(s, company_id, "1100", parent_code="1200")
        await s.commit()


@pytest.mark.parametrize("second_read", ["lock_accounts", "require_money_account"])
async def test_a_posting_and_a_chart_move_never_deadlock(committed_engine, second_read):
    """The posting holds 1200, the move holds 1100 and needs 1200, the posting then needs 1100."""
    from celerp.services.journal_accounts import lock_accounts
    from celerp_accounting.ledger_accounts import require_money_account

    factory = _factory(committed_engine)
    company_id = await _company_with_accounts(factory)

    async def posting(entered: asyncio.Event, go: asyncio.Event) -> None:
        async with factory() as s:
            assert set(await lock_accounts(s, company_id, {"1200"})) == {"1200"}
            entered.set()
            await go.wait()
            if second_read == "lock_accounts":
                assert set(await lock_accounts(s, company_id, {"1100"})) == {"1100"}
            else:
                await require_money_account(s, company_id, "1100")
            await s.commit()

    results = await _race(committed_engine, posting, lambda: _move_1100_under_1200(factory, company_id))
    assert results == [None, None]
    assert await _parent(factory, company_id, "1100") == "1200"


@pytest.mark.parametrize("read", ["lock_accounts", "require_money_account"])
async def test_a_posting_and_a_role_change_never_deadlock(committed_engine, read):
    """A posting reads its accounts, then records its event under the company lock
    (events.engine.emit_event); a role change takes the company lock, then reads and
    changes the chart. The posting's read takes the company lock, so the change waits
    for it at the company and both commit."""
    from celerp.services.account_roles import reconcile_company
    from celerp.services.company_lock import lock_company
    from celerp.services.journal_accounts import lock_accounts
    from celerp_accounting.ledger_accounts import require_money_account

    factory = _factory(committed_engine)
    company_id = await _company_with_accounts(factory)

    async def posting(entered: asyncio.Event, go: asyncio.Event) -> None:
        async with factory() as s:
            if read == "lock_accounts":
                assert set(await lock_accounts(s, company_id, {"1100"})) == {"1100"}
            else:
                await require_money_account(s, company_id, "1100")
            entered.set()
            await go.wait()
            await lock_company(s, company_id)
            await s.commit()

    async def role_change() -> None:
        async with factory() as s:
            await reconcile_company(s, company_id)
            await s.commit()

    assert await _race(committed_engine, posting, role_change) == [None, None]

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An imported journal is a new posting: each line goes to an active account with nothing
under it, as a manual journal does. A header holds no balance of its own (it sums the
accounts below it) and an inactive account takes nothing new."""
from __future__ import annotations

import uuid

import pytest

from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio


async def _import(client, session, auth, debit_account: str):
    je = f"je:{uuid.uuid4()}"
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": je, "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex,
        "data": {"status": "posted", "ts": "2026-03-01", "memo": "import", "entries": [
            {"account": debit_account, "debit": 50.0, "credit": 0.0},
            {"account": "1111", "debit": 0.0, "credit": 50.0}]}}]})
    assert r.status_code == 200, r.text
    return r.json(), await session.get(Projection, (auth["company_id"], je)) is not None


async def test_an_imported_journal_posts_only_to_active_leaf_accounts(client, session, auth):
    h = auth["headers"]
    for body in ({"code": "6999", "name": "Child", "account_type": "expense", "parent_code": "6000"},
                 {"code": "6998", "name": "Retired", "account_type": "expense", "parent_code": None}):
        assert (await client.post("/accounting/accounts", headers=h, json=body)).status_code == 200
    assert (await client.patch("/accounting/accounts/6998", headers=h, json={"is_active": False})).status_code == 200

    for account, problem in (("6000", "header"), ("6998", "inactive")):
        result, stored = await _import(client, session, auth, account)
        assert result["created"] == 0 and len(result["errors"]) == 1, result
        assert f"Account {account} is" in str(result["errors"][0]), (problem, result)
        assert not stored, account

    result, stored = await _import(client, session, auth, "6999")
    assert result["created"] == 1 and not result["errors"], result
    assert stored


async def _import_lines(client, auth, lines):
    je = f"je:{uuid.uuid4()}"
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
        "entity_id": je, "event_type": "acc.journal_entry.created", "source": "csv",
        "idempotency_key": uuid.uuid4().hex,
        "data": {"status": "posted", "ts": "2026-03-01", "memo": "import", "entries": lines}}]})
    assert r.status_code == 200, r.text
    return r.json()


async def _trial_balance_balanced(client, auth) -> bool:
    r = await client.get("/accounting/trial-balance", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()["balanced"]


async def test_an_imported_journal_that_does_not_balance_is_refused(client, auth):
    result = await _import_lines(client, auth, [{"account": "6100", "debit": 5.0}, {"account": "1111", "credit": 10.0}])
    assert result["created"] == 0 and len(result["errors"]) == 1, result
    assert "doesn't balance" in str(result["errors"][0]) and "Add 5.00 to the debit side" in str(result["errors"][0]), result
    assert await _trial_balance_balanced(client, auth)


async def test_the_journal_boundary_refuses_a_line_with_no_account_or_an_unbalanced_entry(session, auth):
    """The rules live at the boundary every journal writer passes, not in a route."""
    from fastapi import HTTPException

    from celerp.events.engine import emit_event

    for lines, needle in (([{"debit": 10.0}, {"account": "1111", "credit": 10.0}], "is not in the chart"),
                          ([{"account": "6100", "debit": 10.0}, {"account": "1111", "credit": 9.0}], "doesn't balance")):
        with pytest.raises(HTTPException) as exc:
            await emit_event(
                session, company_id=uuid.UUID(str(auth["company_id"])), entity_id=f"je:{uuid.uuid4()}",
                entity_type="journal_entry", event_type="acc.journal_entry.created",
                data={"status": "posted", "ts": "2026-03-01", "entries": lines},
                actor_id=None, location_id=None, source="test", idempotency_key=uuid.uuid4().hex, metadata_={})
        await session.rollback()
        assert exc.value.status_code == 422 and needle in str(exc.value.detail), exc.value.detail

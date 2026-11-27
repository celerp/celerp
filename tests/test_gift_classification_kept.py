# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A journal line naming a donor is classified gift or not once, when it posts, and every copy of
the posted journal carries that answer: changing which accounts count as gift income afterwards
never moves it, whichever door writes the copy: Change fund on an imported journal through HTTP,
and every other copy door (an accountant's reversal, a module's relabel, an unvoid, a recognition
reversal) writes its lines through the same peer_lines.copy_posted_lines.

Initial conditions per test: a fresh company with the seeded chart, funds on (_setup), account
4260 (revenue) and a category credited to it, a gift category or not as ``direction`` starts."""
from __future__ import annotations

import uuid

import pytest

from income_books import PERIOD, _account, _category, _donor, _item, _set_category_account
from test_fund_movements import _setup

pytestmark = pytest.mark.asyncio

ACCOUNT = "4260"


async def _history_total(client, auth, donor: str) -> float:
    r = await client.get(f"/donors/{donor}/history", headers=auth["headers"], params=PERIOD)
    assert r.status_code == 200, r.text
    return float(r.json()["total"])


async def _gift_setting(client, auth, category: str, gift: bool) -> None:
    r = await client.put(f"/companies/me/categories/{category}/gift", headers=auth["headers"], json={"gift": gift})
    assert r.status_code == 200, r.text


def _lines(donor: str) -> list[dict]:
    return [{"account": "1111", "debit": 100.0, "credit": 0.0},
            {"account": ACCOUNT, "debit": 0.0, "credit": 100.0, "contact": donor}]


async def _imported(client, auth, donor: str, fund: str) -> str:
    je = f"je:{uuid.uuid4()}"
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={
        "records": [{"entity_id": je, "event_type": "acc.journal_entry.created", "source": "csv",
                     "idempotency_key": uuid.uuid4().hex,
                     "data": {"status": "posted", "ts": "2026-03-10", "memo": "Bank transfer", "entries": _lines(donor)}}],
        "fund_choices": {"all": fund}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    return je


async def _change_fund(client, auth, je: str, funds: dict) -> None:
    v = await client.get(f"/accounting/journal/{je}/funds", headers=auth["headers"])
    assert v.status_code == 200, v.text
    r = await client.post(f"/accounting/journal/{je}/change-fund", headers=auth["headers"], json={
        "expected_version": v.json()["version"], "request_key": uuid.uuid4().hex, "reason": "Wrong fund",
        "all": funds["U2"]})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("direction", ["became_gift", "stopped_gift"])
async def test_a_copy_of_a_posted_journal_keeps_its_gift_classification(client, auth, direction):
    funds = await _setup(client, auth)
    await _account(client, auth, ACCOUNT)
    category = await _category(client, auth, "Donations by transfer")
    assert (await _set_category_account(client, auth, category, ACCOUNT)).status_code == 200
    was_gift = direction == "stopped_gift"
    if was_gift:
        await _gift_setting(client, auth, category, True)
    donor = await _donor(client, auth, "Transfer Donor")
    je = await _imported(client, auth, donor, funds["U1"])
    posted = 100.0 if was_gift else 0.0
    assert await _history_total(client, auth, donor) == posted
    await _gift_setting(client, auth, category, not was_gift)
    assert await _history_total(client, auth, donor) == posted
    await _change_fund(client, auth, je, funds)
    assert await _history_total(client, auth, donor) == posted


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("posted", [True, False, None])
async def test_every_copy_of_a_named_line_carries_the_classification_it_posted_with(posted, reverse):
    """None: a line posted before lines carried an answer, which counted as no gift."""
    from celerp.services.peer_lines import copy_posted_lines

    line = {"account": ACCOUNT, "debit": 0.0, "credit": 100.0, "contact": "contact:x",
            **({} if posted is None else {"gift": posted})}
    bank = {"account": "1111", "debit": 100.0, "credit": 0.0}
    named, other = copy_posted_lines("je:1", [line, bank], reverse=reverse)
    assert named["gift"] is (posted is True)
    assert "gift" not in other


async def test_a_line_on_the_income_account_of_an_item_in_a_gift_category_posts_as_a_gift(client, auth):
    """The gift category has no account of its own: its item's account is the gift account."""
    funds = await _setup(client, auth)
    await _account(client, auth, ACCOUNT)
    category = await _category(client, auth, "Donations by transfer")
    await _gift_setting(client, auth, category, True)
    await _item(client, auth, "Transfer gift", category=category, income_account=ACCOUNT)
    donor = await _donor(client, auth, "Transfer Donor")
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-03-10", "memo": "Bank transfer", "idempotency_token": uuid.uuid4().hex,
        "entries": [{**line, "fund_id": funds["U1"]} for line in _lines(donor)]})
    assert r.status_code == 200, r.text
    assert await _history_total(client, auth, donor) == 100.0


async def test_an_imported_line_never_claims_to_copy_a_posted_entry(client, auth, session):
    """Only these books' own copiers name ``posted_from``; an import row naming it is a new posting:
    it stores no such claim, takes the fund the import chose and is classified gift by these books."""
    funds = await _setup(client, auth)
    await _account(client, auth, ACCOUNT)
    donor = await _donor(client, auth, "Transfer Donor")
    earlier = await _imported(client, auth, donor, funds["U1"])
    je = f"je:{uuid.uuid4()}"
    claimed = [{**line, "posted_from": earlier, "gift": True} for line in _lines(donor)]
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={
        "records": [{"entity_id": je, "event_type": "acc.journal_entry.created", "source": "csv",
                     "idempotency_key": uuid.uuid4().hex,
                     "data": {"status": "posted", "ts": "2026-03-10", "memo": "Bank transfer", "entries": claimed}}],
        "fund_choices": {"all": funds["U1"]}})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1, r.text
    from sqlalchemy import select

    from celerp.models.projections import Projection
    state = (await session.execute(select(Projection.state).where(Projection.entity_id == je))).scalar_one()
    assert [e.get("posted_from") for e in state["entries"]] == [None, None]
    assert {e["fund_id"] for e in state["entries"]} == {funds["U1"]}
    assert [e.get("gift") for e in state["entries"]] == [None, False]


async def test_an_imported_line_naming_a_posted_entry_still_has_its_fund_refused(client, auth):
    """A fund an import row gives itself is refused before the import's choice applies (rule V);
    naming a posted entry in ``posted_from`` never lets it through as a copy."""
    funds = await _setup(client, auth)
    await _account(client, auth, ACCOUNT)
    donor = await _donor(client, auth, "Transfer Donor")
    earlier = await _imported(client, auth, donor, funds["U1"])
    claimed = [{**line, "posted_from": earlier, "fund_id": funds["U2"]} for line in _lines(donor)]
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={
        "records": [{"entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
                     "idempotency_key": uuid.uuid4().hex,
                     "data": {"status": "posted", "ts": "2026-03-10", "memo": "Bank transfer", "entries": claimed}}],
        "fund_choices": {"all": funds["U1"]}})
    assert r.status_code == 200 and r.json()["created"] == 0 and r.json()["errors"], r.text

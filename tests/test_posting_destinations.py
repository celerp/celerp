# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An account a user picks for a new posting must be able to take it.

Wherever a user names the account a new journal line goes to (a stock write-off, a
bank reconciliation entry, split or tolerance write-off, a manual journal, a payment),
the account must be in the chart, active, and have nothing under it: a header only
sums the accounts below it. The check holds the account until the entry is posted, so
switching it off or putting an account under it meanwhile waits for the posting, and
a posting that waited sees the change and is refused.
"""
from __future__ import annotations

import pytest

from migration_support import auth as bearer
from test_helpers import in_language
from test_posting_roles_race_pg_draft import _company, _race, race  # noqa: F401  (race is a fixture)

HEADER = "6000"  # the seeded chart's expenses header
_REFUSALS = {
    "header": ("posting.destination.header", "Account {code} is a header account. Choose one of the accounts under it."),
    "inactive": ("posting.destination.inactive", "Account {code} is inactive. Choose an active account."),
}


def _refused(r, why: str, code: str) -> None:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    key, english = _REFUSALS[why]
    assert detail["message_key"] == key, detail
    assert detail["message"] == english.format(code=code), detail
    assert in_language("de", detail) != detail["message"]


async def _account(client, headers, code: str, parent: str = HEADER) -> None:
    r = await client.post("/accounting/accounts", headers=headers, json={
        "code": code, "name": f"Account {code}", "account_type": "expense", "parent_code": parent})
    assert r.status_code == 200, r.text


async def _switch_off(client, headers, code: str):
    return await client.patch(f"/accounting/accounts/{code}", headers=headers, json={"is_active": False})


async def _destination(client, headers, why: str) -> str:
    """A code the user picks that cannot take a posting, for the reason ``why``."""
    if why == "header":
        return HEADER
    await _account(client, headers, "6990")
    assert (await _switch_off(client, headers, "6990")).status_code == 200
    return "6990"


async def _statement_line(client, headers) -> tuple[str, str]:
    """An open reconciliation of the default bank with one statement line, a 50 fee; once
    the fee is booked, 0.5 is left to write off."""
    [bank] = (await client.get("/accounting/bank-accounts", headers=headers)).json()["items"]
    r = await client.post("/accounting/reconciliation/start", headers=headers, json={
        "bank_account_id": bank["id"], "statement_date": "2026-03-31", "statement_balance": -49.5})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await client.post(f"/accounting/reconciliation/{sid}/import-csv", headers=headers,
                          files={"file": ("s.csv", b"Date,Description,Amount\n2026-03-01,Fee,-50\n", "text/csv")})
    assert r.status_code == 200, r.text
    [line] = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=headers)).json()["items"]
    return sid, line["id"]


async def _recon_create(client, headers, code: str):
    sid, line = await _statement_line(client, headers)
    return await client.post(f"/accounting/reconciliation/{sid}/lines/{line}/create", headers=headers,
                             json={"account_code": code})


async def _recon_split(client, headers, code: str):
    sid, line = await _statement_line(client, headers)
    return await client.post(f"/accounting/reconciliation/{sid}/lines/{line}/split", headers=headers,
                             json={"splits": [{"account_code": "6100", "amount": 20},
                                              {"account_code": code, "amount": 30}]})


async def _recon_write_off(client, headers, code: str):
    sid, line = await _statement_line(client, headers)
    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{line}/create", headers=headers,
                          json={"account_code": "6100"})
    assert r.status_code == 200, r.text
    return await client.post(f"/accounting/reconciliation/{sid}/write-off", headers=headers,
                             json={"account_code": code})


async def _stock_write_off_line(client, headers, code: str):
    return (await _stock_write_off(client, headers, code))[1]


async def _stock_write_off(client, headers, code: str):
    """A stock write-off of one unit of a new 2-unit lot to ``code``: its id, and the
    response to picking the account."""
    r = await client.post("/items", headers=headers, json={
        "status": "available", "sku": "WO-1", "name": "WO-1", "quantity": 2, "sell_by": "piece", "cost_total": 20})
    assert r.status_code == 200, r.text
    r = await client.post("/lists/writeoff", headers=headers, json={"entity_ids": [r.json()["id"]]})
    assert r.status_code == 200, r.text
    wo = r.json()["id"]
    [line] = (await client.get(f"/lists/{wo}", headers=headers)).json()["line_items"]
    return wo, await client.post(f"/lists/{wo}/writeoff-line", headers=headers,
                                 json={"line_id": line["line_id"], "qty_out": 1, "account": code})


async def _manual_journal(client, headers, code: str):
    return await client.post("/accounting/journal-entries", headers=headers, json={
        "ts": "2026-03-01", "memo": "Fee", "idempotency_token": "dest-1",
        "entries": [{"account": code, "debit": 10, "credit": 0}, {"account": "1111", "debit": 0, "credit": 10}]})


_PATHS = {"stock write-off": _stock_write_off_line, "reconciliation entry": _recon_create,
          "reconciliation split": _recon_split, "reconciliation write-off": _recon_write_off,
          "manual journal": _manual_journal}


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["header", "inactive"])
@pytest.mark.parametrize("path", list(_PATHS))
async def test_a_picked_account_that_cannot_take_a_posting_is_refused(session, client, auth, path, why):
    headers = auth["headers"]
    code = await _destination(client, headers, why)
    before = (await client.get("/ledger?entity_type=journal_entry", headers=headers)).json()["items"]

    _refused(await _PATHS[path](client, headers, code), why, code)

    after = (await client.get("/ledger?entity_type=journal_entry", headers=headers)).json()["items"]
    assert [e for e in after if any(x.get("account") == code for x in (e["data"].get("entries") or []))] == []
    assert len(after) - len(before) <= 2  # at most the setup entry of the write-off case


@pytest.mark.asyncio
async def test_a_stock_write_off_whose_account_was_switched_off_since_it_was_picked_is_refused(client, auth):
    headers = auth["headers"]
    await _account(client, headers, "6990")
    wo, r = await _stock_write_off(client, headers, "6990")
    assert r.status_code == 200, r.text
    assert (await _switch_off(client, headers, "6990")).status_code == 200

    _refused(await client.post(f"/lists/{wo}/write-off", headers=headers), "inactive", "6990")


# --- On real Postgres: the account cannot change between the check and the posting -----


async def _race_recon(engine, client, hold, change, why: str) -> None:
    cid, tok = await _company(engine)
    headers = bearer(tok)
    await _account(client, headers, "6990")
    sid, line = await _statement_line(client, headers)

    changed, posted = await _race(
        engine, client, hold, lambda: change(client, headers),
        lambda: client.post(f"/accounting/reconciliation/{sid}/lines/{line}/create", headers=headers,
                            json={"account_code": "6990"}))

    assert changed.status_code == 200, changed.text
    _refused(posted, why, "6990")


async def test_a_posting_waiting_on_the_account_being_switched_off_is_refused(committed_engine, race):
    client, hold = race
    await _race_recon(committed_engine, client, hold, lambda c, h: _switch_off(c, h, "6990"), "inactive")


async def test_a_posting_waiting_on_an_account_being_put_under_it_is_refused(committed_engine, race):
    client, hold = race

    async def child(c, h):
        return await c.post("/accounting/accounts", headers=h, json={
            "code": "6991", "name": "Account 6991", "account_type": "expense", "parent_code": "6990"})

    await _race_recon(committed_engine, client, hold, child, "header")

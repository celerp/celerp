# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An account a user picks for a new posting must be able to take it.

Wherever a user names the account a new journal line goes to (a stock write-off, a
bank reconciliation entry, split or tolerance write-off, a manual journal, a payment,
the account on a bill line, a bank in a transfer),
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


async def _account(client, headers, code: str, parent: str = HEADER, account_type: str = "expense") -> None:
    r = await client.post("/accounting/accounts", headers=headers, json={
        "code": code, "name": f"Account {code}", "account_type": account_type, "parent_code": parent})
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


_LINE = {"name": "Cleaning service", "quantity": 1, "unit_price": 40.0}


async def _with_line_account(client, headers, doc_type: str, code: str) -> str:
    """A draft ``doc_type`` whose one line names ``code`` as its own account."""
    r = await client.post("/docs", headers=headers, json={
        "doc_type": doc_type, "contact_id": "supplier:1", "line_items": [_LINE]})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    r = await client.patch(f"/docs/{doc}", headers=headers, json={
        "fields_changed": {"line_items": {"new": [{**_LINE, "account_code": code}]}}})
    assert r.status_code == 200, r.text
    return doc


async def _bill_line(client, headers, code: str):
    return await client.post(f"/docs/{await _with_line_account(client, headers, 'bill', code)}/finalize",
                             headers=headers)


def _bill_snapshot(code: str) -> dict:
    return {"doc_type": "bill", "status": "awaiting_payment", "contact_id": "supplier:1", "currency": "USD",
            "line_items": [{**_LINE, "line_total": 40.0, "account_code": code}], "subtotal": 40.0, "total": 40.0}


async def _bill_import(client, headers, code: str):
    return await client.post("/docs/import", headers=headers, json={
        "entity_id": "doc:IMP-1", "event_type": "doc.created", "data": _bill_snapshot(code),
        "source": "import", "idempotency_key": "imp-1"})


_PATHS = {"stock write-off": _stock_write_off_line, "bill line": _bill_line, "bill import": _bill_import, "reconciliation entry": _recon_create,
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
@pytest.mark.parametrize("splits", [
    [{"amount": 50}],
    [{"account_code": "6100", "amount": 20}, {"account_code": "", "amount": 30}],
])
async def test_a_split_with_no_account_chosen_is_refused(client, auth, splits):
    headers = auth["headers"]
    sid, line = await _statement_line(client, headers)

    r = await client.post(f"/accounting/reconciliation/{sid}/lines/{line}/split", headers=headers,
                          json={"splits": splits})

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "reconciliation.split.account_required", detail
    assert detail["message"] == "Choose an account for every split."
    assert in_language("de", detail) != detail["message"]
    [stmt] = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=headers)).json()["items"]
    assert stmt["status"] != "matched"


@pytest.mark.asyncio
async def test_a_stock_write_off_whose_account_was_switched_off_since_it_was_picked_is_refused(client, auth):
    headers = auth["headers"]
    await _account(client, headers, "6990")
    wo, r = await _stock_write_off(client, headers, "6990")
    assert r.status_code == 200, r.text
    assert (await _switch_off(client, headers, "6990")).status_code == 200

    _refused(await client.post(f"/lists/{wo}/write-off", headers=headers), "inactive", "6990")


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["header", "inactive"])
async def test_an_account_picked_for_a_line_of_a_finalized_bill_is_refused(client, auth, why):
    headers = auth["headers"]
    bill = await _with_line_account(client, headers, "bill", "6100")
    assert (await client.post(f"/docs/{bill}/finalize", headers=headers)).status_code == 200
    code = await _destination(client, headers, why)

    _refused(await client.patch(f"/docs/{bill}", headers=headers, json={
        "fields_changed": {"line_items": {"new": [{**_LINE, "account_code": code}]}}}), why, code)

    [line] = (await client.get(f"/docs/{bill}", headers=headers)).json()["line_items"]
    assert line["account_code"] == "6100"


@pytest.mark.asyncio
async def test_a_consignment_converted_to_a_bill_with_a_line_on_a_header_is_refused(client, auth):
    headers = auth["headers"]
    doc = await _with_line_account(client, headers, "consignment_in", HEADER)
    assert (await client.post(f"/docs/{doc}/finalize", headers=headers)).status_code == 200
    bills = (await client.get("/docs?doc_type=bill", headers=headers)).json()["items"]

    _refused(await client.post(f"/docs/{doc}/convert", headers=headers), "header", HEADER)

    assert (await client.get("/docs?doc_type=bill", headers=headers)).json()["items"] == bills


@pytest.mark.asyncio
async def test_a_batch_imported_bill_with_a_line_on_a_header_is_refused_alone(client, auth):
    headers = auth["headers"]

    def record(entity_id: str, code: str) -> dict:
        return {"entity_id": entity_id, "event_type": "doc.created", "data": _bill_snapshot(code),
                "source": "import", "idempotency_key": entity_id}

    r = await client.post("/docs/import/batch", headers=headers, json={"records": [
        record("doc:IMP-BAD", HEADER), record("doc:IMP-OK", "6100")]})

    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1, r.json()
    [error] = r.json()["errors"]
    assert "doc:IMP-BAD" in error and f"Account {HEADER} is a header account" in error, error
    assert (await client.get("/docs/doc:IMP-BAD", headers=headers)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["header", "inactive"])
async def test_a_transfer_into_a_bank_whose_account_cannot_take_a_posting_is_refused(client, auth, why):
    headers = auth["headers"]
    [bank] = (await client.get("/accounting/bank-accounts", headers=headers)).json()["items"]
    r = await client.post("/accounting/bank-accounts", headers=headers, json={
        "bank_name": "Savings", "account_number": "2", "bank_type": "savings", "currency": "USD"})
    assert r.status_code == 200, r.text
    savings = r.json()
    code = savings["chart_account_code"]
    if why == "header":
        await _account(client, headers, f"{code}1", parent=code, account_type="asset")
    else:
        assert (await _switch_off(client, headers, code)).status_code == 200

    _refused(await client.post("/accounting/transfers", headers=headers, json={
        "from_bank_id": bank["id"], "to_bank_id": savings["id"], "amount": 10, "date": "2026-03-01"}), why, code)


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

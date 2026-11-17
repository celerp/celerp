# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Equal money near the same date never proves two records are one transaction. A requested
posting is matched to an entry already in the books on its own only when the entry agrees with
it on the counterparty, or (neither naming one) on the account, or carries the line's
reference; amount and date alone stop the line with the entry shown, to match it or post
anyway. A transfer settles the other bank's line on its own only when that line is named or
carries the same reference; otherwise that line is left open with the transfer as its
suggestion."""
from __future__ import annotations

import pytest

import bank_statement_helpers as B


async def _contact(client, co, name: str) -> str:
    r = await client.post("/crm/contacts", json={"name": name, "contact_type": "vendor"}, headers=co["h"])
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _keyed(client, co, bk, amount: float, day: str, *, account: str = "5100", contact: str | None = None,
                 memo: str = "keyed") -> str:
    other = {"account": account, "debit": amount, "credit": 0.0, **({"contact": contact} if contact else {})}
    r = await client.post("/accounting/journal-entries", headers=co["h"], json={
        "ts": day, "memo": memo, "idempotency_token": B.key(),
        "entries": [other, {"account": bk["chart_account_code"], "debit": 0.0, "credit": amount}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _one_line(client, co, bk, rows, *, reference: str | None = None) -> dict:
    head = "Date,Description,Amount" + (",Reference" if reference else "")
    body = "\n".join([head] + [f"{d},{t},{a}" + (f",{reference}" if reference else "") for d, t, a in rows]) + "\n"
    total = sum(float(a) for _, _, a in rows)
    preview = await B.read(client, co, [(body.encode(), "st.csv", "text/csv")], bank_id=bk["id"])
    await B.import_statement(client, co, preview, bank_id=bk["id"], tick_all=True, header={
        "opening_balance": "0.00", "closing_balance": f"{total:.2f}", "period_start": "2026-01-01",
        "period_end": "2026-01-31"})
    return B.all_lines(await B.groups(client, co, bk["id"]))[0]


async def _post(client, co, bk, line, **fields) -> dict:
    r = await B.resolve(client, co, bk["id"], decisions=[{"line_ids": [line["id"]], "kind": "post", **fields}])
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


@pytest.mark.asyncio
async def test_two_suppliers_paid_the_same_sum_stay_two_payments(client):
    """Supplier A's 100 is in the books; a line asked to post as Supplier B's 100 near it stops
    with A's entry shown instead of being matched to it, and posts once the user says it is
    another payment."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    a, b = await _contact(client, co, "Supplier A"), await _contact(client, co, "Supplier B")
    line = await _one_line(client, co, bk, [("2026-01-10", "SUPPLIER B", "-100.00")])
    supplier_a = await _keyed(client, co, bk, 100.0, "2026-01-09", contact=a)
    row = await _post(client, co, bk, line, account_code="5100", contact=b)
    assert not row["ok"] and row["candidates"] == [supplier_a], row
    assert row["reason"]["message_key"] == "acct.recon_reason_looks_like", row
    after = B.line_by_text(await B.groups(client, co, bk["id"]), "SUPPLIER B")
    assert after["status"] == "unmatched" and after["suggestion"]["je_ids"] == [supplier_a], after
    assert after["suggestion"]["candidates"] == [supplier_a], after  # what an assistant or a reader posts anyway past
    row = await _post(client, co, bk, line, account_code="5100", contact=b, separate_from=row["candidates"])
    assert row["ok"] and not row["matched_instead"] and supplier_a not in row["je_ids"], row


@pytest.mark.asyncio
async def test_the_same_supplier_keyed_after_import_is_matched_and_says_so(client):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    a = await _contact(client, co, "Supplier A")
    line = await _one_line(client, co, bk, [("2026-01-10", "SUPPLIER A", "-100.00")])
    keyed = await _keyed(client, co, bk, 100.0, "2026-01-09", account="5300", contact=a)
    row = await _post(client, co, bk, line, account_code="5100", contact=a)
    assert row["ok"] and row["matched_instead"] and row["je_ids"] == [keyed], row
    assert row["reason"]["message_key"] == "acct.recon_reason_match", row


@pytest.mark.asyncio
async def test_another_account_and_no_contact_is_no_proof(client):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    line = await _one_line(client, co, bk, [("2026-01-10", "STATIONERY", "-25.00")])
    other = await _keyed(client, co, bk, 25.0, "2026-01-10", account="5300")
    row = await _post(client, co, bk, line, account_code="5100")
    assert not row["ok"] and row["candidates"] == [other], row


@pytest.mark.asyncio
async def test_the_lines_reference_on_the_entry_is_proof(client):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    line = await _one_line(client, co, bk, [("2026-01-10", "CHEQUE", "-60.00")], reference="CHQ4471")
    keyed = await _keyed(client, co, bk, 60.0, "2026-01-08", account="5300", memo="Paid by cheque CHQ4471")
    row = await _post(client, co, bk, line, account_code="5100")
    assert row["ok"] and row["matched_instead"] and row["je_ids"] == [keyed], row


@pytest.mark.asyncio
async def test_post_anyway_still_stops_on_an_entry_it_was_not_shown(client):
    """The user's "post anyway" covers the entries the stop showed, never one keyed since."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    line = await _one_line(client, co, bk, [("2026-01-10", "STATIONERY", "-25.00")])
    shown = await _keyed(client, co, bk, 25.0, "2026-01-10", account="5300")
    row = await _post(client, co, bk, line, account_code="5100")
    assert row["candidates"] == [shown], row
    later = await _keyed(client, co, bk, 25.0, "2026-01-11", account="6100")
    row = await _post(client, co, bk, line, account_code="5100", separate_from=[shown])
    assert not row["ok"] and row["candidates"] == [later], row


@pytest.mark.asyncio
async def test_two_unrelated_lines_of_equal_money_on_two_banks_are_not_paired(client):
    """A transfer from bank A leaves bank B's one line of the same money near it open, with the
    transfer entry as its suggestion, until the user confirms it."""
    co = await B.company(client)
    a = await B.bank(client, co["h"], name="A")
    b = await B.bank(client, co["h"], name="B")
    la = await _one_line(client, co, a, [("2026-01-08", "TRANSFER OUT", "-100.00")])
    lb = await _one_line(client, co, b, [("2026-01-09", "CUSTOMER DEPOSIT", "100.00")])
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                         "target_bank_id": b["id"]}])
    row = r.json()["results"][0]
    assert row["ok"] and row["paired_line_id"] is None, row
    other = B.line_by_text(await B.groups(client, co, b["id"]), "CUSTOMER DEPOSIT")
    assert other["status"] == "unmatched", other
    assert other["suggestion"]["kind"] == "match" and other["suggestion"]["je_ids"] == row["je_ids"], other
    table = await B.groups(client, co, b["id"])
    r = await B.resolve(client, co, b["id"], accept=B.accept(table, [g["key"] for g in table["groups"]], [lb["id"]]))
    assert r.json()["results"][0]["ok"], r.text
    assert B.line_by_text(await B.groups(client, co, b["id"], view="all"), "CUSTOMER DEPOSIT")["status"] == "matched"


@pytest.mark.asyncio
async def test_the_same_reference_on_both_lines_pairs_the_transfer(client):
    co = await B.company(client)
    a = await B.bank(client, co["h"], name="A")
    b = await B.bank(client, co["h"], name="B")
    la = await _one_line(client, co, a, [("2026-01-08", "TRANSFER OUT", "-100.00")], reference="TRF88213")
    lb = await _one_line(client, co, b, [("2026-01-09", "TRANSFER IN", "100.00")], reference="TRF88213")
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                         "target_bank_id": b["id"]}])
    row = r.json()["results"][0]
    assert row["ok"] and row["paired_line_id"] == lb["id"], row


def test_the_ladder_names_a_counterpart_only_on_evidence():
    """The suggestion ladder's transfer rung carries a counterpart only when the same reference
    is on both lines; equal money alone suggests the transfer and leaves the counterpart out."""
    from celerp_accounting import suggest

    line = {"id": "l1", "amount": "-100.00", "line_date": "2026-01-08", "reference": None, "description": "OUT"}
    pair = {"id": "p1", "bank_id": "b2", "amount": "100.00", "line_date": "2026-01-08", "reference": None}
    ctx = suggest.LadderContext(bank_id="b1", pair_lines=[pair])
    got = suggest._transfer(line, ctx)
    assert got["kind"] == "transfer" and "paired_line_id" not in got, got
    got = suggest._transfer({**line, "reference": "TRF1234"}, suggest.LadderContext(
        bank_id="b1", pair_lines=[{**pair, "reference": "TRF1234"}]))
    assert got["paired_line_id"] == "p1", got

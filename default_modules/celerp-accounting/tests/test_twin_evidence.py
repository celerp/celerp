# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Two records are one transaction on their own only when they share a transaction identifier:
a reference no other line of the company carries in the window, found in exactly one candidate
(``suggest.identified``). Equal money, a near date, a shared account and a shared contact only
rank the candidates: a requested posting with candidates left stops with them shown, to match one
or post anyway, and a transfer leaves the other bank's line open with the transfer as its
suggestion. Each case reaches the same books in either order, and they equal their rebuild."""
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
    return (await _lines(client, co, bk, [(*r, reference) for r in rows]))[0]


async def _lines(client, co, bk, rows) -> list[dict]:
    """Import ``rows`` (date, description, amount, reference or None) as one statement on ``bk``;
    its lines by date."""
    refs = any(r[3] for r in rows)
    head = "Date,Description,Amount" + (",Reference" if refs else "")
    body = "\n".join([head] + [f"{d},{t},{a}" + (f",{ref or ''}" if refs else "") for d, t, a, ref in rows]) + "\n"
    total = sum(float(r[2]) for r in rows)
    preview = await B.read(client, co, [(body.encode(), "st.csv", "text/csv")], bank_id=bk["id"])
    await B.import_statement(client, co, preview, bank_id=bk["id"], tick_all=True, header={
        "opening_balance": "0.00", "closing_balance": f"{total:.2f}", "period_start": "2026-01-01",
        "period_end": "2026-01-31"})
    return sorted(B.all_lines(await B.groups(client, co, bk["id"])), key=lambda l: (l["line_date"], l["description"]))


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
async def test_the_same_supplier_keyed_after_import_is_a_candidate_never_a_match(client):
    """One supplier is paid many times: an entry naming the line's contact and account, keyed
    after import, is shown to match or post past, never matched on its own."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    a = await _contact(client, co, "Supplier A")
    line = await _one_line(client, co, bk, [("2026-01-10", "SUPPLIER A", "-100.00")])
    keyed = await _keyed(client, co, bk, 100.0, "2026-01-09", account="5300", contact=a)
    row = await _post(client, co, bk, line, account_code="5300", contact=a)
    assert not row["ok"] and not row["matched_instead"] and row["candidates"] == [keyed], row
    assert row["reason"]["message_key"] == "acct.recon_reason_looks_like", row


async def _books(client, session, co) -> dict:
    """The trial balance by account, every journal entry's postings, and the rebuild check."""
    import uuid

    from sqlalchemy import select

    from celerp.models.projections import Projection
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    async def stored() -> dict:
        session.expire_all()
        rows = (await session.execute(select(Projection.entity_type, Projection.entity_id, Projection.state).where(
            Projection.company_id == uuid.UUID(co["company_id"])))).all()
        return {(t, e): s for t, e, s in rows}

    before = await stored()
    await lock_company(session, uuid.UUID(co["company_id"]))
    await ProjectionEngine.rebuild(session, company_id=uuid.UUID(co["company_id"]))
    await session.commit()
    assert await stored() == before, "the books differ from their rebuild from the ledger"
    tb = (await client.get("/accounting/trial-balance", headers=co["h"])).json()
    postings = sorted(sorted((x["account"], x.get("contact") or "", float(x["debit"]), float(x["credit"]))
                             for x in state.get("entries") or [])
                      for (kind, _), state in before.items() if kind == "journal_entry")
    return {"ledger": {l["code"]: (round(l["total_debit"], 2), round(l["total_credit"], 2)) for l in tb["lines"]},
            "postings": postings}


async def _settle(client, co, bk, line, *, stop: bool, **fields) -> dict:
    """Post ``line``: when ``stop``, the request must stop with candidates and the user posts
    anyway past them; otherwise it must post (or match by identifier) at once."""
    row = await _post(client, co, bk, line, **fields)
    if stop:
        assert not row["ok"] and not row["matched_instead"] and row["candidates"], row
        row = await _post(client, co, bk, line, separate_from=row["candidates"], **fields)
    assert row["ok"], row
    return row


async def _match_it(client, co, bk, line) -> dict:
    """Post ``line``; it must stop with candidates, and the user matches the first one."""
    row = await _post(client, co, bk, line, account_code="5300")
    assert not row["ok"] and not row["matched_instead"] and row["candidates"], row
    r = await B.resolve(client, co, bk["id"], decisions=[
        {"line_ids": [line["id"]], "kind": "match", "je_ids": row["candidates"][:1]}])
    assert r.json()["results"][0]["ok"], r.text
    return r.json()["results"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("second_first", [False, True])
async def test_two_payments_to_one_supplier_stay_two_in_either_order(client, session, second_first):
    """The first of two 100 payments to Supplier A, a day apart, is keyed before the statement
    arrives. Posting the second line to the supplier never takes the first payment's entry as its
    own: it stops with it shown and posts past it, and the first line is matched to its entry only
    when the user says so. Either order: both payments in the books once each."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    a = await _contact(client, co, "Supplier A")
    first = await _keyed(client, co, bk, 100.0, "2026-01-09", account="5300", contact=a, memo="Supplier A")
    l1, l2 = await _lines(client, co, bk, [("2026-01-09", "SUPPLIER A", "-100.00", None),
                                           ("2026-01-10", "SUPPLIER A", "-100.00", None)])
    if second_first:
        posted = await _settle(client, co, bk, l2, stop=True, account_code="5300", contact=a)
        matched = await _match_it(client, co, bk, l1)
    else:
        matched = await _match_it(client, co, bk, l1)
        posted = await _settle(client, co, bk, l2, stop=False, account_code="5300", contact=a)
    assert matched["je_ids"] == [first] and first not in posted["je_ids"], (matched, posted)
    books = await _books(client, session, co)
    assert books["ledger"]["5300"] == (200.0, 0.0), books
    assert sum(1 for p in books["postings"] if ("5300", a, 100.0, 0.0) in p) == 2, books


@pytest.mark.asyncio
async def test_two_unrelated_payments_to_one_expense_account_stay_two(client, session):
    """An entry on the expense account a line is posted to, keyed near it for the same money, is
    another payment until the user says otherwise: the line stops with it shown and posts past it."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    keyed = await _keyed(client, co, bk, 25.0, "2026-01-10", account="5300", memo="Office chairs")
    line = await _one_line(client, co, bk, [("2026-01-10", "STATIONERY", "-25.00")])
    row = await _settle(client, co, bk, line, stop=True, account_code="5300")
    assert keyed not in row["je_ids"] and not row["matched_instead"], row
    assert (await _books(client, session, co))["ledger"]["5300"] == (50.0, 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("INV-7001", "INV-7002"), ("INV-7002", "INV-7001")])
async def test_two_invoices_of_one_supplier_reach_the_same_books_in_either_order(client, session, order):
    """INV-7001 is keyed before the statement (5300, Supplier A, its reference in the memo). The
    statement pays INV-7001 and INV-7002, 100 each, references on the lines. A memo is no identifier,
    so the line the user posts first while the entry is open stops with it shown: INV-7001's line is
    matched to it by the user, INV-7002's posts past it to 6100. Either order: 5300 100 and 6100 100."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    a = await _contact(client, co, "Supplier A")
    keyed = await _keyed(client, co, bk, 100.0, "2026-01-08", account="5300", contact=a, memo="Paid INV-7001")
    l1, l2 = await _lines(client, co, bk, [("2026-01-09", "INV 7001 PAYMENT", "-100.00", "INV-7001"),
                                           ("2026-01-10", "INV 7002 PAYMENT", "-100.00", "INV-7002")])
    for ref in order:
        if ref == "INV-7001":
            assert (await _match_it(client, co, bk, l1))["je_ids"] == [keyed]
        else:
            row = await _settle(client, co, bk, l2, stop=order[0] == ref, account_code="6100", contact=a)
            assert keyed not in row["je_ids"] and not row["matched_instead"], row
    ledger = (await _books(client, session, co))["ledger"]
    assert ledger["5300"] == (100.0, 0.0) and ledger["6100"] == (100.0, 0.0), ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["PAYMENT", "SALARY PAYMENT", "12345", "RENT-ACME"])
async def test_a_reference_that_names_no_one_transaction_never_matches(client, session, reference):
    """A generic word, a short run of digits, or a reference two lines of the statement carry is no
    identifier: the keyed entry that carries it is a candidate to confirm, for either line."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    keyed = await _keyed(client, co, bk, 50.0, "2026-01-09", account="5300", memo=f"Paid {reference}")
    l1, l2 = await _lines(client, co, bk, [("2026-01-09", "DEBIT ONE", "-50.00", reference),
                                           ("2026-01-10", "DEBIT TWO", "-50.00", reference)])
    for line in (l2, l1):
        row = await _post(client, co, bk, line, account_code="5300")
        assert not row["ok"] and not row["matched_instead"] and row["candidates"] == [keyed], (reference, row)
    await _books(client, session, co)


@pytest.mark.asyncio
async def test_a_line_matched_instead_says_why_in_each_readers_language(client):
    """The reason a line matched an entry keyed after import is kept as its catalog key, so the
    answer a repeat replays reads in the language of whoever reads it, not the first request's."""
    from ui.i18n import english, refusal_text, set_lang

    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    _doc, number = await B.paid_invoice(client, co, bk, 40.0, "2026-01-05")
    line = await _one_line(client, co, bk, [("2026-01-05", "RECEIVED", "40.00")], reference=number)
    decisions, idem = [{"line_ids": [line["id"]], "kind": "post", "account_code": "4100"}], B.key()
    rows = [(await B.resolve(client, co, bk["id"], decisions=decisions, idem=idem,
                             headers={**co["h"], "Accept-Language": lang})).json()["results"][0]
            for lang in ("th", "en")]
    assert rows[0] == rows[1] and rows[0]["matched_instead"], rows
    reason = rows[0]["reason"]
    assert reason["message_key"] == "acct.recon_reason_match", reason
    set_lang("th")
    thai = refusal_text(reason)
    with english():
        assert refusal_text(reason) == reason["message"]
    assert thai and thai != reason["message"], thai


@pytest.mark.asyncio
async def test_another_account_and_no_contact_is_no_proof(client):
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    line = await _one_line(client, co, bk, [("2026-01-10", "STATIONERY", "-25.00")])
    other = await _keyed(client, co, bk, 25.0, "2026-01-10", account="5300")
    row = await _post(client, co, bk, line, account_code="5100")
    assert not row["ok"] and row["candidates"] == [other], row


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


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["TRANSFER", "XFER-OPS"])
async def test_two_transfers_of_one_amount_never_pair_on_a_shared_reference(client, reference):
    """Two transfers of 100 the same day, A to B and C to D, every line carrying ``reference`` (a
    generic word, or one reference four lines carry): no counterpart is paired on its own."""
    co = await B.company(client)
    a, b, c, d = [await B.bank(client, co["h"], name=n) for n in "ABCD"]
    la = await _one_line(client, co, a, [("2026-01-08", "OUT", "-100.00")], reference=reference)
    await _one_line(client, co, b, [("2026-01-08", "IN", "100.00")], reference=reference)
    await _one_line(client, co, c, [("2026-01-08", "OUT", "-100.00")], reference=reference)
    await _one_line(client, co, d, [("2026-01-08", "IN", "100.00")], reference=reference)
    r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                         "target_bank_id": b["id"]}])
    row = r.json()["results"][0]
    assert row["ok"] and row["paired_line_id"] is None, row
    assert B.line_by_text(await B.groups(client, co, b["id"]), "IN")["status"] == "unmatched"


def test_a_reference_is_an_identifier_only_when_it_names_one_transaction():
    from celerp_accounting import suggest

    assert [suggest.identifier(r) for r in ("12345", "abc", None, "")] == [None] * 4
    assert [suggest.identifier(r) for r in ("INV-7001", "123456", " Salary   payment ")] == [
        "inv-7001", "123456", "salary payment"]
    refs = suggest.References([{"id": "x", "line_date": "2025-06-01", "reference": "inv-7001 "}])
    assert suggest.identified("INV-7001", ["INV-7001", "INV-70012", None], refs.elsewhere({"x"})) == 0
    assert suggest.identified("INV-7001", ["INV-7001"], refs.elsewhere(set())) is None  # another line, any date
    assert suggest.identified("INV-7001", ["Paid INV-7001"], refs.elsewhere({"x"})) is None  # a word of a text
    assert suggest.identified("INV-7001", ["INV-7001", "inv-7001"], refs.elsewhere({"x"})) is None
    assert suggest.identified("ACME", [None, None], refs.elsewhere(set())) is None


def test_the_index_reads_only_the_lines_carrying_an_identifier_or_matched_to_an_entry():
    """``References.carrying``, ``elsewhere`` and ``matched_to`` give the same lines a scan of every
    line would, read through the identifier and entry indexes so a check never walks the company."""
    from celerp_accounting import suggest

    lines = [{"id": "a", "line_date": "2026-01-09", "reference": "INV-7001", "matched_je_ids": ["je:1"]},
             {"id": "b", "line_date": "2026-01-09", "reference": "INV-7002 ACME", "matched_je_ids": []},
             {"id": "c", "line_date": "2026-01-09", "reference": "ACME", "matched_je_ids": ["je:1", "je:2"]}]
    refs = suggest.References(lines)
    assert sorted(l["id"] for l in refs.carrying(["inv-7001", "acme", None])) == ["a", "c"]
    assert [l["id"] for l in refs.carrying(["inv-7002 acme"])] == ["b"] and refs.carrying(["inv-7002"]) == []
    assert refs.elsewhere({"c"})("acme") is False and refs.elsewhere({"a"})("acme") is True
    assert sorted(l["id"] for l in refs.matched_to({"je:1"})) == ["a", "c"]
    assert [l["id"] for l in refs.matched_to({"je:2", "je:9"})] == ["c"] and refs.matched_to(set()) == []


def test_the_ladder_names_a_counterpart_only_on_evidence():
    """The suggestion ladder's transfer rung carries a counterpart only when ``identified`` proves
    it (an identifier only the two lines carry); equal money alone suggests the transfer and
    leaves the counterpart out, and so does a reference a third line carries."""
    from celerp_accounting import suggest

    line = {"id": "l1", "amount": "-100.00", "line_date": "2026-01-08", "reference": None, "description": "OUT"}
    pair = {"id": "p1", "bank_id": "b2", "amount": "100.00", "line_date": "2026-01-08", "reference": None}
    ctx = suggest.LadderContext(bank_id="b1", pair_lines=[pair])
    got = suggest._transfer(line, ctx)
    assert got["kind"] == "transfer" and "paired_line_id" not in got, got
    got = suggest._transfer({**line, "reference": "TRF1234"}, suggest.LadderContext(
        bank_id="b1", pair_lines=[{**pair, "reference": "TRF1234"}]))
    assert got["paired_line_id"] == "p1", got
    crowded = suggest.References([{"id": "l3", "line_date": "2026-01-08", "reference": "TRF1234"}])
    got = suggest._transfer({**line, "reference": "TRF1234"}, suggest.LadderContext(
        bank_id="b1", pair_lines=[{**pair, "reference": "TRF1234"}], references=crowded))
    assert "paired_line_id" not in got, got


# ── Identity is a field naming one transaction on both sides, never a word free text shares ──

_SHARED_WORDS = {  # reference, keyed memo, account the line posts to
    "payee name": ("ACME", "Paid ACME", "6100"),
    "unlisted category word": ("UTILITIES", "UTILITIES office", "6200"),
    "period": ("2026-03", "Rent 2026-03", "6100"),
    "uncleared cheque": ("ACME", "Cheque to ACME", "6100"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed_first", [True, False])
@pytest.mark.parametrize("case", sorted(_SHARED_WORDS))
async def test_a_reference_a_memo_shares_is_a_candidate_never_a_match(client, session, case, keyed_first):
    """The line's reference is a word of the keyed entry's memo (a payee name, a category word, a
    period, an uncleared cheque's payee): the posting stops with the entry shown and posts past it.
    Keyed before or after the import, the books are the same."""
    reference, memo, account = _SHARED_WORDS[case]
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    keyed = await _keyed(client, co, bk, 100.0, "2026-01-05", account="5300", memo=memo) if keyed_first else None
    line = await _one_line(client, co, bk, [("2026-01-06", "DEBIT", "-100.00")], reference=reference)
    keyed = keyed or await _keyed(client, co, bk, 100.0, "2026-01-05", account="5300", memo=memo)
    row = await _settle(client, co, bk, line, stop=True, account_code=account)
    assert keyed not in row["je_ids"] and not row["matched_instead"], row
    ledger = (await _books(client, session, co))["ledger"]
    assert ledger["5300"] == (100.0, 0.0) and ledger[account] == (100.0, 0.0), ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [0, 1])
async def test_one_reference_on_lines_weeks_apart_names_neither(client, session, first):
    """Two payments of 100 to one supplier, each printed ACME, eight days apart, and one keyed in
    between: whichever line is posted first stops with the entry shown, so the books do not depend
    on the order and neither requested posting is lost."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    keyed = await _keyed(client, co, bk, 100.0, "2026-01-06", account="5300", memo="Paid ACME")
    lines = await _lines(client, co, bk, [("2026-01-02", "ACME A", "-100.00", "ACME"),
                                          ("2026-01-10", "ACME B", "-100.00", "ACME")])
    for line in (lines[first], lines[1 - first]):
        row = await _settle(client, co, bk, line, stop=True, account_code="6100")
        assert keyed not in row["je_ids"], row
    ledger = (await _books(client, session, co))["ledger"]
    assert ledger["5300"] == (100.0, 0.0) and ledger["6100"] == (200.0, 0.0), ledger


@pytest.mark.asyncio
async def test_the_number_of_the_one_document_a_payment_pays_is_its_identifier(client, session):
    """The automation that stays: a deposit whose reference is the number of an invoice paid once
    into this bank is that payment, so posting it is matched to the payment's entry and says so."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    _doc, number = await B.paid_invoice(client, co, bk, 120.0, "2026-01-08")
    line = await _one_line(client, co, bk, [("2026-01-09", "RECEIVED", "120.00")], reference=number)
    row = await _post(client, co, bk, line, account_code="4100")
    assert row["ok"] and row["matched_instead"] and len(row["je_ids"]) == 1, row
    ledger = (await _books(client, session, co))["ledger"]
    assert ledger["4100"] == (0.0, 120.0), ledger  # the invoice's sale only: the deposit is not posted again


@pytest.mark.asyncio
async def test_a_document_paid_twice_names_neither_payment(client, session):
    """An invoice paid in two parts of 60 on one day: its number is on both payments, so a line
    carrying it stops with both shown."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    _doc, number = await B.paid_invoice(client, co, bk, 120.0, "2026-01-08", payments=2)
    line = await _one_line(client, co, bk, [("2026-01-09", "RECEIVED", "60.00")], reference=number)
    row = await _post(client, co, bk, line, account_code="4100")
    assert not row["ok"] and not row["matched_instead"] and len(row["candidates"]) == 2, row

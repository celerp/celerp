# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A statement line is a book entry's transaction on its own only when its whole reference is the
number of the one document that entry pays, carried by no other document and no other statement
line of the company (``suggest.identified``); two statement lines never are, whatever they share,
until the user confirms it. Equal money, a near date, a shared account and a shared contact only
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


def test_only_a_document_number_carried_once_proves_identity():
    from celerp_accounting import suggest

    assert [suggest.normalised(r) for r in (None, "", "  ")] == [None] * 3
    assert [suggest.normalised(r) for r in ("INV-7001", " TRF   448812 ", "Straße")] == [
        "inv-7001", "trf 448812", "strasse"]
    refs = suggest.References([{"id": "x", "line_date": "2025-06-01", "reference": "inv-7001 "}])
    assert suggest.identified("INV-7001", ["INV-7001", "INV-70012", None], refs.elsewhere({"x"})) == 0
    assert suggest.identified("INV-7001", ["INV-7001"], refs.elsewhere(set())) is None  # another line, any date
    assert suggest.identified("INV-7001", ["Paid INV-7001"], refs.elsewhere({"x"})) is None  # a word of a text
    assert suggest.identified("INV-7001", ["INV-7001", "inv-7001"], refs.elsewhere({"x"})) is None
    assert suggest.identified("ACME", [None, None], refs.elsewhere(set())) is None
    assert suggest.identified("STRASSE 1", ["Straße 1"], refs.elsewhere(set())) == 0
    assert suggest.identified("20261115", ["20261115"], refs.elsewhere(set())) == 0  # its shape is no bar


def test_the_ladder_and_a_post_identify_a_line_s_entry_the_same_way():
    """The ladder's match and a post's keyed-instead check compose ``twins`` and ``identified``
    through one helper (``suggest.own_twin``): a line of another bank already matched to the
    candidate entry is that entry's own reading (a transfer counted on both accounts), never a
    second line carrying the number; a line matched to nothing that carries it still is."""
    from celerp_accounting import suggest

    entry = {"je_id": "je:1", "amount": "-100.00", "date": "2026-01-05", "identifier": "INV-7"}
    line = {"id": "l1", "amount": "-100.00", "line_date": "2026-01-05", "reference": "INV-7",
            "counterparty_key": "ACME", "description": "ACME"}

    def evidence(other: dict) -> dict:
        refs = suggest.References([{"id": "l1", "bank_account_id": "b1", "line_date": "2026-01-05",
                                    "reference": "INV-7", "matched_je_ids": []}, other])
        ctx = suggest.LadderContext(bank_id="b1", entries=[entry], references=refs)
        return suggest._matches([line], ctx)["l1"].get("evidence") or {}
    counted = {"id": "l2", "bank_account_id": "b2", "line_date": "2026-01-05", "reference": "inv-7",
               "matched_je_ids": ["je:1"]}
    assert evidence(counted) == {"identifier": True}
    assert evidence({**counted, "matched_je_ids": []}) == {}
    assert suggest.own_twin("l1", "INV-7", [entry], suggest.References([counted])) == 0
    assert suggest.own_twin("l1", "INV-7", [entry], suggest.References([{**counted, "matched_je_ids": []}])) is None


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]


def _period(rng) -> str:
    """A date or period in one of the forms statements print, separated or compact, in any case."""
    y, m, d = rng.choice([rng.randint(1990, 2039), rng.randint(2540, 2582)]), rng.randint(1, 12), rng.randint(1, 28)
    name = rng.choice([_MONTHS[m - 1], _MONTHS[m - 1][:3], _MONTHS[m - 1][:3] + "."])
    mm, dd, sep = f"{m:02d}", f"{d:02d}", rng.choice("-/.")
    return rng.choice([f"{y}{sep}{mm}", f"{y}{sep}{mm}{sep}{dd}", f"{dd}{sep}{mm}{sep}{y}", f"{name} {y}",
                       f"{y}{mm}{dd}", f"{dd}{mm}{y}", f"{y}{mm}", f"{mm}{y}", f"{y}", f"{dd} {name}"])


def _amount(rng) -> str:
    n = rng.randint(1, 99999)
    return rng.choice([str(n), f"{n:,}", f"{n:,}.00", f"{n}.{rng.randint(0, 99):02d}"])


def _reference(rng) -> str:
    """Anything a bank prints as a reference: words, dates and periods, amounts, and numbers that
    look like a document's, alone or together."""
    words = ["TRANSFER", "PAYMENT", "SALARY", "Rent", "fees", "Online Banking", "XFER-OPS", "DEPOSIT"]
    number = rng.choice(["INV", "TRF", "CHQ", "PO", "BILL", ""]) + rng.choice(["", " ", "-", "/"]) + \
        "".join(rng.choices("0123456789", k=rng.randint(1, 9)))
    parts = [rng.choice([rng.choice(words), _period(rng), _amount(rng), number]) for _ in range(rng.randint(1, 3))]
    return " ".join(p.upper() if rng.random() < .5 else p for p in parts)


def test_a_reference_proves_identity_only_as_a_stored_document_number_carried_once():
    """Seeded generative oracle over ``suggest.identified``: whatever a reference looks like (a
    label, a date or period in any form, an amount, a number like a document's), it proves the
    entry only when it equals, normalised, exactly one of the candidates' document numbers and no
    other statement line carries it; a document number of any shape does."""
    import random

    from celerp_accounting import suggest

    rng = random.Random(10)
    proved = 0
    for _ in range(3000):
        numbers = [_reference(rng) for _ in range(rng.randint(0, 4))]
        ref = rng.choice(numbers) if numbers and rng.random() < .5 else _reference(rng)
        ref = rng.choice([ref, ref.upper(), ref.lower(), f"  {ref} "])
        other = rng.random() < .2
        refs = suggest.References([{"id": "o", "line_date": "2026-01-01", "reference": ref}] if other else [])
        got = suggest.identified(ref, numbers, refs.elsewhere(set()))
        norm = " ".join(ref.split()).casefold()
        hits = [i for i, n in enumerate(numbers) if " ".join(n.split()).casefold() == norm]
        assert got == (hits[0] if len(hits) == 1 and not other else None), (ref, numbers, other)
        proved += got is not None
    assert proved > 500


def test_the_index_reads_only_the_lines_carrying_a_reference_or_matched_to_an_entry():
    """``References.carrying``, ``elsewhere`` and ``matched_to`` give the same lines a scan of every
    line would, read through the reference and entry indexes so a check never walks the company."""
    from celerp_accounting import suggest

    lines = [{"id": "a", "line_date": "2026-01-09", "reference": "INV-7001", "matched_je_ids": ["je:1"]},
             {"id": "b", "line_date": "2026-01-09", "reference": "INV-7002 CHQ 4471", "matched_je_ids": []},
             {"id": "c", "line_date": "2026-01-09", "reference": "CHQ 4471", "matched_je_ids": ["je:1", "je:2"]}]
    refs = suggest.References(lines)
    assert sorted(l["id"] for l in refs.carrying(["inv-7001", "chq 4471", None])) == ["a", "c"]
    assert [l["id"] for l in refs.carrying(["inv-7002 chq 4471"])] == ["b"] and refs.carrying(["inv-7002"]) == []
    assert refs.elsewhere({"c"})("chq 4471") is False and refs.elsewhere({"a"})("chq 4471") is True
    assert sorted(l["id"] for l in refs.matched_to({"je:1"})) == ["a", "c"]
    assert [l["id"] for l in refs.matched_to({"je:2", "je:9"})] == ["c"] and refs.matched_to(set()) == []


def test_the_ladder_never_names_a_counterpart():
    """The suggestion ladder's transfer rung suggests the transfer but never names the other bank's
    line, even when both carry one reference no other line carries: only the user pairs them."""
    from celerp_accounting import suggest

    line = {"id": "l1", "amount": "-100.00", "line_date": "2026-01-08", "reference": "TRF1234", "description": "OUT"}
    pair = {"id": "p1", "bank_id": "b2", "amount": "100.00", "line_date": "2026-01-08", "reference": "TRF1234"}
    got = suggest._transfer(line, suggest.LadderContext(bank_id="b1", pair_lines=[pair]))
    assert got["kind"] == "transfer" and "paired_line_id" not in got and "evidence" not in got, got


# ── Identity is a document number on both sides, never a word free text shares ──

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
async def test_a_number_two_documents_carry_names_neither_payment(client, session):
    """An invoice number that another document of the company carries too is not one transaction's
    name: a line carrying it stops with the payment shown."""
    from sqlalchemy import select
    from celerp.models.projections import Projection
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    _doc, number = await B.paid_invoice(client, co, bk, 120.0, "2026-01-08")
    other, _ = await B.paid_invoice(client, co, bk, 90.0, "2026-01-08", paid=0)
    proj = (await session.execute(select(Projection).where(Projection.entity_id == other))).scalar_one()
    proj.state = {**proj.state, "ref_id": number, "doc_number": number}
    await session.flush()
    line = await _one_line(client, co, bk, [("2026-01-09", "RECEIVED", "120.00")], reference=number)
    row = await _post(client, co, bk, line, account_code="4100")
    assert not row["ok"] and not row["matched_instead"] and len(row["candidates"]) == 1, row


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


async def _renumber(session, entity_id: str, number: str) -> None:
    from sqlalchemy import select
    from celerp.models.projections import Projection
    proj = (await session.execute(select(Projection).where(Projection.entity_id == entity_id))).scalar_one()
    proj.state = {**proj.state, "ref_id": number, "doc_number": number}
    await session.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["20261115", "PAYMENT 500", "Salary"])
async def test_a_document_number_names_its_payment_whatever_its_shape(client, session, number):
    """A document really numbered like a date, an amount or a word is still that document: a
    deposit carrying its number is matched to its one payment."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    doc, _ = await B.paid_invoice(client, co, bk, 120.0, "2026-01-08")
    await _renumber(session, doc, number)
    line = await _one_line(client, co, bk, [("2026-01-09", "RECEIVED", "120.00")], reference=number.upper())
    row = await _post(client, co, bk, line, account_code="4100")
    assert row["ok"] and row["matched_instead"] and len(row["je_ids"]) == 1, row


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["document", "line"])
async def test_a_number_another_record_carries_in_another_case_names_neither(client, session, where):
    """``STRASSE-7001`` and ``Straße-7001`` are one number (casefolded alike): when another
    document or another statement line carries the paid document's number in that other form, a
    line carrying it stops with the payment shown."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    doc, _ = await B.paid_invoice(client, co, bk, 120.0, "2026-01-08")
    await _renumber(session, doc, "STRASSE-7001")
    if where == "document":
        other, _ = await B.paid_invoice(client, co, bk, 90.0, "2026-01-08", paid=0)
        await _renumber(session, other, "Straße-7001")
        line = await _one_line(client, co, bk, [("2026-01-09", "RECEIVED", "120.00")], reference="STRASSE-7001")
    else:
        line, _ = await _lines(client, co, bk, [("2026-01-09", "RECEIVED", "120.00", "STRASSE-7001"),
                                                ("2026-01-28", "FEE", "-5.00", "Straße-7001")])
    row = await _post(client, co, bk, line, account_code="4100")
    assert not row["ok"] and not row["matched_instead"] and len(row["candidates"]) == 1, row


# ── Two statement lines are one transaction only when the user confirms it ──

async def _suggested(client, co, bk) -> list[tuple[dict, bool]]:
    """Run the suggestion ladder on ``bk``; each line with whether its group starts ticked."""
    r = await client.post(f"{B.API}/bank/{bk['id']}/suggest", json={"idempotency_key": B.key()}, headers=co["h"])
    assert r.status_code == 200, r.text
    return [(l, g["ticked"]) for g in (await B.groups(client, co, bk["id"]))["groups"] for l in g["lines"]]


async def _statuses(client, co, *banks) -> list[str]:
    tables = [await B.groups(client, co, bk["id"], view="all") for bk in banks]
    return sorted(l["status"] for table in tables for l in B.all_lines(table))


async def _moved(client, session, co, before: dict) -> list:
    """The postings the reconciliation made since ``before`` (the company's books before it),
    after checking the books equal their rebuild."""
    left = list(before["postings"])
    out = []
    for p in (await _books(client, session, co))["postings"]:
        if p in left:
            left.remove(p)
        else:
            out.append(p)
    assert not left, left
    return out


async def _two_banks(client, reference: str, first: int) -> tuple:
    """Banks A and B, each with one line of 100 a day apart, out of A and into B, both carrying
    ``reference``; statement ``first`` (0 is A's) imported first."""
    co = await B.company(client)
    a = await B.bank(client, co["h"], name="A")
    b = await B.bank(client, co["h"], name="B")
    rows = [(a, ("2026-01-08", "OUT", "-100.00")), (b, ("2026-01-09", "IN", "100.00"))]
    for bk, row in (rows[first], rows[1 - first]):
        await _one_line(client, co, bk, [row], reference=reference)
    return co, a, b


# A bank reference never makes two statement lines one transaction, whatever its shape: a label,
# a date or period written compactly, an amount, or a transaction reference on both sides.
LINE_REFERENCES = ["TRANSFER", "PAYMENT", "20261115", "15112569", "TRANSFER 20261115", "SALARY 202611",
                   "256911", "PAYMENT 500", "TRF 448812"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", LINE_REFERENCES)
async def test_two_lines_sharing_a_reference_pair_only_when_the_user_confirms(
        client, session, reference):
    """Two lines on two banks, equal and opposite money on near dates, both carrying one
    reference: the transfer is offered but nothing starts ticked, a transfer the
    user posts from A leaves B's line open with it as the suggestion, and the two are one
    transaction only once the user confirms it. Either import order reaches the same books."""
    books = []
    for first in (0, 1):
        co, a, b = await _two_banks(client, reference, first)
        before = await _books(client, session, co)
        for bk in (a, b):
            for line, ticked in await _suggested(client, co, bk):
                assert line["suggestion"]["kind"] == "transfer", line
                assert not line["suggestion"].get("paired_line_id") and not ticked, line
        la = B.all_lines(await B.groups(client, co, a["id"]))[0]
        r = await B.resolve(client, co, a["id"], decisions=[{"line_ids": [la["id"]], "kind": "transfer",
                                                             "target_bank_id": b["id"]}])
        row = r.json()["results"][0]
        assert row["ok"] and row["paired_line_id"] is None, row
        table = await B.groups(client, co, b["id"])
        lb = B.line_by_text(table, "IN")
        assert lb["status"] == "unmatched" and lb["suggestion"]["je_ids"] == row["je_ids"], lb
        assert not any(g["ticked"] for g in table["groups"]), table
        r = await B.resolve(client, co, b["id"], accept=B.accept(table, [g["key"] for g in table["groups"]],
                                                                  [lb["id"]]))
        assert r.json()["results"][0]["ok"], r.text
        assert await _statuses(client, co, a, b) == ["matched", "posted"]  # A posts the transfer, B is its other side
        books.append(await _moved(client, session, co, before))
    assert books[0] == books[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", LINE_REFERENCES)
async def test_a_reversal_pair_sharing_a_reference_is_offered_not_accepted(client, session, reference):
    """A payment out and the same money back in on one bank two days later, both referenced
    with one reference: the reversal is offered, its group does not start ticked, and the
    lines settle only when the user ticks them. Either row order reaches the same books."""
    books = []
    for first in (0, 1):
        co = await B.company(client)
        bk = await B.bank(client, co["h"])
        rows = [("2026-01-05", "ACME LTD", "-100.00", reference), ("2026-01-07", "ACME LTD", "100.00", reference)]
        await _lines(client, co, bk, [rows[first], rows[1 - first]])
        before = await _books(client, session, co)
        suggested = await _suggested(client, co, bk)
        assert [l["suggestion"]["source"] for l, _ in suggested] == ["reversal", "reversal"], suggested
        assert not any(ticked or l["suggestion"].get("evidence") for l, ticked in suggested), suggested
        table = await B.groups(client, co, bk["id"])
        assert await _statuses(client, co, bk) == ["unmatched", "unmatched"]
        r = await B.resolve(client, co, bk["id"], accept=B.accept(
            table, [g["key"] for g in table["groups"]], [l["id"] for l, _ in suggested]))
        assert all(x["ok"] for x in r.json()["results"]), r.text
        assert "unmatched" not in await _statuses(client, co, bk)
        books.append(await _moved(client, session, co, before))
    assert books[0] == books[1]


@pytest.mark.parametrize("source,kind,evidence", [
    ("match", "match", {"identifier": True}),
    ("document", "pay_doc", {"names_contact": True, "doc_id": "d1"}),
    ("rule", "post", {"names_contact": True, "rule_id": "r1"}),
    ("slip", "post", {"names_contact": True, "file_id": "f1"}),
    ("history", "post", {"names_contact": True, "history": "ACME"}),
])
def test_every_source_starts_ticked_only_with_the_evidence_it_names(source, kind, evidence):
    """A group starts ticked only on a suggestion whose evidence names the one record it rests
    on: the identified entry, the document, the rule, the slip or the history key. The same
    suggestion without that record, or with it empty, waits for the user to tick it."""
    from celerp_accounting.bank_statements import _starts_ticked

    def line(**suggestion):
        return {"counterparty_key": "ACME", "description": "ACME",
                "suggestion": {"source": source, "kind": kind, "account_code": "6100", **suggestion}}
    assert _starts_ticked(line(evidence=evidence))
    named = {k: v for k, v in evidence.items() if k != "names_contact"}
    assert not _starts_ticked(line(evidence={k: v for k, v in evidence.items() if k not in named}))
    assert not _starts_ticked(line(evidence={**evidence, **{k: None for k in named}}))
    assert not _starts_ticked(line())
    if kind == "post":
        assert not _starts_ticked(line(evidence=evidence, account_code=None))


@pytest.mark.parametrize("source", ["transfer", "reversal", "document_amount", "answer", None])
def test_a_source_that_names_no_one_record_never_starts_ticked(source):
    from celerp_accounting.bank_statements import _starts_ticked

    assert not _starts_ticked({"counterparty_key": "ACME", "description": "ACME", "suggestion": {
        "source": source, "kind": "post", "account_code": "6100", "evidence": {"names_contact": True, "answer": True}}})


async def _open_bill(client, co, total: float, contact: str) -> tuple[str, str]:
    """A finalized supplier bill of ``total`` with nothing paid: its id and its number."""
    r = await client.post("/docs", headers=co["h"], json={
        "doc_type": "bill", "contact_id": contact, "issue_date": "2026-01-02",
        "line_items": [{"name": "Supplies", "quantity": 1, "unit_price": total, "line_total": total}],
        "subtotal": total, "tax": 0.0, "total": total})
    assert r.status_code == 200, r.text
    doc = r.json()["id"]
    assert (await client.post(f"/docs/{doc}/finalize", json={}, headers=co["h"])).status_code == 200
    state = (await client.get(f"/docs/{doc}", headers=co["h"])).json()
    return doc, state.get("ref_id") or state["doc_number"]


async def _post_ticked(client, co, bk) -> None:
    """Post ticked as the bank page sends it: the groups that start ticked, nothing else."""
    table = await B.groups(client, co, bk["id"])
    keys = [g["key"] for g in table["groups"] if g["ticked"]]
    if keys:
        r = await B.resolve(client, co, bk["id"], accept=B.accept(table, keys))
        assert r.status_code == 200, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("suppliers", [1, 2])
async def test_an_open_bill_of_the_same_amount_is_shown_never_ticked(client, suppliers):
    """Equal money names no document: with one open bill of the amount, or two of different
    suppliers, the payment shows a bill unticked and Post ticked pays none of them."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    bills = [await _open_bill(client, co, 300.0, await _contact(client, co, f"Supplier {i}")) for i in range(suppliers)]
    await _one_line(client, co, bk, [("2026-01-10", "PAYMENT OUT", "-300.00")])
    [(line, ticked)] = await _suggested(client, co, bk)
    assert line["suggestion"]["kind"] == "pay_doc" and line["suggestion"]["source"] == "document_amount", line
    assert not ticked, line
    await _post_ticked(client, co, bk)
    assert await _statuses(client, co, bk) == ["unmatched"]
    for doc, _number in bills:
        assert (await client.get(f"/docs/{doc}", headers=co["h"])).json()["amount_outstanding"] == 300.0


@pytest.mark.asyncio
async def test_a_payment_naming_the_one_bill_it_pays_starts_ticked(client):
    """A payment whose reference is the number of the one open bill of its amount, carried by
    nothing else, is that bill's payment: it starts ticked and Post ticked pays the bill."""
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    doc, number = await _open_bill(client, co, 300.0, await _contact(client, co, "Supplier A"))
    await _open_bill(client, co, 300.0, await _contact(client, co, "Supplier B"))
    await _one_line(client, co, bk, [("2026-01-10", "PAYMENT OUT", "-300.00")], reference=number)
    [(line, ticked)] = await _suggested(client, co, bk)
    assert line["suggestion"]["source"] == "document" and line["suggestion"]["docs"][0]["doc_id"] == doc, line
    assert ticked, line
    await _post_ticked(client, co, bk)
    assert await _statuses(client, co, bk) == ["posted"]
    assert (await client.get(f"/docs/{doc}", headers=co["h"])).json()["amount_outstanding"] == 0

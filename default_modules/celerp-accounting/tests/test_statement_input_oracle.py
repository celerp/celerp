# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Seeded generated statement input through the real read, preview, import and resolve
routes: every file and every decision is either taken as a reference reading says it should
be, or refused with a keyed reason. Never a server error, never an empty statement read
from a file that held nothing. A generated statement printed with its signs reversed (a card
statement) reads to the same lines, fingerprints, counterparties and duplicates as the same
statement printed in the bank's convention, with running balances or without. Two generated
statements cut from one ledger (the same file again, an overlap, the next one; read with running
balances, the bank's ids or neither; reversed or not) leave out, offer for review and import each
line as an independent count of the duplicate rule says."""

from __future__ import annotations

import random
from decimal import Decimal, InvalidOperation

import pytest

import bank_statement_helpers as B
from celerp_accounting.models import BankStatementLine, ReconciliationSession
from celerp_accounting.statement_reader import review_statement

pytestmark = pytest.mark.asyncio

_LIMIT = Decimal(10) ** 16          # what a stored statement amount can hold
_AMOUNTS = ["-5.00", "12.34", "-0.50", "250.00", "-99.99", "1e17", "-123456789012345678", "7"]
_PADS = ["", " ", "  ", "\t", " ", " ", "  "]


def _fits(text: str) -> bool:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return False
    return value.is_finite() and abs(value) < _LIMIT


def _ofx(rows: list[tuple[str, str, str]]) -> bytes:
    """An OFX file of (fitid, date, amount) transactions."""
    body = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d.replace('-', '')}<TRNAMT>{a}<FITID>{f}"
                   f"<NAME>PAYEE {i}</STMTTRN>" for i, (f, d, a) in enumerate(rows))
    return (f"OFXHEADER:100\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD<BANKTRANLIST>{body}"
            "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>").encode()


def _file(rng: random.Random) -> tuple[bytes, str, list[str]]:
    """A generated statement file and the amounts of the transactions it holds."""
    kind = rng.choice(["ofx", "ofx", "csv", "empty_csv", "header_only_csv", "garbage_ofx", "empty_ofx"])
    count = rng.randint(1, 5)
    dates = [f"2026-01-{rng.randint(2, 28):02d}" for _ in range(count)]
    amounts = [rng.choice(_AMOUNTS) for _ in range(count)]
    if kind == "ofx":
        fitids = [rng.choice(["T1", "T2", "T3", " T1 "]) for _ in range(count)]
        return _ofx(list(zip(fitids, dates, amounts))), "st.ofx", amounts
    if kind == "csv":
        return B.csv_bytes([(d, f"PAYEE {i}", a) for i, (d, a) in enumerate(zip(dates, amounts))]), "st.csv", amounts
    return {"empty_csv": (b"", "st.csv"), "header_only_csv": (b"Date,Description,Amount\n", "st.csv"),
            "garbage_ofx": (b"\x00\x01 this is not a statement \xff", "st.ofx"),
            "empty_ofx": (b"", "st.ofx")}[kind] + ([],)


@pytest.mark.parametrize("seed", range(12))
async def test_generated_statement_files_import_or_refuse_by_key(client, seed):
    rng = random.Random(seed)
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    content, name, amounts = _file(rng)
    preview = await B.read(client, co, [(content, name, "application/octet-stream")], bank_id=bk["id"])
    if not amounts:
        assert preview["statements"] == [], name
        [failed] = preview["failed_steps"]
        assert failed["reason"] and failed["retry"] is False
        return
    [st] = preview["statements"]
    assert len(st["lines"]) == len(amounts)
    closing = sum((Decimal(a) for a in amounts if _fits(a)), Decimal(0))
    body = {"job_id": preview["job_id"], "bank_id": bk["id"], "idempotency_key": B.key(), "statements": [{
        "header": {**st["header"], "opening_balance": "0", "closing_balance": f"{closing:.2f}",
                   "period_start": "2026-01-01", "period_end": "2026-01-31"},
        "lines": [{**line, "ticked": True} for line in st["lines"]]}]}
    if preview.get("column_map"):
        body["column_map"] = preview["column_map"]
    r = await client.post(f"{B.API}/import-statement", json=body, headers=co["h"])
    assert r.status_code < 500, r.text
    if all(_fits(a) for a in amounts):
        assert r.status_code == 200, r.text
        assert r.json()["sessions"][0]["imported"] == len(amounts)
    else:
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["message_key"].startswith("acct.recon_block_no_amount")


def _money_ok(value: float) -> bool:
    amount = Decimal(str(value))
    return amount != 0 and abs(amount) < _LIMIT and amount == amount.quantize(Decimal("0.01"))


@pytest.mark.parametrize("seed", range(10))
async def test_generated_decisions_post_or_refuse_by_key(client, session, seed):
    """Split amounts and account codes as a model or a paste might send them: a padded code
    is the code, an amount the books cannot hold is refused by its key."""
    rng = random.Random(seed)
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    await B.statement(client, co, bk["id"], [("2026-01-05", "FEE", "-5.00")], opening="0.00", closing="-5.00",
                      start="2026-01-01", end="2026-01-31")
    line = B.all_lines(await B.groups(client, co, bk["id"]))[0]
    code = rng.choice(["5100", "5200", "9999"])
    padded = rng.choice(_PADS) + code + rng.choice(_PADS)
    first = rng.choice([2.0, 1.25, 1e300, -1e300, 1e16, 0.001, 0.0, 4.999])
    second = float(Decimal("5") - Decimal(str(first))) if _money_ok(first) else 3.0
    decision = {"line_ids": [line["id"]], "kind": "post",
                "splits": [{"account_code": padded, "amount": first}, {"account_code": "5300", "amount": second}]}
    r = await B.resolve(client, co, bk["id"], decisions=[decision])
    assert r.status_code < 500, r.text
    if not (_money_ok(first) and _money_ok(second)):
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["message_key"].startswith("acct.err_amount_")
        return
    assert r.status_code == 200, r.text
    [result] = r.json()["results"]
    assert result["ok"] is (code != "9999"), result
    if result["ok"]:
        je = await B.journal(session, co, result["je_ids"][0])
        assert sorted(e["account"] for e in je["entries"] if e.get("debit")) == sorted([code, "5300"])


_LINE_TEXT = ["ATM", "TRANSFER", "COFFEE SHOP", "ATM FEE", "DEPOSIT", "HOTEL 123456789012"]
_LINE_AMOUNTS = ["-5.00", "12.34", "-0.50", "250.00", "-99.99", "7.00"]


def _bank_statement(rng: random.Random) -> tuple[dict, list[dict]]:
    """A statement in the bank's sign convention whose amounts do not net to nothing (so only
    one convention ties), repeated identical lines included."""
    while True:
        opening, lines = Decimal(rng.choice(["0.00", "100.00", "-50.00"])), []
        for _ in range(rng.randint(1, 8)):
            if lines and rng.random() < 0.25:
                lines.append(dict(lines[-1]))
            else:
                lines.append({"line_date": rng.choice(["2026-01-02", "2026-01-03"]),
                              "description": rng.choice(_LINE_TEXT), "amount": rng.choice(_LINE_AMOUNTS)})
        total = sum((Decimal(l["amount"]) for l in lines), Decimal(0))
        if total:
            break
    running, balances = opening, rng.random() < 0.5
    for line in lines:
        running += Decimal(line["amount"])
        line["raw_balance"] = str(running) if balances else None
    header = {"currency": "USD", "period_start": "2026-01-01", "period_end": "2026-01-31",
              "opening_balance": str(opening)}
    if not balances or rng.random() < 0.5:
        header["closing_balance"] = str(opening + total)
    return header, lines


@pytest.mark.parametrize("seed", range(40))
async def test_a_statement_printed_with_reversed_signs_reads_to_the_same_line_identities(seed):
    rng = random.Random(seed)
    header, bank_lines = _bank_statement(rng)
    card_lines = [{**l, "amount": str(-Decimal(l["amount"]))} for l in bank_lines]
    plain = review_statement(header, bank_lines, bank_currency="USD")
    known = [{**l, "statement": "2025-12-31"} for l in plain["lines"] if rng.random() < 0.4]
    held = {}
    for line in rng.sample(plain["lines"], rng.randint(0, len(plain["lines"]))):
        bucket = (line["line_date"], line["amount"])
        held[bucket] = held.get(bucket, 0) + 1
    bank = review_statement(header, bank_lines, bank_currency="USD", known=known, held_counts=held)
    card = review_statement(header, card_lines, bank_currency="USD", known=known, held_counts=held)
    assert (bank["flipped"], bank["blocking"], card["flipped"], card["blocking"]) == (False, [], True, [])
    fields = ("amount", "fingerprint", "counterparty_key", "duplicate", "repeats", "probable", "ticked")
    assert [[l[f] for f in fields] for l in card["lines"]] == [[l[f] for f in fields] for l in bank["lines"]]


# ── Overlapping statements: a line read again or a repeat of it ──────────────
# A bank's ledger of genuine transactions drawn from a tiny pool, so identical transactions
# abound; two statements cut from it overlap (a line read again) or follow it (repeats only).
# Each statement is read one way: "none" (no running balance, no bank id), "balance" (running
# balances) or "fitid" (OFX: the bank's id, read as the reference too), the second one printed
# in the bank's convention or reversed.
_POOL = [("2026-01-02", "COFFEE SHOP", "-4.50"), ("2026-01-02", "ATM", "-20.00"), ("2026-01-03", "DEPOSIT", "250.00")]
_MODES = ("none", "balance", "fitid")


def _ledger(rng: random.Random) -> tuple[Decimal, list[dict]]:
    opening, running, out = Decimal("100.00"), Decimal("100.00"), []
    for k in range(rng.randint(2, 12)):
        date, text, amount = rng.choice(_POOL)
        running += Decimal(amount)
        out.append({"line_date": date, "description": text, "amount": Decimal(amount), "balance": running,
                    "fitid": f"FIT{k:03d}", "id": k})
    return opening, out


def _statement(opening: Decimal, ledger: list[dict], lo: int, hi: int, mode: str,
               reversed_: bool) -> tuple[dict, list[dict]]:
    before = opening if lo == 0 else ledger[lo - 1]["balance"]
    lines = []
    for tx in ledger[lo:hi]:
        line = {"line_date": tx["line_date"], "description": tx["description"],
                "amount": str(-tx["amount"] if reversed_ else tx["amount"]), "raw_balance": None}
        if mode == "balance":
            line["raw_balance"] = str(tx["balance"])
        if mode == "fitid":
            line.update(source="ofx", reference=tx["fitid"])
        lines.append(line)
    header = {"currency": "USD", "period_start": "2026-01-01", "period_end": "2026-01-31",
              "opening_balance": str(before), "closing_balance": str(ledger[hi - 1]["balance"])}
    return header, lines


def _oracle(held: list[dict], held_mode: str, read: list[dict], mode: str) -> list[str]:
    """The rule counted per transaction, independent of the reader: with the bank's id on both
    sides only the id decides; with running balances on both sides only the same balance is
    the same line; otherwise k identical lines against m held leave min(k, m) for review."""
    base = lambda tx: (tx["line_date"], tx["description"], tx["amount"])
    if mode == held_mode and mode != "none":
        key = (lambda tx: tx["fitid"]) if mode == "fitid" else (lambda tx: (base(tx), tx["balance"]))
        pool = [key(tx) for tx in held]
        out = []
        for tx in read:
            out.append("duplicate" if key(tx) in pool else "new")
            if key(tx) in pool:
                pool.remove(key(tx))
        return out
    left = {}
    for tx in held:
        left[base(tx)] = left.get(base(tx), 0) + 1
    out = []
    for tx in read:
        out.append("repeat" if left.get(base(tx), 0) > 0 else "new")
        left[base(tx)] = left.get(base(tx), 0) - 1
    return out


def _seen(line: dict) -> str:
    if line["duplicate"]:
        return "duplicate"
    return "repeat" if line["repeats"] and not line["ticked"] else "new" if line["ticked"] else "other"


@pytest.mark.parametrize("seed", range(200))
async def test_overlapping_statements_tell_a_line_read_again_from_a_repeat_by_the_rule(seed):
    rng = random.Random(seed)
    opening, ledger = _ledger(rng)
    n = len(ledger)
    a_lo, a_hi = 0, rng.randint(1, n - 1)
    b_lo = rng.choice([0, rng.randint(0, a_hi), a_hi])     # the same file again, an overlap, or the next one
    b_hi = n if b_lo == 0 and rng.random() < 0.5 else rng.randint(max(b_lo, a_hi) if b_lo < a_hi else b_lo + 1, n)
    if b_lo == 0 and rng.random() < 0.3:
        b_hi = a_hi
    a_mode, b_mode = rng.choice(_MODES), rng.choice(_MODES)
    b_total = sum((tx["amount"] for tx in ledger[b_lo:b_hi]), Decimal(0))
    reversed_ = bool(b_total) and rng.random() < 0.4
    a = review_statement(*_statement(opening, ledger, a_lo, a_hi, a_mode, False), bank_currency="USD")
    assert a["blocking"] == []
    known = [{**l, "statement": "2026-01-15"} for l in a["lines"]]
    b = review_statement(*_statement(opening, ledger, b_lo, b_hi, b_mode, reversed_), bank_currency="USD",
                         known=known)
    assert (b["blocking"], b["flipped"]) == ([], reversed_)
    read, held = ledger[b_lo:b_hi], ledger[a_lo:a_hi]
    want = _oracle(held, a_mode, read, b_mode)
    seen = [_seen(l) for l in b["lines"]]
    # Lines identical as read may take their verdicts in any order: compare per identity.
    ident = lambda tx: (tx["line_date"], tx["description"], tx["amount"],
                        tx["balance"] if b_mode == "balance" else None, tx["fitid"] if b_mode == "fitid" else None)
    assert sorted(zip(map(ident, read), seen)) == sorted(zip(map(ident, read), want))
    assert all(l["repeats"] == "2026-01-15" for l in b["lines"] if l["repeats"])
    # A line that may import never collides with a held line or another line of the statement.
    importable = [l["fingerprint"] for l in b["lines"] if not l["duplicate"]]
    assert len(set(importable)) == len(importable) and not set(importable) & {l["fingerprint"] for l in known}
    # Against what really happened: a line left out as read again is one, and no more lines
    # import ticked than are genuinely new, per transaction kind.
    unique_balance = len({(tx["line_date"], tx["description"], tx["amount"], tx["balance"]) for tx in ledger}) == n
    held_ids = {tx["id"] for tx in held}
    if b_mode == "fitid" or unique_balance:
        assert all(tx["id"] in held_ids for tx, s in zip(read, seen) if s == "duplicate")
    for kind in _POOL:
        mine = [(tx, s) for tx, s in zip(read, seen) if (tx["line_date"], tx["description"], str(tx["amount"])) == kind]
        assert sum(s == "new" for _, s in mine) <= sum(tx["id"] not in held_ids for tx, _ in mine)


# What a stored line and statement can hold, read from their columns (the one source the
# import's check reads too); each generated value is sized around its limit.
_LINE_LIMITS = {f: BankStatementLine.__table__.c[f].type.length
                for f in ("line_date", "reference", "fitid", "counterparty_account")}
_HEADER_LIMITS = {"period_start": ReconciliationSession.__table__.c.period_start.type.length,
                  "period_end": ReconciliationSession.__table__.c.statement_date.type.length}
_STARTS = {"line_date": "2026-01-15", "period_start": "2026-01-01", "period_end": "2026-01-31",
           "reference": "R", "fitid": "F", "counterparty_account": "9"}


def _sized(rng: random.Random, field: str, limit: int) -> str:
    """A value of ``field`` one under, at, one over or far over ``limit`` characters."""
    n = rng.choice([limit - 1, limit, limit + 1, limit * 4])
    start = _STARTS[field]
    return start + "Z" * (n - len(start))


@pytest.mark.parametrize("seed", range(24))
async def test_a_value_longer_than_its_stored_column_is_refused_by_name_and_never_reaches_the_database(client, seed):
    """Through the read, preview and import routes, a statement whose header or line values are
    sized around what their columns hold (an edited line, or a bank file's own FITID imported as
    read): imported with every value stored as sent when each fits, otherwise refused naming the
    field, never a server error. A long description still imports, its counterparty fitting."""
    rng = random.Random(seed)
    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    header = {"opening_balance": "0", "closing_balance": "-15.00", "period_start": "2026-01-01",
              "period_end": "2026-01-31"}
    if rng.random() < 0.3:      # the bank file's own FITID, imported as the file reads it
        fitid = _sized(rng, "fitid", _LINE_LIMITS["fitid"])
        content, name = _ofx([(f" {fitid} ", "2026-01-15", "-15.00")]), "st.ofx"
        sized, edit = {"fitid": fitid, "reference": fitid}, None     # an OFX FITID is read as the reference too
    else:
        content, name = B.csv_bytes([("2026-01-15", "PAYEE " + " ".join(["W" * 70] * 3), "-15.00")]), "st.csv"
        fields = rng.sample(sorted(_LINE_LIMITS | _HEADER_LIMITS), rng.randint(1, 3))
        sized = {f: _sized(rng, f, (_LINE_LIMITS | _HEADER_LIMITS)[f]) for f in fields}
        header.update({f: v for f, v in sized.items() if f in _HEADER_LIMITS})
        edit = {f: v for f, v in sized.items() if f in _LINE_LIMITS}
    preview = await B.read(client, co, [(content, name, "application/octet-stream")], bank_id=bk["id"])
    [st] = preview["statements"]
    lines = None if edit is None else [{**line, **edit, "ticked": True} for line in st["lines"]]
    statement = {"header": {**st["header"], **header}, **({} if lines is None else {"lines": lines})}
    if preview.get("column_map"):
        statement_map = {"column_map": preview["column_map"]}
    else:
        statement_map = {}
    over = sorted(f for f, v in sized.items() if len(v) > (_LINE_LIMITS | _HEADER_LIMITS)[f])
    checked = await client.post(f"{B.API}/statement-preview/{preview['job_id']}", headers=co["h"],
                                json={"statements": [statement], **statement_map})
    assert checked.status_code == 200, checked.text
    flagged = sorted(b["params"]["field"]["message_key"] for b in checked.json()["statements"][0]["blocking"]
                     if b["code"] in ("too_long", "header_too_long"))
    assert flagged == sorted(f"field.{f}" if f != "reference" else "label.reference" for f in over)
    r = await client.post(f"{B.API}/import-statement", headers=co["h"], json={
        "job_id": preview["job_id"], "bank_id": bk["id"], "idempotency_key": B.key(),
        "statements": [statement], **statement_map})
    assert r.status_code < 500, r.text
    if over:
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        named = [detail, *detail.get("also", [])]
        # Every blocking reason once, in the order the preview showed them: the first leads.
        assert [(d["message_key"], d["params"]) for d in named] == [
            (f"acct.recon_block_{b['code']}", b["params"]) for b in checked.json()["statements"][0]["blocking"]], detail
        return
    assert r.status_code == 200, r.text
    [session] = r.json()["sessions"]
    assert session["statement_date"] == header["period_end"]
    [line] = [l for l in B.all_lines(await B.groups(client, co, bk["id"], view="all"))
              if l["statement_id"] == session["id"]]
    for f, v in sized.items():
        if f in _LINE_LIMITS:
            assert line[f] == v, (f, line[f])
    assert len(line["counterparty_key"]) <= BankStatementLine.__table__.c.counterparty_key.type.length

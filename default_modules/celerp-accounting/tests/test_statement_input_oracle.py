# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Seeded generated statement input through the real read, preview, import and resolve
routes: every file and every decision is either taken as a reference reading says it should
be, or refused with a keyed reason. Never a server error, never an empty statement read
from a file that held nothing. A generated statement printed with its signs reversed (a card
statement) reads to the same lines, fingerprints, counterparties and duplicates as the same
statement printed in the bank's convention, with running balances or without."""

from __future__ import annotations

import random
from decimal import Decimal, InvalidOperation

import pytest

import bank_statement_helpers as B
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
    existing = {l["fingerprint"]: "Statement 2025-12-31" for l in plain["lines"] if rng.random() < 0.4}
    held = {}
    for line in rng.sample(plain["lines"], rng.randint(0, len(plain["lines"]))):
        bucket = (line["line_date"], line["amount"])
        held[bucket] = held.get(bucket, 0) + 1
    bank = review_statement(header, bank_lines, bank_currency="USD", existing=existing, held_counts=held)
    card = review_statement(header, card_lines, bank_currency="USD", existing=existing, held_counts=held)
    assert (bank["flipped"], bank["blocking"], card["flipped"], card["blocking"]) == (False, [], True, [])
    fields = ("amount", "fingerprint", "counterparty_key", "duplicate", "probable", "ticked")
    assert [[l[f] for f in fields] for l in card["lines"]] == [[l[f] for f in fields] for l in bank["lines"]]

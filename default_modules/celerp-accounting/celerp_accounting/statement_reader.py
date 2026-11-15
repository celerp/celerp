# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Reading a bank statement before it is imported. Pure functions, no database.

``counterparty_key`` names who a line was with, so lines group by counterparty and
direction. ``review_statement`` checks a statement as read: the tie-out (opening plus
lines equals closing, and each running balance follows the one before), the sign
convention, the period, the currency, rows with no readable amount, each line's
fingerprint, and lines already in Celerp.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from celerp.services.field_fingerprints import statement_line, statement_line_fitid, statement_line_ident
from celerp.services.money import round_money

# Words banks print around the counterparty, in every locale a statement arrives in.
# Each maps to the kind a line with nothing else left is grouped under.
_BOILERPLATE = {
    "POS": None, "CARD": None, "REF": None, "PROMPTPAY": "TRANSFER", "TRANSFER": "TRANSFER",
    "ATM": "ATM", "WITHDRAWAL": "WITHDRAWAL", "DEPOSIT": "DEPOSIT", "CHQ": "CHEQUE", "CHEQUE": "CHEQUE",
    "โอนเงิน": "TRANSFER", "รับโอน": "TRANSFER", "ถอนเงินสด": "WITHDRAWAL", "ถอนเงิน": "WITHDRAWAL",
    "ชำระเงิน": None, "ฝากเงิน": "DEPOSIT",
}
_AFTER_TRANSFER = {"TO", "FROM"}
# The kind a sparse line takes when it carried several words: the most telling first.
_KIND_ORDER = ("ATM", "CHEQUE", "WITHDRAWAL", "DEPOSIT", "TRANSFER", "REFERENCE")

_CARD_MASK = re.compile(r"\b\d{4,6}[*X•]{4,}\d{2,4}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_ACCOUNT = re.compile(r"(?<![\d-])\d{9,16}(?![\d-])")
# The pattern the matcher reads a document reference by (INV-0042, PO-001).
_REFERENCE = re.compile(r"^[A-Z]{2,}-\d+(?:-\d+)*$")


def _digits(token: str) -> int:
    return sum(c.isdigit() for c in token)


def counterparty_key(description: str, reference: str | None, amount: Decimal,
                     account: str | None = None) -> tuple[str, str | None]:
    """(key, account) for a statement line. An account number the line names (or the bank's
    own counterparty account field) becomes ``ACCT:<account>``; otherwise the first three
    words left after dates, times, card masks, long numbers, references and bank boilerplate
    are dropped; with nothing left, ``SPARSE:<kind>:<in|out>``."""
    text = " ".join((description or "").upper().split())
    text = _CARD_MASK.sub(" ", text)
    if account is None:
        found = _ACCOUNT.search(text)
        account = found.group(0) if found else None
    if account:
        return f"ACCT:{account}", account
    text = _TIME.sub(" ", _DATE.sub(" ", text)).replace("*", " ")
    ref = (reference or "").strip().upper()
    kinds: set[str] = set()
    kept: list[str] = []
    previous = ""
    for token in text.split():
        word = token.strip(".,;:")
        if word in _BOILERPLATE:
            if _BOILERPLATE[word]:
                kinds.add(_BOILERPLATE[word])
        elif previous == "TRANSFER" and word in _AFTER_TRANSFER:
            pass
        elif (ref and word == ref) or _REFERENCE.match(word):
            kinds.add("REFERENCE")
        elif word.startswith("#") or (_digits(word) and not re.sub(r"[\d\-]", "", word)):
            pass
        elif word:
            kept.append(word)
        previous = word
    if kept:
        return " ".join(kept[:3]), None
    kind = next((k for k in _KIND_ORDER if k in kinds), "OTHER")
    return f"SPARSE:{kind}:{'in' if amount > 0 else 'out'}", None


def _money(value, currency: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return round_money(Decimal(str(value)), currency)
    except (InvalidOperation, ValueError):
        return None


def _block(code: str, text: str, line: int | None = None) -> dict:
    return {"code": code, "line": line, "text": text}


def _tie(opening: Decimal | None, closing: Decimal | None, amounts: list[Decimal],
         balances: list[Decimal | None]) -> tuple[bool | None, int | None]:
    """(ties, first breaking line) for one sign convention. Unknown when a balance is missing."""
    running = opening
    for i, (amount, balance) in enumerate(zip(amounts, balances)):
        if running is not None:
            running += amount
        if balance is not None:
            if running is not None and running != balance:
                return False, i
            running = balance
    if opening is None or closing is None:
        return None, None
    return opening + sum(amounts, Decimal(0)) == closing, None


def review_statement(header: dict, lines: list[dict], *, bank_currency: str, bank_id,
                     existing: dict[str, str] | None = None,
                     held_counts: dict[tuple[str, str], int] | None = None) -> dict:
    """The statement as it would import: each line with its amount at the currency's minor
    unit, fingerprint, counterparty, duplicate and tick; the tie-out; notices; and the
    reasons Import is blocked (each with the line it names)."""
    existing = existing or {}
    currency = (header.get("currency") or bank_currency).upper()
    blocking: list[dict] = []
    notices: list[str] = []
    if currency != bank_currency.upper():
        blocking.append(_block("currency", f"This statement is in {currency}; the bank account is in {bank_currency}."))
    start, end = header.get("period_start"), header.get("period_end")
    out: list[dict] = []
    seen: dict[tuple, int] = {}
    held = dict(held_counts or {})
    for i, raw in enumerate(lines):
        amount = _money(raw.get("amount"), bank_currency)
        balance = _money(raw.get("raw_balance"), bank_currency)
        flags = list(raw.get("flags") or [])
        if amount is None:
            flags = sorted(set(flags) | {"no_amount"})
            blocking.append(_block("no_amount", f"Line {i + 1} has no amount that can be read.", i))
        date = raw.get("line_date") or ""
        if (start and date < start) or (end and date > end):
            blocking.append(_block("outside_period", f"Line {i + 1} ({date}) is outside the statement period.", i))
        out.append({**{k: v for k, v in raw.items() if not k.startswith("_")}, "amount": amount,
                    "raw_balance": balance, "flags": flags})
    amounts = [l["amount"] or Decimal(0) for l in out]
    balances = [l["raw_balance"] for l in out]
    opening = _money(header.get("opening_balance"), bank_currency)
    closing = _money(header.get("closing_balance"), bank_currency)
    if closing is None and balances and balances[-1] is not None:
        closing = balances[-1]

    for line in out:
        amount = line["amount"] or Decimal(0)
        if line.get("fitid") or (line.get("source") == "ofx" and line.get("reference")):
            fp = statement_line_fitid(bank_id, line.get("fitid") or line["reference"])
        else:
            ident = statement_line_ident(line.get("line_date"), amount, line.get("description"),
                                         line.get("reference"), line["raw_balance"])
            n = seen.get(ident, 0)
            seen[ident] = n + 1
            fp = statement_line(bank_id, line.get("line_date"), amount, line.get("description"),
                                line.get("reference"), line["raw_balance"], n)
        key, account = counterparty_key(line.get("description") or "", line.get("reference"), amount,
                                        line.get("counterparty_account"))
        duplicate = existing.get(fp) or False
        probable = False
        bucket = (line.get("line_date"), str(amount))
        if not duplicate and held.get(bucket, 0) > 0:
            held[bucket] -= 1
            probable = True
        line.update(fingerprint=fp, counterparty_key=key, counterparty_account=account,
                    duplicate=duplicate, probable=probable, ticked=not (duplicate or probable))

    counted = [i for i, l in enumerate(out) if not l["duplicate"]]
    ties, first_break = _tie(opening, closing, [amounts[i] for i in counted], [balances[i] for i in counted])
    flipped = False
    if ties is False and first_break is None:
        reversed_ties, _ = _tie(opening, closing, [-amounts[i] for i in counted], [None for _ in counted])
        if reversed_ties:
            flipped, ties = True, True
            for line in out:
                if line["amount"] is not None:
                    line["amount"] = -line["amount"]
            notices.append("The amounts on this statement were reversed to match its balances "
                           "(card statements print spending as positive).")
    if first_break is not None:
        first_break = counted[first_break]
        blocking.append(_block("tie_break", f"The running balance stops adding up at line {first_break + 1}.",
                               first_break))
    elif ties is False:
        blocking.append(_block("tie_out", "Opening balance plus the lines does not equal the closing balance."))
    elif ties is None:
        blocking.append(_block("balances_missing", "Enter the opening and closing balance from the statement."))

    for line in out:
        for field in ("amount", "raw_balance"):
            if line[field] is not None:
                line[field] = str(line[field])
    return {
        "header": {**header, "currency": currency,
                   "closing_balance": None if closing is None else str(closing)},
        "lines": out,
        "tie": {"ok": ties, "first_break": first_break},
        "flipped": flipped,
        "notices": notices,
        "blocking": blocking,
    }

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Reading a bank statement before it is imported. Pure functions, no database.

``counterparty_key`` names who a line was with, so lines group by counterparty and
direction. ``review_statement`` checks a statement as read: the tie-out (opening plus
lines equals closing, and each running balance follows the one before), the sign
convention, the period, the currency, rows with no readable amount, each line's
fingerprint, the lines already in Celerp and the lines that may repeat one.
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
    from celerp_accounting.statement_lines import MONEY_LIMIT
    try:
        amount = Decimal(str(value))
        # NaN and infinity parse as Decimals but are no amount: unread, never compared or rounded.
        if not amount.is_finite():
            return None
        amount = round_money(amount, currency)
    except (InvalidOperation, ValueError):
        return None
    return amount if abs(amount) < MONEY_LIMIT else None


def block_refusal(code: str, /, **params) -> dict:
    """The refusal of a reason Import is blocked, keyed ``acct.recon_block_<code>``."""
    from celerp.accounting_roles import catalog_refusal

    return catalog_refusal(f"acct.recon_block_{code}", **params)


def _block(code: str, line: int | None = None, **params) -> dict:
    """A reason Import is blocked: its code, the line it names, the parameters of its
    refusal and the refusal's text in the user's language."""
    from ui.i18n import refusal_text

    return {"code": code, "line": line, "params": params, "text": refusal_text(block_refusal(code, **params))}


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


def _external_id(line: dict) -> str | None:
    """The id the bank gave the line (OFX FITID), if any: sent as ``fitid`` or read from an
    OFX file as the line's reference."""
    fitid = line.get("fitid") or (line.get("reference") if line.get("source") == "ofx" else None)
    return (str(fitid).strip() or None) if fitid else None


def _evidence(line: dict) -> tuple:
    """(external id, what two readings of one transaction share, printed reference, running
    balance): the reference is the printed one, not the external id an OFX file reads as it."""
    fitid = line.get("fitid")
    reference = line.get("reference")
    date, amount, text, ref, balance = statement_line_ident(
        line.get("line_date"), line["amount"] or Decimal(0), line.get("description"),
        None if fitid and (reference or "").strip() == fitid else reference, line.get("raw_balance"))
    return fitid, (date, amount, text), ref, balance


def _relation(new: tuple, held: tuple) -> str | None:
    """How a line read now stands to a line the bank already holds. ``confirmed``: evidence
    names one transaction (the same external id, or the same line with the same running
    balance). ``None``: they are distinct (another external id, date, amount, description,
    printed reference or running balance). Otherwise ``ambiguous``: a repeat the data cannot
    tell from the same line read twice."""
    fitid, base, ref, balance = new
    h_fitid, h_base, h_ref, h_balance = held
    if fitid and h_fitid:
        return "confirmed" if fitid == h_fitid else None
    if base != h_base or (ref and h_ref and ref != h_ref) or (balance and h_balance and balance != h_balance):
        return None
    return "confirmed" if balance and balance == h_balance and ref == h_ref else "ambiguous"


def review_statement(header: dict, lines: list[dict], *, bank_currency: str,
                     known: list[dict] | None = None,
                     held_counts: dict[tuple[str, str], int] | None = None) -> dict:
    """The statement as it would import: each line with its amount at the currency's minor
    unit, fingerprint, counterparty, duplicate or possible repeat and tick; the tie-out;
    notices; and the reasons Import is blocked (each with the line it names). ``known`` is the
    bank's lines (as this returns them, each with the date of its ``statement``)."""
    currency = (header.get("currency") or bank_currency).upper()
    blocking: list[dict] = []
    notices: list[str] = []
    if currency != bank_currency.upper():
        blocking.append(_block("currency", currency=currency, bank_currency=bank_currency))
    start, end = header.get("period_start"), header.get("period_end")
    out: list[dict] = []
    seen: dict[tuple, int] = {}
    for i, raw in enumerate(lines):
        amount = _money(raw.get("amount"), bank_currency)
        balance = _money(raw.get("raw_balance"), bank_currency)
        flags = list(raw.get("flags") or [])
        if amount is None:
            flags = sorted(set(flags) | {"no_amount"})
            blocking.append(_block("no_amount", i, n=i + 1))
        date = raw.get("line_date") or ""
        if (start and date < start) or (end and date > end):
            blocking.append(_block("outside_period", i, n=i + 1, date=date))
        out.append({**{k: v for k, v in raw.items() if not k.startswith("_")}, "amount": amount,
                    "raw_balance": balance, "flags": flags})
    amounts = [l["amount"] or Decimal(0) for l in out]
    balances = [l["raw_balance"] for l in out]
    opening = _money(header.get("opening_balance"), bank_currency)
    closing = _money(header.get("closing_balance"), bank_currency)
    if closing is None and balances and balances[-1] is not None:
        closing = balances[-1]

    # The sign convention comes first, from the opening, the closing and every running balance:
    # the statement ties out as read, every line in it, those already imported too (its balances
    # count them), or only with every amount reversed (card statements print spending as
    # positive). Neither ties: the break as read blocks. Only then is each line's identity read,
    # from its amount in the bank's convention, and the duplicates left out of the import.
    ties, first_break = _tie(opening, closing, amounts, balances)
    flipped = False
    if ties is False and _tie(opening, closing, [-a for a in amounts], balances)[0]:
        flipped, ties, first_break = True, True, None
        for line in out:
            if line["amount"] is not None:
                line["amount"] = -line["amount"]
        notices.append("The amounts on this statement were reversed to match its balances "
                       "(card statements print spending as positive).")

    held = [(h, _evidence(h)) for h in known or []]
    taken = {h["fingerprint"] for h, _ in held}
    by_base: dict[tuple, list[int]] = {}
    by_fitid: dict[str, list[int]] = {}
    for i, (_, ev) in enumerate(held):
        by_base.setdefault(ev[1], []).append(i)
        if ev[0]:
            by_fitid.setdefault(ev[0], []).append(i)
    for line in out:
        line.update(fitid=_external_id(line), duplicate=False, repeats=None)
    evidence = [_evidence(line) for line in out]
    claimed: set[int] = set()
    # Confirmed duplicates first, so a repeat never takes the held line another line names;
    # then each held line a repeat may be is offered once, so k identical lines against m held
    # leave min(k, m) for review and import the rest.
    for kind in ("confirmed", "ambiguous"):
        for line, ev in zip(out, evidence):
            if line["duplicate"] or line["repeats"]:
                continue
            pool = by_fitid.get(ev[0], []) + by_base.get(ev[1], []) if kind == "confirmed" else by_base.get(ev[1], [])
            hit = next((i for i in pool if i not in claimed and _relation(ev, held[i][1]) == kind), None)
            if hit is None:
                continue
            claimed.add(hit)
            statement = held[hit][0].get("statement") or ""
            if kind == "confirmed":
                from ui.i18n import t
                line["duplicate"] = t("acct.recon_duplicate_of", date=statement)
            else:
                line["repeats"] = statement

    bucket_counts = dict(held_counts or {})
    for line in out:
        amount = line["amount"] or Decimal(0)
        fitid = line["fitid"]
        ident = ("FITID", fitid) if fitid else statement_line_ident(
            line.get("line_date"), amount, line.get("description"), line.get("reference"), line["raw_balance"])
        n = seen.get(ident, 0)
        seen[ident] = n + 1
        make = (lambda k: statement_line_fitid(fitid, k)) if fitid else (lambda k: statement_line(
            line.get("line_date"), amount, line.get("description"), line.get("reference"), line["raw_balance"], k))
        fp = make(n)
        # A line that imports takes the first number of its kind no held or earlier line has,
        # so a repeat ticked in never collides with the line it repeats.
        while not line["duplicate"] and fp in taken:
            n += 1
            fp = make(n)
        taken.add(fp)
        key, account = counterparty_key(line.get("description") or "", line.get("reference"), amount,
                                        line.get("counterparty_account"))
        probable = bool(line["repeats"])
        bucket = (line.get("line_date"), str(amount))
        if not (line["duplicate"] or probable) and bucket_counts.get(bucket, 0) > 0:
            bucket_counts[bucket] -= 1
            probable = True
        line.update(fingerprint=fp, counterparty_key=key, counterparty_account=account,
                    probable=probable, ticked=not (line["duplicate"] or probable))

    if first_break is not None:
        blocking.append(_block("tie_break", first_break, n=first_break + 1))
    elif ties is False:
        blocking.append(_block("tie_out"))
    elif ties is None:
        blocking.append(_block("balances_missing"))

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

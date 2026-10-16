# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The general-ledger effect of a decoded Manager book, and what a migration imports.

Postings are computed from the source records alone, so the same numbers serve
both the CIF conversion and the independent source-side reconciliation
expectations. Amounts are base currency, debit positive, each record converted at
its own rate; `native` holds the same amount in the currency of the account or
contact it posts to. A receipt or payment relieves its document at the
document's rate and moves the bank at its own, and a note relieves its invoice
or bill at that document's rate, the difference on the exchange gains and
losses account: the rule Celerp applies itself when a payment or note is
applied, so the source figures and the imported ones agree.

Full history imports every record. Cutover partitions the records at the cutover
date: those dated on or before it are pre-cutover, the rest are imported as they
are. Of the pre-cutover records it imports the documents still open at the
cutover date or settled by a later record, the notes applied to them, the
settlement portions allocated to them and the quotes and orders not yet
converted or cancelled; one opening journal and an opening stock
position per item carry the rest of the pre-cutover history.

Stock moves on the physical record: a goods receipt or delivery note on its own date
and quantity, or an invoice or bill flagged to move its own stock. An invoice or bill
with neither moves no stock. An inventory item sold posts the cost of sales Manager
books from what it owns on the invoice date, bills counted whether or not their goods
have arrived; the goods delivered for it leave stock at that same cost.

Starting balances post as one journal the day before the first dated record, offset to
retained earnings, with the stock the business started with as an opening position per
item; a cutover folds both into its opening.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.adapters.manager_io.book import (
    AP, AR, FX_GAIN, INVENTORY, INVENTORY_PURCHASES, RETAINED, STARTING, Book, Document, Journal, JournalLine,
    Movement, Settlement, line_account,
)
from celerp.importers.schema import CIFMode
from celerp.services.money import round_money
from ui.i18n import t

ZERO = Decimal(0)
SALES_TYPES = ("SalesInvoice", "CreditNote")
# Sign of the customer or supplier balance posting: an invoice and a debit note raise a debit there.
PARTY_SIGN = {"SalesInvoice": 1, "CreditNote": -1, "PurchaseInvoice": -1, "DebitNote": 1}


@dataclass(frozen=True)
class Posting:
    record: str
    part: str                                  # document, settlement, fx, fallback, transfer, journal, opening
    date: date
    account: str
    amount: Decimal                            # debit positive
    contact: str | None = None
    description: str | None = None
    document: str | None = None                # the document a settlement, or a note's exchange difference, settles
    native: Decimal = ZERO                     # the amount in the currency of the account or contact posted to


@dataclass(frozen=True)
class DocumentState:
    amount_paid: Decimal
    amount_outstanding: Decimal
    status: str


@dataclass
class Ledger:
    book: Book
    cutover: date | None                                           # None: full history
    postings: list[Posting] = field(default_factory=list)          # every posting in the book
    states: dict[str, DocumentState] = field(default_factory=dict)
    documents: list[str] = field(default_factory=list)             # imported document keys
    orders: list[str] = field(default_factory=list)                # imported quote and order keys
    allocations: dict[str, list[tuple[str, Decimal]]] = field(default_factory=dict)  # imported
    settlement_amounts: dict[str, Decimal] = field(default_factory=dict)             # imported
    opening: list[Posting] = field(default_factory=list)
    opening_stock: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    starting_stock: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)   # imported, full history
    moves: list[Movement] = field(default_factory=list)           # imported stock movements
    unrealized: dict | None = None                                 # foreign balances revalued, reported only

    @property
    def realized(self) -> Decimal:
        """Exchange differences booked on settlements and notes."""
        return sum((p.amount for p in self.postings if p.part == "fx" and p.account == FX_GAIN), ZERO)

    @property
    def opening_key(self) -> str:
        return f"opening:{self.cutover.isoformat()}" if self.cutover else "opening"

    def balances(self, postings: list[Posting] | None = None) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for p in self.postings if postings is None else postings:
            out[p.account] += p.amount
        return dict(out)

    def imported_postings(self) -> list[Posting]:
        """Postings the imported records carry, excluding the opening journal."""
        docs = set(self.documents)
        if self.cutover is None:
            return list(self.postings)
        return [p for p in self.postings if p.date > self.cutover
                or (p.part in ("document", "fx") and p.record in docs)
                or (p.part in ("settlement", "fx") and p.document in docs)]


def _document_postings(book: Book, doc: Document) -> list[Posting]:
    party_sign = PARTY_SIGN[doc.source_type]
    party = AR if doc.source_type in SALES_TYPES else AP
    out: list[Posting] = []
    for line in doc.lines:
        out.append(Posting(doc.key, "document", doc.date, line_account(doc, line), -party_sign * line.base_net,
                           description=line.description, native=-party_sign * line.net))
        if line.cost:
            out.append(Posting(doc.key, "document", doc.date, INVENTORY_PURCHASES, line.cost, native=line.cost))
            out.append(Posting(doc.key, "document", doc.date, INVENTORY, -line.cost, native=-line.cost))
        if line.tax:
            tax_account = book.tax_codes[line.tax_code].account
            out.append(Posting(doc.key, "document", doc.date, tax_account, -party_sign * line.base_tax,
                               native=-party_sign * line.tax))
    out.append(Posting(doc.key, "document", doc.date, party, party_sign * doc.base_total, contact=doc.contact,
                       native=party_sign * doc.total))
    if doc.applies_to:
        out += _note_difference(book, doc)
    return out


def _note_difference(book: Book, note: Document) -> list[Posting]:
    """A note relieves its invoice or bill at that document's rate; the difference from the note's
    own base total is an exchange difference on the customer or supplier balance."""
    target = book.documents[note.applies_to]
    relieved = round_money(note.total * target.rate, book.base_code)
    difference = -PARTY_SIGN[note.source_type] * (note.base_total - relieved)
    if not difference:
        return []
    party = AR if note.source_type in SALES_TYPES else AP
    return [Posting(note.key, "fx", note.date, party, difference, contact=note.contact, document=target.key),
            Posting(note.key, "fx", note.date, FX_GAIN, -difference, document=target.key)]


def _settlement_postings(book: Book, s: Settlement) -> list[Posting]:
    """The bank moves at the settlement's rate and the document is relieved at its own."""
    bank_sign = 1 if s.source_type == "Receipt" else -1
    party = AR if s.source_type == "Receipt" else AP
    out: list[Posting] = []
    for line in s.party_lines:
        relieved = round_money(line.net * book.documents[line.document].rate, book.base_code)
        out.append(Posting(s.key, "settlement", s.date, party, -bank_sign * relieved, contact=line.contact,
                           description=line.description, document=line.document, native=-bank_sign * line.net))
        out.append(Posting(s.key, "settlement", s.date, s.bank, bank_sign * line.base_net,
                           description=line.description, document=line.document, native=bank_sign * line.net))
        if line.base_net != relieved:
            out.append(Posting(s.key, "fx", s.date, FX_GAIN, -bank_sign * (line.base_net - relieved),
                               document=line.document))
    other: list[Posting] = []
    for line in s.other_lines:
        other.append(Posting(s.key, "fallback", s.date, line.account, -bank_sign * line.base_net,
                             contact=line.contact, description=line.description, native=-bank_sign * line.net))
        if line.tax:
            other.append(Posting(s.key, "fallback", s.date, book.tax_codes[line.tax_code].account,
                                 -bank_sign * line.base_tax, native=-bank_sign * line.tax))
    if other:
        native = bank_sign * sum((ln.net + ln.tax for ln in s.other_lines), ZERO)
        other.append(Posting(s.key, "fallback", s.date, s.bank, -sum((p.amount for p in other), ZERO),
                             description=s.description, native=native))
    return out + other


def _postings(book: Book, keys: set[str]) -> list[Posting]:
    out: list[Posting] = []
    for key in sorted(keys, key=lambda k: _record_date(book, k)):
        if key in book.documents:
            out += _document_postings(book, book.documents[key])
        elif key in book.settlements:
            out += _settlement_postings(book, book.settlements[key])
        elif key in book.transfers:
            t = book.transfers[key]
            out.append(Posting(key, "transfer", t.date, t.from_bank, -t.base, description=t.description,
                               native=-t.amount))
            out.append(Posting(key, "transfer", t.date, t.to_bank, t.base, description=t.description,
                               native=t.to_amount))
        else:
            j = book.journals[key]
            for line in j.lines:
                out.append(Posting(key, "journal", j.date, line.account, line.base_debit - line.base_credit,
                                   contact=line.contact, description=line.description,
                                   native=line.debit - line.credit))
    return out


def _record_date(book: Book, key: str) -> tuple[date, str]:
    for group in (book.documents, book.settlements, book.transfers, book.journals):
        if key in group:
            return group[key].date, key
    raise KeyError(key)


def _states(book: Book, keys: set[str]) -> dict[str, DocumentState]:
    """Paid and outstanding per document from the settlements and notes in scope."""
    paid: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for key in keys:
        if key in book.settlements:
            for line in book.settlements[key].party_lines:
                paid[line.document] += line.net
        elif key in book.documents and book.documents[key].applies_to:
            note = book.documents[key]
            paid[note.applies_to] += note.total
            paid[key] += note.total
    for key, amount in book.starting_paid().items():
        paid[key] += amount
    states = {}
    for key in keys & set(book.documents):
        total = book.documents[key].total
        outstanding = total - paid[key]
        states[key] = DocumentState(paid[key], outstanding, "paid" if outstanding <= 0 else "awaiting_payment")
    return states


def stock(book: Book, moves: list[Movement]) -> dict[str, tuple[Decimal, Decimal]]:
    """Quantity and base value held per item after the stock the business started with and
    the given stock movements."""
    held = book.starting_stock()
    for movement in moves:
        for line in movement.lines:
            qty, value = held.get(line.item, (ZERO, ZERO))
            held[line.item] = (qty + line.quantity, value + line.value)
    return held


def holding_stock(book: Book) -> set[str]:
    """Items still holding quantity or value after every stock movement. An inactive item
    among them is imported as available: archiving it would hide its stock from the
    inventory valuation."""
    return {item for item, (qty, value) in stock(book, book.moves).items() if qty or value}


def _check_cutover(book: Book, cutover: date | None) -> date:
    if cutover is None:
        raise ScanError(t("migration.err_choose_cutover"))
    dated = book.dated_records()
    if dated and cutover < dated[0][0]:
        raise ScanError(t("migration.err_cutover_before_first"))
    return cutover


def foreign_holders(book: Book) -> dict[str, str]:
    """Bank accounts and contacts kept in a foreign currency, with that currency's key."""
    holders = {a.key: a.currency for a in book.accounts.values() if a.control == "bank" and book.is_foreign(a.currency)}
    holders |= {c.key: c.currency for c in book.contacts.values() if book.is_foreign(c.currency)}
    return holders


def posting_holder(p: Posting, holders: dict[str, str]) -> str | None:
    if p.account in holders:
        return p.account
    return p.contact if p.account in (AR, AP) and p.contact in holders else None


def _unrealized(book: Book, postings: list[Posting]) -> dict | None:
    """Foreign bank and contact balances revalued at the rate table's latest rate per currency.
    Reported, not posted: Manager computes it for its reports only."""
    holders = foreign_holders(book)
    if not holders:
        return None
    latest: dict[str, tuple] = {}
    for rate in sorted((r for r in book.rates if not book.is_blocked(r.key)), key=lambda r: (r.effective, r.key)):
        latest[rate.currency] = (rate.effective, rate.rate)
    base: dict[str, Decimal] = defaultdict(lambda: ZERO)
    native: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in postings:
        holder = posting_holder(p, holders)
        if holder:
            base[holder] += p.amount
            native[holder] += p.native
    entries = {}
    for holder, currency in sorted(holders.items()):
        if currency in latest:
            difference = round_money(native[holder] * latest[currency][1], book.base_code) - base[holder]
            if difference:
                entries[holder] = difference
    as_of = max((day for day, _ in latest.values()), default=None)
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "rates": {book.currencies[c].code: format(r.normalize(), "f") for c, (_, r) in sorted(latest.items())},
        "entries": {k: str(v) for k, v in entries.items()},
        "net": str(sum(entries.values(), ZERO)),
    }


def _depends_on(book: Book, key: str) -> set[str]:
    """The documents a record acts on in Celerp: the invoices and bills a settlement settles,
    the invoice a note applies to."""
    if key in book.settlements:
        return {ln.document for ln in book.settlements[key].party_lines}
    if key in book.documents and book.documents[key].applies_to:
        return {book.documents[key].applies_to}
    return set()


def _carried(book: Book, records: set[str], cutover: date) -> set[str]:
    """Pre-cutover documents imported as themselves: those open at the cutover date, those
    a later record depends on (a settlement, a note, or goods moved for them), and the
    notes applied to them."""
    pre = {k for k in records if _record_date(book, k)[0] <= cutover}
    carried = {k for k, state in _states(book, pre).items() if state.amount_outstanding != 0}
    for key in records - pre:
        carried |= _depends_on(book, key)
    carried |= {m.document for m in book.moves if m.date > cutover}
    carried &= pre
    return carried | {k for k in pre if k in book.documents and book.documents[k].applies_to in carried}


def _install_starting(book: Book, cutover: date | None) -> None:
    """Starting balances post as one journal, offset to retained earnings the way Manager's
    own balance sheet does. It stands the day before the first dated record, or with none on a
    cutover date. With no dated record Full history has no day to start on."""
    lines = [line for sb in book.live_starting() for line in sb.lines]
    if not lines and not book.starting_stock():
        return
    day = book.starting_date or cutover
    if day is None:
        raise ScanError(t("migration.err_starting_balances_undated"))
    lines.append((RETAINED, -sum((amount for _, amount, _ in lines), ZERO), None))
    book.journals[STARTING] = Journal(STARTING, day, None, "Starting balances.", None, [
        JournalLine(account, contact, max(amount, ZERO), max(-amount, ZERO), None, max(amount, ZERO),
                    max(-amount, ZERO)) for account, amount, contact in lines if amount], Decimal(1))
    book.names[STARTING] = "StartingBalances"


def build_ledger(book: Book, decisions: MigrationDecisions) -> Ledger:
    """The postings and imported records for one migration. The book must carry no blockers."""
    book.journals.pop(STARTING, None)                              # installed per migration, for its mode
    cutover = _check_cutover(book, decisions.cutover_date) if decisions.mode == CIFMode.CUTOVER else None
    _install_starting(book, cutover)
    records = {k for _, k in book.dated_records()}
    ledger = Ledger(book, cutover)
    ledger.postings = _postings(book, records)
    ledger.states = _states(book, records)

    documents = sorted((k for k in records if k in book.documents), key=lambda k: _record_date(book, k))
    if cutover is not None:
        carried = _carried(book, records, cutover)
        documents = [k for k in documents if k in carried or book.documents[k].date > cutover]
    ledger.documents = documents
    # A quote or order before the cutover is carried while it is still open: not cancelled and not yet converted.
    ledger.orders = [o.key for o in sorted(book.orders.values(), key=lambda o: (o.date, o.key))
                     if cutover is None or o.date > cutover or not (o.cancelled or o.converted_to)]
    imported = set(documents)
    for key in sorted((k for k in records if k in book.settlements), key=lambda k: _record_date(book, k)):
        s = book.settlements[key]
        lines = s.party_lines
        if cutover is not None and s.date <= cutover:
            lines = [ln for ln in lines if ln.document in imported]
        if lines:
            ledger.settlement_amounts[key] = sum((ln.net for ln in lines), ZERO)
            ledger.allocations[key] = [(ln.document, ln.net) for ln in lines]

    if cutover is not None:
        imported_postings = ledger.imported_postings()
        balance: dict[tuple[str, str | None], Decimal] = defaultdict(lambda: ZERO)
        native: dict[tuple[str, str | None], Decimal] = defaultdict(lambda: ZERO)
        for sign, postings in ((1, ledger.postings), (-1, imported_postings)):
            for p in postings:
                balance[(p.account, p.contact)] += sign * p.amount
                native[(p.account, p.contact)] += sign * p.native
        holders = foreign_holders(book)
        ledger.opening = [Posting(ledger.opening_key, "opening", cutover, account, amount, contact=contact,
                                  native=native[(account, contact)] if (contact or account) in holders else amount)
                          for (account, contact), amount in sorted(balance.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
                          if amount]
        before = stock(book, [m for m in book.moves if m.date <= cutover])
        ledger.opening_stock = {item: held for item, held in sorted(before.items()) if held[0] or held[1]}
        ledger.moves = [m for m in book.moves if m.date > cutover]
    else:
        ledger.moves = list(book.moves)
        ledger.starting_stock = {item: held for item, held in sorted(book.starting_stock().items())}
    ledger.unrealized = _unrealized(book, ledger.postings)
    return ledger

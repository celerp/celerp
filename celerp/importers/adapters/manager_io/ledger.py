# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The general-ledger effect of a decoded Manager book, and what a migration imports.

Postings are computed from the source records alone, so the same numbers serve
both the CIF conversion and the independent source-side reconciliation
expectations. Every imported record is in the base currency; amounts are debit
positive.

Full history imports every record. Cutover partitions the records at the cutover
date: those dated on or before it are pre-cutover, the rest are imported as they
are. Of the pre-cutover records it imports the documents still open at the
cutover date or settled by a later record, the notes applied to them and the
settlement portions allocated to them; one opening journal and an opening stock
position per item carry the rest of the pre-cutover history.

Stock moves on the physical record: a goods receipt or delivery note on its own date
and quantity, or an invoice or bill flagged to move its own stock. An invoice or bill
with neither moves no stock. An inventory item sold posts its cost of sales at the
unit cost set for it on the invoice date.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.adapters.manager_io.book import (
    AP, AR, INVENTORY, INVENTORY_PURCHASES, Book, Document, Movement, Settlement, line_account,
)
from celerp.importers.schema import CIFMode

ZERO = Decimal(0)
SALES_TYPES = ("SalesInvoice", "CreditNote")
# Sign of the customer or supplier balance posting: an invoice and a debit note raise a debit there.
PARTY_SIGN = {"SalesInvoice": 1, "CreditNote": -1, "PurchaseInvoice": -1, "DebitNote": 1}


@dataclass(frozen=True)
class Posting:
    record: str
    part: str                                  # document, settlement, fallback, transfer, journal, opening
    date: date
    account: str
    amount: Decimal                            # debit positive
    contact: str | None = None
    description: str | None = None
    document: str | None = None                # the document a settlement posting settles


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
    allocations: dict[str, list[tuple[str, Decimal]]] = field(default_factory=dict)  # imported
    settlement_amounts: dict[str, Decimal] = field(default_factory=dict)             # imported
    opening: list[Posting] = field(default_factory=list)
    opening_stock: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    moves: list[Movement] = field(default_factory=list)           # imported stock movements

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
        return [p for p in self.postings if p.date > self.cutover or (p.part == "document" and p.record in docs)
                or (p.part == "settlement" and p.document in docs)]


def _document_postings(book: Book, doc: Document) -> list[Posting]:
    party_sign = PARTY_SIGN[doc.source_type]
    party = AR if doc.source_type in SALES_TYPES else AP
    out: list[Posting] = []
    for line in doc.lines:
        out.append(Posting(doc.key, "document", doc.date, line_account(doc, line), -party_sign * line.net,
                           description=line.description))
        if line.cost:
            out.append(Posting(doc.key, "document", doc.date, INVENTORY_PURCHASES, line.cost))
            out.append(Posting(doc.key, "document", doc.date, INVENTORY, -line.cost))
        if line.tax:
            tax_account = book.tax_codes[line.tax_code].account
            out.append(Posting(doc.key, "document", doc.date, tax_account, -party_sign * line.tax))
    out.append(Posting(doc.key, "document", doc.date, party, party_sign * doc.total, contact=doc.contact))
    return out


def _settlement_postings(book: Book, s: Settlement) -> list[Posting]:
    bank_sign = 1 if s.source_type == "Receipt" else -1
    party = AR if s.source_type == "Receipt" else AP
    out: list[Posting] = []
    for line in s.party_lines:
        out.append(Posting(s.key, "settlement", s.date, party, -bank_sign * line.net,
                           contact=line.contact, description=line.description, document=line.document))
        out.append(Posting(s.key, "settlement", s.date, s.bank, bank_sign * line.net,
                           description=line.description, document=line.document))
    other: list[Posting] = []
    for line in s.other_lines:
        other.append(Posting(s.key, "fallback", s.date, line.account, -bank_sign * line.net,
                             contact=line.contact, description=line.description))
        if line.tax:
            other.append(Posting(s.key, "fallback", s.date, book.tax_codes[line.tax_code].account,
                                 -bank_sign * line.tax))
    if other:
        other.append(Posting(s.key, "fallback", s.date, s.bank, -sum((p.amount for p in other), ZERO),
                             description=s.description))
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
            out.append(Posting(key, "transfer", t.date, t.from_bank, -t.amount, description=t.description))
            out.append(Posting(key, "transfer", t.date, t.to_bank, t.amount, description=t.description))
        else:
            j = book.journals[key]
            for line in j.lines:
                amount = line.debit - line.credit
                out.append(Posting(key, "journal", j.date, line.account, amount, contact=line.contact,
                                   description=line.description))
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
    states = {}
    for key in keys & set(book.documents):
        total = book.documents[key].total
        outstanding = total - paid[key]
        states[key] = DocumentState(paid[key], outstanding, "paid" if outstanding <= 0 else "awaiting_payment")
    return states


def stock(moves: list[Movement]) -> dict[str, tuple[Decimal, Decimal]]:
    """Quantity and base value held per item after the given stock movements."""
    held: dict[str, tuple[Decimal, Decimal]] = {}
    for movement in moves:
        for line in movement.lines:
            qty, value = held.get(line.item, (ZERO, ZERO))
            held[line.item] = (qty + line.quantity, value + line.value)
    return held


def holding_stock(book: Book) -> set[str]:
    """Items still holding quantity or value after every stock movement. An inactive item
    among them is imported as available: archiving it would hide its stock from the
    inventory valuation."""
    return {item for item, (qty, value) in stock(book.moves).items() if qty or value}


def _check_cutover(book: Book, cutover: date | None) -> date:
    if cutover is None:
        raise ScanError("Choose a cutover date.")
    dated = book.dated_records()
    if dated and cutover < dated[0][0]:
        raise ScanError("The cutover date is before the first transaction in this business file.")
    if any(book.is_foreign(a.currency) for a in book.accounts.values()) or any(
            book.is_foreign(c.currency) for c in book.contacts.values()):
        raise ScanError("Cutover is not available yet for a business with foreign currency balances. "
                        "Use Full history.")
    return cutover


def _carried(book: Book, records: set[str], cutover: date) -> set[str]:
    """Pre-cutover documents imported as themselves: those open at the cutover date or
    settled by a later record, and the notes applied to them."""
    pre = {k for k in records if _record_date(book, k)[0] <= cutover}
    carried = {k for k, state in _states(book, pre).items() if state.amount_outstanding != 0}
    for key in records - pre:
        if key in book.settlements:
            carried |= {ln.document for ln in book.settlements[key].party_lines}
        elif key in book.documents and book.documents[key].applies_to:
            carried.add(book.documents[key].applies_to)
    carried &= pre
    return carried | {k for k in pre if k in book.documents and book.documents[k].applies_to in carried}


def build_ledger(book: Book, decisions: MigrationDecisions) -> Ledger:
    """The postings and imported records for one migration. The book must carry no blockers."""
    records = {k for _, k in book.dated_records()}
    cutover = _check_cutover(book, decisions.cutover_date) if decisions.mode == CIFMode.CUTOVER else None
    ledger = Ledger(book, cutover)
    ledger.postings = _postings(book, records)
    ledger.states = _states(book, records)

    documents = sorted((k for k in records if k in book.documents), key=lambda k: _record_date(book, k))
    if cutover is not None:
        carried = _carried(book, records, cutover)
        documents = [k for k in documents if k in carried or book.documents[k].date > cutover]
    ledger.documents = documents
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
        for p in ledger.postings:
            balance[(p.account, p.contact)] += p.amount
        for p in imported_postings:
            balance[(p.account, p.contact)] -= p.amount
        ledger.opening = [Posting(ledger.opening_key, "opening", cutover, account, amount, contact=contact)
                          for (account, contact), amount in sorted(balance.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
                          if amount]
        before = stock([m for m in book.moves if m.date <= cutover])
        ledger.opening_stock = {item: held for item, held in sorted(before.items()) if held[0] or held[1]}
        ledger.moves = [m for m in book.moves if m.date > cutover]
    else:
        ledger.moves = list(book.moves)
    return ledger

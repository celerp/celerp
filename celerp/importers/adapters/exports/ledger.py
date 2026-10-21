# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""What an export set's general ledger becomes in Celerp, and what a migration imports.

The ledger is the source's own: every posting here is a line of its General Ledger
export, so the books Celerp ends with are the books the source printed. Each journal
is carried as the native Celerp record it describes, where one fits:

- a journal posting an invoice, bill or credit note found in the invoices and bills
  export is that document;
- a receipt or payment journal settling one such document from one bank account is a
  receipt or payment allocated to it;
- a transfer journal between two bank accounts is a bank transfer;
- every other journal (spend and receive money, overpayments, manual journals) is a
  journal entry.

Celerp books a document and its settlement by its own rules: lines converted at the
document's rate, the customer or supplier balance relieved at the document's rate,
the bank moved at the settlement's, the difference on the exchange account. Where the
source booked a cent or an exchange difference differently, the difference between
the source's lines and what Celerp books is carried as a journal fallback on the same
record, so every account and contact ends at the source's figure.

Cutover partitions the records at the cutover date as the Manager adapter does: the
documents still open then or settled later are carried with their notes and
settlements, and one opening journal carries the rest.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.adapters.exports.book import PARTY_SIGN, ZERO, Book, Document, GLJournal, GLLine
from celerp.importers.schema import CIFMode
from celerp.services.money import checked_exchange_rate, round_money, split_to_base
from ui.i18n import t

# Journal kinds, as each source reader names them on `GLJournal.kind`.
DOCUMENT_KINDS = ("receivable invoice", "receivable credit note", "payable invoice", "payable credit note")
SETTLEMENT_KINDS = {"receivable payment": "receipt", "payable payment": "payment"}
TRANSFER_KINDS = ("bank transfer",)


@dataclass(frozen=True)
class Posting:
    record: str
    part: str                                  # document, settlement, fx, fallback, transfer, journal, opening
    date: date
    account: str
    amount: Decimal                            # base, debit positive
    contact: str | None = None
    description: str | None = None
    document: str | None = None                # the document the posting books, settles or applies to
    native: Decimal = ZERO                     # the amount in the record's currency


@dataclass(frozen=True)
class Settlement:
    key: str
    kind: str                                  # receipt or payment
    date: date
    contact: str
    bank: str
    currency: str
    rate: Decimal                              # base per settlement unit
    amount: Decimal                            # in the document's currency
    document: str
    reference: str
    source: str


@dataclass(frozen=True)
class Transfer:
    key: str
    date: date
    from_bank: str
    to_bank: str
    amount: Decimal                            # leaving, in the paying account's currency
    to_amount: Decimal                         # arriving, in the receiving account's currency
    base: Decimal
    reference: str
    description: str


@dataclass(frozen=True)
class DocumentState:
    amount_paid: Decimal
    amount_outstanding: Decimal
    status: str


@dataclass
class Ledger:
    book: Book
    cutover: date | None                                           # None: full history
    postings: list[Posting] = field(default_factory=list)          # every posting in the ledger export
    settlements: dict[str, Settlement] = field(default_factory=dict)
    transfers: dict[str, Transfer] = field(default_factory=dict)
    journals: dict[str, GLJournal] = field(default_factory=dict)   # journals carried as journal entries
    states: dict[str, DocumentState] = field(default_factory=dict)
    documents: list[str] = field(default_factory=list)             # imported document keys
    opening: list[Posting] = field(default_factory=list)

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
        if self.cutover is None:
            return list(self.postings)
        docs = set(self.documents)
        return [p for p in self.postings if p.date > self.cutover or p.document in docs]

    def imported_settlements(self) -> list[Settlement]:
        docs = set(self.documents)
        return [s for s in self.settlements.values() if s.document in docs]


def find_document(book: Book, sales: bool, number: str, contact: str) -> Document | None:
    side = "S" if sales else "P"
    return book.documents.get(f"{side}:{number}") or book.documents.get(f"{side}:{number}|{contact}")


def _party(book: Book, doc_type: str) -> set[str]:
    return set(book.control("receivable" if doc_type in ("invoice", "credit_note") else "payable"))


def _actual(book: Book, j: GLJournal, record: str, part: str, document: str | None = None) -> list[Posting]:
    """A journal's own lines; a customer or supplier balance line names its own contact, else the journal's."""
    parties = {*book.control("receivable"), *book.control("payable")}
    return [Posting(record, part, j.date, ln.account, ln.amount,
                    (ln.contact or j.contact) if ln.account in parties else None,
                    ln.description or None, document, ln.foreign if ln.foreign is not None else ln.amount)
            for ln in j.lines]


def _fallback(actual: list[Posting], predicted: list[Posting], record: str, on: date,
              document: str | None) -> list[Posting]:
    """The source's lines less what Celerp books for the record, per account and contact."""
    net: dict[tuple[str, str | None], Decimal] = defaultdict(lambda: ZERO)
    for sign, postings in ((1, actual), (-1, predicted)):
        for p in postings:
            net[(p.account, p.contact)] += sign * p.amount
    return [Posting(record, "fallback", on, account, amount, contact, document=document)
            for (account, contact), amount in sorted(net.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
            if amount]



def _document_postings(book: Book, doc: Document) -> list[Posting]:
    """What Celerp books for a posted document: each net and tax converted from the running
    total at the document's rate, the converted total on the customer or supplier."""
    sign = PARTY_SIGN[doc.doc_type]
    parts = iter(split_to_base([x for ln in doc.lines for x in (ln.net, ln.tax)], doc.rate, book.base))
    out: list[Posting] = []
    for ln in doc.lines:
        net, tax = next(parts), next(parts)
        out.append(Posting(doc.key, "document", doc.date, ln.account, -sign * net, description=ln.description,
                           document=doc.key, native=-sign * ln.net))
        if tax:
            out.append(Posting(doc.key, "document", doc.date, book.tax_account_for(ln.tax_code), -sign * tax,
                               document=doc.key, native=-sign * ln.tax))
    out.append(Posting(doc.key, "document", doc.date, book.party_account(doc.doc_type),
                       -sum((p.amount for p in out), ZERO), doc.contact, document=doc.key, native=sign * doc.total))
    return out + _note_difference(book, doc, out[-1].amount)


def _note_difference(book: Book, note: Document, party: Decimal) -> list[Posting]:
    """A note relieves its invoice at that invoice's rate; the difference from the note's own
    converted total is an exchange difference on the customer balance."""
    if not note.applies_to:
        return []
    target = book.documents[note.applies_to]
    sign = PARTY_SIGN[note.doc_type]
    difference = party - sign * round_money(note.total * target.rate, book.base)
    if not difference:
        return []
    return [Posting(note.key, "fx", note.date, book.party_account(note.doc_type), -difference, note.contact,
                    document=target.key),
            Posting(note.key, "fx", note.date, book.fx_account, difference, document=target.key)]


def _settlement_postings(book: Book, s: Settlement) -> list[Posting]:
    """The bank moves at the settlement's rate and the document is relieved at its own."""
    doc = book.documents[s.document]
    bank_sign = 1 if s.kind == "receipt" else -1
    relieved = round_money(s.amount * doc.rate, book.base)
    moved = round_money(s.amount * s.rate, book.base)
    out = [Posting(s.key, "settlement", s.date, book.party_account(doc.doc_type), -bank_sign * relieved, s.contact,
                   document=doc.key, native=-bank_sign * s.amount),
           Posting(s.key, "settlement", s.date, s.bank, bank_sign * moved, document=doc.key,
                   native=bank_sign * s.amount)]
    if moved != relieved:
        out.append(Posting(s.key, "fx", s.date, book.fx_account, -bank_sign * (moved - relieved), document=doc.key))
    return out


def _rate(base: Decimal, foreign: Decimal | None) -> Decimal | None:
    if not foreign or not base:
        return None
    return checked_exchange_rate(abs(base) / abs(foreign))


def _document_journal(book: Book, j: GLJournal) -> Document | None:
    """The posted document a document journal books, with its rate set from the journal's
    customer or supplier line. None when the journal books no document the export lists."""
    if j.kind not in DOCUMENT_KINDS:
        return None
    doc = find_document(book, j.kind.startswith("receivable"), j.invoice, j.contact)
    if doc is None or doc.status != "posted" or doc.journal is not None:
        return None
    party = [ln for ln in j.lines if ln.account in _party(book, doc.doc_type)]
    if len(party) != 1:
        return None
    rate = _rate(party[0].amount, party[0].foreign) if book.is_foreign(doc.currency) else Decimal(1)
    if rate is None:
        return None
    doc.rate, doc.journal = rate, j.key
    return doc


def _settlement(book: Book, j: GLJournal, owing: dict[str, Decimal]) -> Settlement | None:
    """A receipt or payment journal settling one carried document from one bank account
    in the document's currency or the base currency, within what is still owed on it."""
    kind = SETTLEMENT_KINDS.get(j.kind)
    if kind is None:
        return None
    doc = find_document(book, kind == "receipt", j.invoice, j.contact)
    if doc is None or doc.journal is None:
        return None
    banks = [ln for ln in j.lines if book.accounts[ln.account].control == "bank"]
    party = [ln for ln in j.lines if ln.account in _party(book, doc.doc_type)]
    if len(banks) != 1 or len(party) != 1:
        return None
    bank: GLLine = banks[0]
    bank_currency = book.accounts[bank.account].currency or book.base
    if bank_currency not in (book.base, doc.currency):
        return None
    foreign = book.is_foreign(doc.currency)
    amount = abs(party[0].foreign if foreign and party[0].foreign is not None else party[0].amount)
    rate = _rate(bank.amount, bank.foreign if foreign else bank.amount) if foreign else Decimal(1)
    if not amount or rate is None or amount > owing[doc.key] or (foreign and book.fx_account is None):
        return None
    owing[doc.key] -= amount
    return Settlement(j.key, kind, j.date, doc.contact, bank.account, doc.currency, rate, amount, doc.key,
                      j.reference, j.source)


def _transfer(book: Book, j: GLJournal) -> Transfer | None:
    """A transfer journal between two bank accounts that Celerp moves at the same base amount:
    one side in the base currency, or both."""
    if j.kind not in TRANSFER_KINDS or len(j.lines) != 2:
        return None
    out, into = sorted(j.lines, key=lambda ln: ln.amount)
    if any(book.accounts[ln.account].control != "bank" for ln in (out, into)) or out.amount != -into.amount:
        return None

    def native(ln: GLLine) -> Decimal:
        currency = book.accounts[ln.account].currency
        return abs(ln.foreign) if currency and ln.foreign is not None else abs(ln.amount)

    currencies = [book.accounts[ln.account].currency or book.base for ln in (out, into)]
    if book.base not in currencies or not (native(out) and native(into)):
        return None
    if currencies == [book.base, book.base] and native(out) != native(into):
        return None
    return Transfer(j.key, j.date, out.account, into.account, native(out), native(into), into.amount,
                    j.reference, next((ln.description for ln in j.lines if ln.description), ""))


def _states(book: Book, settlements: list[Settlement], keys: set[str]) -> dict[str, DocumentState]:
    """Paid and outstanding per document from the settlements and notes given."""
    paid: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for s in settlements:
        paid[s.document] += s.amount
    for key in keys:
        note = book.documents[key]
        if note.applies_to:
            paid[note.applies_to] += note.total
            paid[key] += note.total
    states = {}
    for key in keys:
        outstanding = book.documents[key].total - paid[key]
        states[key] = DocumentState(paid[key], outstanding, "paid" if outstanding <= 0 else "awaiting_payment")
    return states


def _check_cutover(book: Book, cutover: date | None) -> date:
    if cutover is None:
        raise ScanError(t("migration.err_choose_cutover"))
    if book.journals and cutover < book.journals[0].date:
        raise ScanError(t("migration.err_cutover_before_first"))
    return cutover


def _carried(book: Book, ledger: Ledger, posted: list[str], cutover: date) -> set[str]:
    """Pre-cutover documents imported as themselves: those open at the cutover date, those a
    later settlement or note settles, and the notes applied to them."""
    pre = {k for k in posted if book.documents[k].date <= cutover}
    early = [s for s in ledger.settlements.values() if s.date <= cutover]
    carried = {k for k, state in _states(book, early, pre).items() if state.amount_outstanding != 0}
    carried |= {s.document for s in ledger.settlements.values() if s.date > cutover}
    carried |= {book.documents[k].applies_to for k in posted if book.documents[k].date > cutover
                and book.documents[k].applies_to}
    carried &= pre
    return carried | {k for k in pre if book.documents[k].applies_to in carried}


def _latest_rates(book: Book) -> None:
    """A foreign invoice or bill that posts nothing carries the latest rate the ledger booked
    for its currency on or before its date, else the earliest one booked after."""
    booked = sorted((d.date, d.rate, d.currency) for d in book.documents.values() if d.journal)
    for doc in book.documents.values():
        if doc.journal or not book.is_foreign(doc.currency):
            doc.rate = doc.rate if doc.journal else Decimal(1)
            continue
        same = [(on, rate) for on, rate, currency in booked if currency == doc.currency]
        before = [rate for on, rate in same if on <= doc.date]
        doc.rate = before[-1] if before else (same[0][1] if same else None)


def _link_notes(book: Book) -> None:
    """A credit note applies to the invoice its reference names, of the same contact."""
    for note in book.documents.values():
        if note.doc_type in ("credit_note", "debit_note") and note.reference:
            target = find_document(book, note.sales, note.reference, note.contact)
            if target is not None and target.doc_type in ("invoice", "bill") and target.contact == note.contact:
                note.applies_to = target.key


def build_ledger(book: Book, decisions: MigrationDecisions) -> Ledger:
    """The postings and imported records for one migration."""
    cutover = _check_cutover(book, decisions.cutover_date) if decisions.mode == CIFMode.CUTOVER else None
    for doc in book.documents.values():
        doc.journal, doc.rate, doc.applies_to = None, None, None
    _link_notes(book)
    ledger = Ledger(book, cutover)
    booked: dict[str, list[Posting]] = {}
    for j in book.journals:
        doc = _document_journal(book, j)
        if doc is not None:
            booked[j.key] = _actual(book, j, doc.key, "document", doc.key)
    for doc in book.documents.values():
        # A note whose invoice posts nothing is booked as the journal it is.
        if doc.applies_to and (doc.journal is None or book.documents[doc.applies_to].journal is None):
            doc.applies_to = None
        if doc.doc_type == "debit_note" and doc.journal and not doc.applies_to:
            booked.pop(doc.journal)
            doc.journal = None
    _latest_rates(book)
    posted = [d.key for d in sorted(book.documents.values(), key=lambda d: (d.date, d.key)) if d.journal]
    owing = {k: book.documents[k].total for k in posted}
    for k in posted:
        if book.documents[k].applies_to:
            owing[book.documents[k].applies_to] -= book.documents[k].total
            owing[k] = ZERO

    for j in book.journals:
        if j.key in booked:
            doc = next(d for d in book.documents.values() if d.journal == j.key)
            predicted = _document_postings(book, doc)
            ledger.postings += predicted + _fallback(booked[j.key], predicted, doc.key, j.date, doc.key)
        elif (s := _settlement(book, j, owing)) is not None:
            ledger.settlements[j.key] = s
            predicted = _settlement_postings(book, s)
            ledger.postings += predicted + _fallback(_actual(book, j, j.key, "settlement"), predicted, j.key,
                                                     j.date, s.document)
        elif (tr := _transfer(book, j)) is not None:
            ledger.transfers[j.key] = tr
            ledger.postings += _actual(book, j, j.key, "transfer")
        else:
            ledger.journals[j.key] = j
            ledger.postings += _actual(book, j, j.key, "journal")

    ledger.states = _states(book, list(ledger.settlements.values()), set(posted))
    carried = posted if cutover is None else [k for k in posted if k in _carried(book, ledger, posted, cutover)
                                              or book.documents[k].date > cutover]
    unposted = [d.key for d in sorted(book.documents.values(), key=lambda d: (d.date, d.key))
                if not d.journal and d.status != "posted"
                and (cutover is None or d.date > cutover or d.status == "draft")]
    ledger.documents = [*carried, *unposted]

    if cutover is not None:
        balance: dict[tuple[str, str | None], Decimal] = defaultdict(lambda: ZERO)
        for sign, postings in ((1, ledger.postings), (-1, ledger.imported_postings())):
            for p in postings:
                balance[(p.account, p.contact)] += sign * p.amount
        ledger.opening = [Posting(ledger.opening_key, "opening", cutover, account, amount, contact)
                          for (account, contact), amount in sorted(balance.items(),
                                                                   key=lambda kv: (kv[0][0], kv[0][1] or ""))
                          if amount]
    return ledger

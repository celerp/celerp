# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Convert an export set's book and its ledger into a CIF bundle and its coverage.

Source identity is what the export names a record by: an account by its code, a
contact by its name, an item by its code, a tax rate by its name, an invoice or bill
by its number (with its contact where two contacts share a number), a journal by its
ledger journal number. Journal amounts are the ledger's own base currency lines, with
the foreign amount alongside where the ledger states one.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from celerp.importers.adapters.exports.book import ZERO, Book, Document
from celerp.importers.adapters.exports.ledger import Ledger, Posting
from celerp.importers.schema import (
    CIFAccount,
    CIFAllocation,
    CIFBankTransfer,
    CIFCompanyProfile,
    CIFContact,
    CIFCoverageEntry,
    CIFCurrency,
    CIFDocument,
    CIFImportBundle,
    CIFItem,
    CIFJournalEntry,
    CIFJournalLine,
    CIFLineItem,
    CIFLineTax,
    CIFLocation,
    CIFSettlement,
    CIFTaxCode,
    CoverageClass,
)
from celerp.services.money import checked_exchange_rate

# Journal parts of the ledger, and the source id suffix each part's journal carries.
JOURNAL_PARTS = {"journal": "", "fallback": ":journal"}
FALLBACK = "{} (journal fallback)"
FALLBACK_NARRATION = ("Lines of this record booked differently in the source: rounding, an exchange difference, "
                      "or a payment's part not applied to any document.")
OPENING_NARRATION = "Opening balances at the cutover date."
TRACKING = "Tracking category"
TRACKING_NOTE = ("Each invoice and bill line keeps its tracking options; journal entry lines are carried without "
                 "them. Celerp does not report by them.")
FALLBACK_NOTE = ("What the source booked for these records less what Celerp books for them (a rounding cent, an "
                 "exchange difference, or a payment's part left on the contact as credit) is carried as a journal "
                 "on the record.")


class Source:
    """Provenance for one source system's records."""

    def __init__(self, system: str) -> None:
        self.system = system

    def __call__(self, source_type: str, key: str, ref: str | None = None) -> dict:
        return {"source_system": self.system, "source_type": source_type, "source_external_id": key,
                "source_ref": ref or None}


def _accounts(book: Book, src: Source) -> list[CIFAccount]:
    return [CIFAccount(**src("Account", a.code, a.code), code=a.code, name=a.name, account_type=a.account_type,
                       control=a.control, currency=a.currency if book.is_foreign(a.currency) else None,
                       is_active=a.active)
            for a in sorted(book.accounts.values(), key=lambda a: a.code)]


def _contacts(book: Book, src: Source) -> list[CIFContact]:
    return [CIFContact(**src("Contact", c.name), name=c.name, roles=sorted(c.roles), email=c.email, phone=c.phone,
                       address=c.address, tax_id=c.tax_id, currency=c.currency)
            for c in sorted(book.contacts.values(), key=lambda c: c.name)]


def _items(book: Book, src: Source) -> list[CIFItem]:
    """Each item at the first location, then each holding as stock of its item."""
    home = book.locations[0] if book.locations else None
    items = [CIFItem(**src("Item", i.code, i.code), sku=i.code, name=i.name, description=i.description,
                     retail_price=i.price, sell_by="piece", status="available" if i.active else "archived",
                     location_name=home)
             for i in sorted(book.items.values(), key=lambda i: i.code)]
    for h in sorted(book.holdings.values(), key=lambda h: h.key):
        item = book.items[h.item]
        items.append(CIFItem(**src("Item", h.key, h.key), sku=item.code, name=item.name, description=item.description,
                             retail_price=item.price, sell_by="piece", status="available" if item.active else "archived",
                             product_external_id=item.code, batch_no=h.lot, location_name=h.location))
    return items



def _document(book: Book, ledger: Ledger, doc: Document, src: Source) -> CIFDocument:
    state = ledger.states.get(doc.key)
    metadata: dict = {}
    if doc.applications:
        metadata["applications"] = {key: str(amount) for key, amount in doc.applications.items()}
    if doc.withheld:
        metadata["withheld"] = {account: str(amount) for account, amount in doc.withheld.items()}
    lines = [CIFLineItem(item_external_id=ln.item, description=ln.description or None,
                         account_external_id=ln.account,
                         taxes=[CIFLineTax(tax_code_external_id=x.code,
                                           tax_account_external_id=book.tax_account_for(x.code), amount=x.amount)
                                for x in ln.taxes],
                         quantity=ln.quantity, unit_price=ln.unit_price, discount_percent=ln.discount,
                         total_price=ln.net, tracking=ln.tracking)
             for ln in doc.lines]
    return CIFDocument(**src(doc.source_type, doc.key, doc.number), doc_type=doc.doc_type,
                       status=state.status if state else doc.status, contact_external_id=doc.contact,
                       ref=doc.number, issue_date=doc.date, payment_due_date=doc.due, currency=doc.currency,
                       exchange_rate=doc.rate if book.is_foreign(doc.currency) else None,
                       total=doc.total, tax_total=doc.tax_total,
                       amount_paid=state.amount_paid if state else ZERO,
                       amount_outstanding=state.amount_outstanding if state else ZERO,
                       line_items=lines, metadata=metadata)


def _settlements(ledger: Ledger, src: Source) -> list[CIFSettlement]:
    book = ledger.book
    return [CIFSettlement(**src(s.source, s.key, s.reference), settlement_type=s.kind, settlement_date=s.date,
                          contact_external_id=s.contact, bank_account_external_id=s.bank, currency=s.currency,
                          exchange_rate=s.rate if book.is_foreign(s.currency) else None, amount=s.amount,
                          allocations=[CIFAllocation(document_external_id=key, amount=amount)
                                       for key, amount in s.allocations])
            for s in ledger.imported_settlements()]


def _transfers(ledger: Ledger, src: Source) -> list[CIFBankTransfer]:
    imported = {p.record for p in ledger.imported_postings() if p.part == "transfer"}
    return [CIFBankTransfer(**src("Bank Transfer", tr.key, tr.reference), transfer_date=tr.date,
                            from_account_external_id=tr.from_bank, to_account_external_id=tr.to_bank,
                            amount=tr.amount, to_amount=tr.to_amount if tr.to_amount != tr.amount else None)
            for tr in ledger.transfers.values() if tr.key in imported]


def _lines(postings: list[Posting], currency=lambda p: None) -> list[CIFJournalLine]:
    """Base currency lines; `currency` names a posting's foreign currency when it was booked
    in one, so the foreign amount and the rate it implies travel with the line."""
    out = []
    for p in postings:
        if not p.amount:
            continue
        foreign = currency(p)
        native = {}
        if foreign and p.native:
            native = {"fx_debit": max(p.native, ZERO) or None, "fx_credit": max(-p.native, ZERO) or None,
                      "fx_currency": foreign, "fx_rate": checked_exchange_rate(abs(p.amount / p.native))}
        out.append(CIFJournalLine(account_external_id=p.account, debit=max(p.amount, ZERO),
                                  credit=max(-p.amount, ZERO), description=p.description,
                                  contact_external_id=p.contact, **native))
    return out


def _journals(ledger: Ledger, src: Source) -> list[CIFJournalEntry]:
    book = ledger.book
    out = []
    if ledger.opening:
        def held(p: Posting) -> str | None:
            currency = book.accounts[p.account].currency
            return currency if book.is_foreign(currency) else None
        out.append(CIFJournalEntry(**src("Opening balances", ledger.opening_key), entry_date=ledger.cutover,
                                   narration=OPENING_NARRATION, currency=book.base, lines=_lines(ledger.opening, held)))
    groups: dict[tuple[str, str], list[Posting]] = defaultdict(list)
    for p in ledger.imported_postings():
        if p.part in JOURNAL_PARTS:
            groups[(p.record, p.part)].append(p)
    for (record, part), postings in groups.items():
        journal = ledger.journals.get(record)
        if part == "journal":
            foreign = journal.currency if book.is_foreign(journal.currency) else None
            lines = _lines(postings, lambda p: foreign)
            source_type, ref = journal.source, journal.reference
            narration = next((ln.description for ln in journal.lines if ln.description), None) or journal.reference
        else:
            lines = _lines(postings)
            source_type, ref = _fallback_type(ledger, record), None
            narration = FALLBACK_NARRATION
        if len(lines) < 2:
            continue
        out.append(CIFJournalEntry(**src(source_type, record + JOURNAL_PARTS[part], ref), entry_date=postings[0].date,
                                   narration=narration or None, currency=book.base, fallback=part == "fallback",
                                   lines=lines))
    return out


def _fallback_type(ledger: Ledger, record: str) -> str:
    """The source type of the record a fallback journal belongs to."""
    if record in ledger.settlements:
        return ledger.settlements[record].source
    return ledger.book.documents[record].source_type


def _currencies(book: Book, src: Source) -> list[CIFCurrency]:
    return [CIFCurrency(**src("Currency", code), code=code) for code in [book.base, *(
        c for c in book.currencies if c != book.base)]]


def build_bundle(ledger: Ledger, src: Source) -> CIFImportBundle:
    """The CIF bundle for one migration."""
    book = ledger.book
    company = CIFCompanyProfile(**src("Organisation", "organisation"), name=book.company_name,
                                base_currency=book.base) if book.company_name else None
    return CIFImportBundle(
        company=company,
        currencies=_currencies(book, src),
        accounts=_accounts(book, src),
        locations=[CIFLocation(**src("Location", name), name=name) for name in book.locations],
        tax_codes=[CIFTaxCode(**src("Tax rate", tc.name), name=tc.name, rate_percent=tc.rate,
                              account_external_id=tc.account) for tc in book.tax_codes.values()],
        contacts=_contacts(book, src),
        items=_items(book, src),
        documents=[_document(book, ledger, book.documents[k], src) for k in ledger.documents],
        settlements=_settlements(ledger, src),
        bank_transfers=_transfers(ledger, src),
        journals=_journals(ledger, src),
    )


def coverage(ledger: Ledger, skipped: dict[str, CoverageClass]) -> list[CIFCoverageEntry]:
    """How each kind of source record is carried. `skipped` classes the records the book
    counted but does not carry."""
    book = ledger.book
    mapped: Counter = Counter({"Account": len(book.accounts), "Contact": len(book.contacts),
                               "Item": len(book.items), "Tax rate": len(book.tax_codes)})
    mapped.update(d.source_type for d in book.documents.values() if d.journal or d.status != "posted")
    mapped.update(j.source for j in ledger.journals.values())
    mapped.update(s.source for s in ledger.settlements.values())
    mapped.update("Bank Transfer" for _ in ledger.transfers)
    fallbacks = Counter(_fallback_type(ledger, record)
                        for record in {p.record for p in ledger.postings if p.part == "fallback"})
    rows = [CIFCoverageEntry(source_type=label, count=count, coverage_class=CoverageClass.MAPPED)
            for label, count in mapped.items() if count]
    rows += [CIFCoverageEntry(source_type=FALLBACK.format(label), count=count,
                              coverage_class=CoverageClass.MAPPED_WITH_LOSS, note=FALLBACK_NOTE)
             for label, count in fallbacks.items()]
    rows += [CIFCoverageEntry(source_type=label, count=count, coverage_class=skipped[label])
             for label, count in book.skipped.items() if count]
    if book.tracking:
        rows.append(CIFCoverageEntry(source_type=TRACKING, count=len(book.tracking),
                                     coverage_class=CoverageClass.UNSUPPORTED_NONFINANCIAL, note=TRACKING_NOTE))
    return sorted(rows, key=lambda r: r.source_type)

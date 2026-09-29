# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Convert a decoded Manager book and its ledger into a CIF bundle.

Source identity is the Manager object key throughout: two records with the
same display name stay two records. Every financial record is in the base
currency; journal amounts are taken from the same postings the source
expectations are computed from.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from celerp.importers.adapters.manager_io.attachments import Screened
from celerp.importers.adapters.manager_io.book import INVENTORY, Book, Document, Line
from celerp.importers.adapters.manager_io.ledger import Ledger, Posting
from celerp.importers.adapters.manager_io.types import GUIDS
from celerp.importers.schema import (
    CIFAccount,
    CIFAllocation,
    CIFAttachment,
    CIFBankTransfer,
    CIFCompanyProfile,
    CIFContact,
    CIFCurrency,
    CIFDocument,
    CIFExchangeRate,
    CIFImportBundle,
    CIFInventoryAdjustment,
    CIFItem,
    CIFJournalEntry,
    CIFJournalLine,
    CIFLineItem,
    CIFSettlement,
    CIFTaxCode,
)
from celerp.services.money import round_money

SOURCE_SYSTEM = "manager_io"
ZERO = Decimal(0)
# Journal parts of the ledger, and the source id suffix each part's journal carries.
JOURNAL_PARTS = {"journal": "", "fallback": ":journal"}
FALLBACK_NARRATION = "Lines of this record that settle no invoice or bill."


def _src(source_type: str, key: str, ref: str | None = None) -> dict:
    return {"source_system": SOURCE_SYSTEM, "source_type": source_type, "source_external_id": key, "source_ref": ref}


def _company(book: Book) -> CIFCompanyProfile | None:
    if not book.company_name:
        return None
    return CIFCompanyProfile(**_src("BusinessDetails", book.company_key), name=book.company_name,
                             base_currency=book.base_code, address=book.address, money_precision=book.precision)


def _currencies(book: Book) -> list[CIFCurrency]:
    base = CIFCurrency(**_src("BaseCurrency", book.base_key or GUIDS["BaseCurrency"]), code=book.base_code,
                       name=book.base_name, precision=book.precision)
    foreign = [CIFCurrency(**_src("ForeignCurrency", c.key), code=c.code, name=c.name, precision=c.precision)
               for c in book.currencies.values() if c.code != book.base_code]
    return [base, *sorted(foreign, key=lambda c: c.code)]


def _exchange_rates(book: Book) -> list[CIFExchangeRate]:
    return [CIFExchangeRate(**_src("ExchangeRate", r.key), from_currency=book.currency_code(r.currency),
                            to_currency=book.base_code, rate=r.rate, effective_date=r.effective)
            for r in sorted(book.rates, key=lambda r: (r.effective, r.key))]


def _accounts(book: Book) -> list[CIFAccount]:
    out = []
    for a in sorted(book.accounts.values(), key=lambda a: (a.code or "", a.key)):
        foreign = a.control == "bank" and book.is_foreign(a.currency)
        out.append(CIFAccount(**_src(a.source_type, a.key, a.code), code=a.code, name=a.name,
                              account_type=a.account_type, control=a.control,
                              currency=book.currency_code(a.currency) if foreign else None, is_active=not a.inactive))
    return out


def _tax_codes(book: Book) -> list[CIFTaxCode]:
    return [CIFTaxCode(**_src("TaxCode", t.key), name=t.name, rate_percent=t.rate, account_external_id=t.account)
            for t in book.tax_codes.values()]


def _contacts(book: Book) -> list[CIFContact]:
    return [CIFContact(**_src(c.source_type, c.key, c.code), name=c.name,
                       roles=["customer" if c.source_type == "Customer" else "supplier"], email=c.email,
                       address=c.address, currency=book.currency_code(c.currency))
            for c in book.contacts.values()]


def _items(book: Book) -> list[CIFItem]:
    return [CIFItem(**_src("InventoryItem", i.key, i.code), sku=i.code, name=i.name, unit=i.unit, sell_by="piece",
                    retail_price=i.sales_price, status="archived" if i.inactive else "available")
            for i in book.items.values()]


def _line(book: Book, doc: Document, line: Line) -> CIFLineItem:
    """One line, tax exclusive: an item line posts to inventory, and a tax-inclusive
    source price is carried as the tax-exclusive price its net implies."""
    unit_price, discount = line.unit_price, line.discount or None
    if doc.include_tax:
        unit_price = round_money(line.net / line.quantity, book.currency_code(doc.currency)) if line.quantity else line.net
        discount = None
    return CIFLineItem(item_external_id=line.item, description=line.description,
                       account_external_id=INVENTORY if line.item else line.account,
                       tax_code_external_id=line.tax_code,
                       tax_account_external_id=book.tax_codes[line.tax_code].account if line.tax_code else None,
                       quantity=line.quantity, unit_price=unit_price, discount=discount,
                       tax_amount=line.tax, total_price=line.net)


def _document(book: Book, ledger: Ledger, doc: Document) -> CIFDocument:
    state = ledger.states[doc.key]
    metadata: dict = {"amounts_include_tax": doc.include_tax}
    if doc.applies_to:
        metadata["applies_to"] = doc.applies_to
    return CIFDocument(**_src(doc.source_type, doc.key, doc.ref), doc_type=doc.doc_type, status=state.status,
                       contact_external_id=doc.contact, ref=doc.ref, issue_date=doc.date, payment_due_date=doc.due,
                       currency=book.currency_code(doc.currency),
                       total=doc.total, tax_total=doc.tax_total, amount_paid=state.amount_paid,
                       amount_outstanding=state.amount_outstanding, line_items=[_line(book, doc, ln) for ln in doc.lines],
                       metadata=metadata)


def _settlements(book: Book, ledger: Ledger) -> list[CIFSettlement]:
    out = []
    for key, amount in ledger.settlement_amounts.items():
        s = book.settlements[key]
        allocated: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for document, allocation in ledger.allocations[key]:
            allocated[document] += allocation
        out.append(CIFSettlement(
            **_src(s.source_type, key, s.ref), settlement_type=s.kind, settlement_date=s.date,
            contact_external_id=s.contact, bank_account_external_id=s.bank, currency=book.currency_code(s.currency),
            amount=amount,
            allocations=[CIFAllocation(document_external_id=d, amount=a) for d, a in allocated.items()],
        ))
    return out


def _journal_lines(postings: list[Posting]) -> list[CIFJournalLine]:
    return [CIFJournalLine(account_external_id=p.account, debit=max(p.amount, ZERO), credit=max(-p.amount, ZERO),
                           description=p.description, contact_external_id=p.contact)
            for p in postings if p.amount]


def _journals(book: Book, ledger: Ledger) -> list[CIFJournalEntry]:
    out = []
    if ledger.opening:
        out.append(CIFJournalEntry(**_src("OpeningBalances", ledger.opening_key), entry_date=ledger.cutover,
                                   narration="Opening balances at the cutover date.", currency=book.base_code,
                                   lines=_journal_lines(ledger.opening)))
    groups: dict[tuple[str, str], list[Posting]] = defaultdict(list)
    for p in ledger.imported_postings():
        if p.part in JOURNAL_PARTS:
            groups[(p.record, p.part)].append(p)
    for (record, part), postings in groups.items():
        lines = _journal_lines(postings)
        if len(lines) < 2:
            continue
        source = book.journals.get(record) or book.settlements[record]
        narration = source.narration if part == "journal" else FALLBACK_NARRATION
        out.append(CIFJournalEntry(**_src(book.names[record], record + JOURNAL_PARTS[part], source.ref),
                                   entry_date=postings[0].date, narration=narration, currency=book.base_code,
                                   lines=lines))
    return out


def _transfers(book: Book, ledger: Ledger) -> list[CIFBankTransfer]:
    imported = {p.record for p in ledger.imported_postings() if p.part == "transfer"}
    return [CIFBankTransfer(**_src("InterAccountTransfer", t.key, t.ref), transfer_date=t.date,
                            from_account_external_id=t.from_bank, to_account_external_id=t.to_bank, amount=t.amount)
            for t in sorted(book.transfers.values(), key=lambda t: (t.date, t.key)) if t.key in imported]


def _stock(book: Book, ledger: Ledger) -> list[CIFInventoryAdjustment]:
    """The opening position per item, then the stock each imported document moves.

    Each carries its value; none posts, since the opening journal and the documents
    carry the value on the ledger."""
    opening = [CIFInventoryAdjustment(**_src("OpeningBalances", f"{ledger.opening_key}:{item}"), kind="opening",
                                      adjustment_date=ledger.cutover, item_external_id=item, quantity=qty, value=value)
               for item, (qty, value) in ledger.opening_stock.items()]
    lines: dict[str, int] = defaultdict(int)
    moved = []
    for p in ledger.imported_postings():
        if p.part != "document" or not p.item:
            continue
        lines[p.record] += 1
        doc = book.documents[p.record]
        moved.append(CIFInventoryAdjustment(**_src(doc.source_type, f"{p.record}:stock:{lines[p.record]}", doc.ref),
                                            kind="adjustment", adjustment_date=p.date, item_external_id=p.item,
                                            quantity=p.quantity, value=p.amount))
    return [*opening, *moved]


def _attachments(book: Book, screened: Screened) -> list[CIFAttachment]:
    return [CIFAttachment(**_src("Attachment", a.key), file_name=a.name, declared_content_type=a.content_type,
                          size_bytes=a.size, sha256=a.sha256, target_source_type=a.target_type,
                          target_source_external_id=a.target)
            for a in screened.accepted.values()]


def carried(book: Book, ledger: Ledger) -> set[str]:
    """Keys of the source records this migration imports under their own id: attachment targets."""
    imported = {p.record for p in ledger.imported_postings() if p.part in ("transfer", "journal")}
    return {*book.contacts, *book.items, *ledger.documents, *ledger.settlement_amounts, *imported}


def build_bundle(book: Book, ledger: Ledger, screened: Screened) -> CIFImportBundle:
    """The CIF bundle for one migration. Accepted attachments must already target imported records."""
    return CIFImportBundle(
        company=_company(book),
        currencies=_currencies(book),
        exchange_rates=_exchange_rates(book),
        accounts=_accounts(book),
        tax_codes=_tax_codes(book),
        contacts=_contacts(book),
        items=_items(book),
        documents=[_document(book, ledger, book.documents[k]) for k in ledger.documents],
        settlements=_settlements(book, ledger),
        bank_transfers=_transfers(book, ledger),
        journals=_journals(book, ledger),
        inventory_adjustments=_stock(book, ledger),
        attachments=_attachments(book, screened),
    )

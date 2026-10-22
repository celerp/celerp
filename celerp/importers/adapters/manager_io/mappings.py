# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Convert a decoded Manager book and its ledger into a CIF bundle.

Source identity is the Manager object key throughout: two records with the
same display name stay two records. A foreign currency record keeps its own
currency and the rate it was booked at; journal amounts are taken from the same
postings the source expectations are computed from, in the base currency, with
the foreign amount alongside.

Stock moves as inventory adjustments, one per item a physical record moves. Each
invoice or bill names the records that moved its goods, line by line, so the
document keeps its delivery or receipt history.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from celerp.importers.adapters.manager_io.attachments import Screened
from celerp.importers.adapters.manager_io.book import (
    DEFAULT_LOCATION, INVENTORY, INVENTORY_PURCHASES, STARTING, Book, Document, Line, line_account,
)
from celerp.importers.adapters.manager_io.ledger import Ledger, Posting, foreign_holders, holding_stock, posting_holder
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
    CIFImportBundle,
    CIFInventoryAdjustment,
    CIFItem,
    CIFJournalEntry,
    CIFJournalLine,
    CIFLineItem,
    CIFLineTax,
    CIFLocation,
    CIFSettlement,
    CIFTaxCode,
)
from celerp.services.money import checked_exchange_rate, round_money

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


def _locations(book: Book) -> list[CIFLocation]:
    """With a custom location, every location, the default first so it stays the default.
    With none, Celerp gives the company its own default location."""
    if not book.locations:
        return []
    custom = [CIFLocation(**_src("CustomInventoryLocation", key), name=name.strip())
              for key, name in book.locations.items() if not book.is_blocked(key)]
    return [CIFLocation(**_src("DefaultInventoryLocation", DEFAULT_LOCATION), name=book.default_location_name),
            *custom]


def _accounts(book: Book) -> list[CIFAccount]:
    out = []
    for a in sorted(book.accounts.values(), key=lambda a: (a.code or "", a.key)):
        foreign = a.control == "bank" and book.is_foreign(a.currency)
        out.append(CIFAccount(**_src(a.source_type, a.key, a.code), code=a.code, name=a.name,
                              account_type=a.account_type, control=a.control, parent_external_id=a.parent,
                              currency=book.currency_code(a.currency) if foreign else None, is_active=not a.inactive))
    return out


def _tax_codes(book: Book) -> list[CIFTaxCode]:
    return [CIFTaxCode(**_src("TaxCode", t.key), name=t.name, rate_percent=t.rate, account_external_id=t.account)
            for t in book.tax_codes.values()]


def _contacts(book: Book) -> list[CIFContact]:
    return [CIFContact(**_src(c.source_type, c.key, c.code), name=c.name,
                       roles=["customer" if c.source_type == "Customer" else "supplier"], email=c.email,
                       address=c.address, phone=c.phone, currency=book.currency_code(c.currency))
            for c in book.contacts.values()]


def _items(book: Book) -> list[CIFItem]:
    held = holding_stock(book)
    return [CIFItem(**_src("InventoryItem", i.key, i.code), sku=i.code, name=i.name, unit=i.unit, sell_by="piece",
                    retail_price=i.sales_price, status="archived" if i.inactive and i.key not in held else "available")
            for i in book.items.values()]


def _line(book: Book, doc: Document, line: Line) -> CIFLineItem:
    """One line, tax exclusive: an item bought posts to inventory, one sold to inventory
    sales carrying its cost of sales, and a tax-inclusive source price is carried as the
    tax-exclusive price its net implies."""
    unit_price, discount_percent = line.unit_price, line.discount_percent
    if doc.include_tax:
        unit_price = round_money(line.net / line.quantity, book.currency_code(doc.currency)) if line.quantity else line.net
        discount_percent = None
    return CIFLineItem(item_external_id=line.item, description=line.description,
                       account_external_id=line_account(doc, line),
                       taxes=[CIFLineTax(tax_code_external_id=line.tax_code,
                                         tax_account_external_id=book.tax_codes[line.tax_code].account
                                         if line.tax_code else None, amount=line.tax)]
                       if line.tax_code or line.tax else [],
                       quantity=line.quantity, unit_price=unit_price, discount_percent=discount_percent,
                       total_price=line.net, cost_basis=line.cost or None)


def _movements(book: Book) -> dict[str, list[dict]]:
    """The physical records that moved each document's goods, in the order stock moved.
    A document flagged to move its own stock names itself."""
    out: dict[str, list[dict]] = defaultdict(list)
    for movement in (m for m in book.moves if m.document):
        out[movement.document].append({
            "source": movement.key, "date": movement.date.isoformat(),
            "lines": [{"line": ln.line, "item": ln.item, "quantity": str(abs(ln.quantity)), "value": str(abs(ln.value))}
                      for ln in movement.lines],
        })
    return out


def _record(book: Book, key: str) -> Document:
    return book.documents.get(key) or book.orders[key]


def _links(book: Book, ledger: Ledger, doc: Document) -> dict:
    """The imported quote or order a record came from, and the imported record a quote or
    order became."""
    links: dict = {}
    if doc.converted_from in ledger.orders:
        source = book.orders[doc.converted_from]
        links["converted_from"] = {"source_type": source.source_type, "external_id": source.key, "ref": source.ref}
    if doc.converted_to and (doc.converted_to in ledger.documents or doc.converted_to in ledger.orders):
        successor = _record(book, doc.converted_to)
        links["converted_to"] = {"source_type": successor.source_type, "external_id": successor.key,
                                 "doc_type": successor.doc_type}
    return links


def _order(book: Book, ledger: Ledger, order: Document) -> CIFDocument:
    """A quote or order: its lines and totals, posting nothing and owing nothing."""
    metadata: dict = {"amounts_include_tax": order.include_tax, **_links(book, ledger, order)}
    if order.valid_until:
        metadata["valid_until"] = order.valid_until.isoformat()
    if order.billing_address:
        metadata["billing_address"] = order.billing_address
    return CIFDocument(**_src(order.source_type, order.key, order.ref), doc_type=order.doc_type,
                       status=order.order_status, contact_external_id=order.contact, ref=order.ref,
                       issue_date=order.date, currency=book.currency_code(order.currency),
                       exchange_rate=order.rate if book.is_foreign(order.currency) else None,
                       total=order.total, tax_total=order.tax_total, amount_paid=ZERO, amount_outstanding=ZERO,
                       line_items=[_line(book, order, ln) for ln in order.lines], metadata=metadata)


def _document(book: Book, ledger: Ledger, doc: Document, movements: dict[str, list[dict]]) -> CIFDocument:
    state = ledger.states[doc.key]
    metadata: dict = {"amounts_include_tax": doc.include_tax, **_links(book, ledger, doc)}
    if doc.applies_to:
        metadata["applications"] = {doc.applies_to: str(doc.total)}
    if doc.billing_address:
        metadata["billing_address"] = doc.billing_address
    if movements.get(doc.key):
        metadata["receipts" if doc.source_type == "PurchaseInvoice" else "deliveries"] = movements[doc.key]
    if paid := book.starting_paid().get(doc.key):
        metadata["paid_at_start"] = str(paid)
    if any(ln.cost for ln in doc.lines):
        metadata["cost_of_sales"] = {"cost_account": INVENTORY_PURCHASES, "inventory_account": INVENTORY}
    return CIFDocument(**_src(doc.source_type, doc.key, doc.ref), doc_type=doc.doc_type, status=state.status,
                       contact_external_id=doc.contact, ref=doc.ref, issue_date=doc.date, payment_due_date=doc.due,
                       currency=book.currency_code(doc.currency),
                       exchange_rate=doc.rate if book.is_foreign(doc.currency) else None,
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
            exchange_rate=s.rate if book.is_foreign(s.currency) else None, amount=amount,
            allocations=[CIFAllocation(document_external_id=d, amount=a) for d, a in allocated.items()],
        ))
    return out


def _journal_lines(postings: list[Posting], fx=lambda p: None) -> list[CIFJournalLine]:
    """Base currency lines; `fx` gives a posting's (currency, rate) when it was booked in a
    foreign currency, so the foreign amount travels with the line."""
    out = []
    for p in postings:
        if not p.amount:
            continue
        foreign = fx(p)
        native = {}
        if foreign:
            native = {"fx_debit": max(p.native, ZERO) or None, "fx_credit": max(-p.native, ZERO) or None,
                      "fx_currency": foreign[0], "fx_rate": foreign[1]}
        out.append(CIFJournalLine(account_external_id=p.account, debit=max(p.amount, ZERO),
                                  credit=max(-p.amount, ZERO), description=p.description,
                                  contact_external_id=p.contact, **native))
    return out


def _record_fx(book: Book, source) -> object:
    """Every line of a journal or a settlement's remainder shares the record's currency and rate."""
    if not book.is_foreign(source.currency):
        return lambda p: None
    foreign = (book.currency_code(source.currency), source.rate)
    return lambda p: foreign


def _opening_fx(book: Book) -> object:
    """An opening line is foreign only for a bank account or contact kept in a foreign
    currency; its rate is the one its base balance and foreign balance imply."""
    holders = foreign_holders(book)

    def fx(p: Posting):
        holder = posting_holder(p, holders)
        if holder is None or not p.native:
            return None
        return book.currency_code(holders[holder]), checked_exchange_rate(p.amount / p.native)
    return fx


def _journals(book: Book, ledger: Ledger) -> list[CIFJournalEntry]:
    out = []
    if ledger.opening:
        out.append(CIFJournalEntry(**_src("OpeningBalances", ledger.opening_key), entry_date=ledger.cutover,
                                   narration="Opening balances at the cutover date.", currency=book.base_code,
                                   lines=_journal_lines(ledger.opening, _opening_fx(book))))
    groups: dict[tuple[str, str], list[Posting]] = defaultdict(list)
    for p in ledger.imported_postings():
        if p.part in JOURNAL_PARTS:
            groups[(p.record, p.part)].append(p)
    for (record, part), postings in groups.items():
        source = book.journals.get(record) or book.settlements[record]
        lines = _journal_lines(postings, _record_fx(book, source))
        if len(lines) < 2:
            continue
        narration = source.narration if part == "journal" else FALLBACK_NARRATION
        out.append(CIFJournalEntry(**_src(book.names[record], record + JOURNAL_PARTS[part], source.ref),
                                   entry_date=postings[0].date, narration=narration, currency=book.base_code,
                                   fallback=part == "fallback", lines=lines))
    return out


def _transfers(book: Book, ledger: Ledger) -> list[CIFBankTransfer]:
    imported = {p.record for p in ledger.imported_postings() if p.part == "transfer"}
    return [CIFBankTransfer(**_src("InterAccountTransfer", t.key, t.ref), transfer_date=t.date,
                            from_account_external_id=t.from_bank, to_account_external_id=t.to_bank, amount=t.amount,
                            to_amount=t.to_amount if t.to_amount != t.amount else None, exchange_rate=t.rate)
            for t in sorted(book.transfers.values(), key=lambda t: (t.date, t.key)) if t.key in imported]


def _stock(book: Book, ledger: Ledger) -> list[CIFInventoryAdjustment]:
    """The opening position per item, then the stock each imported physical record moves.

    The opening position is the cutover balance, or on full history the starting
    balance. Each carries its value; none posts, since the opening or starting
    balances journal and the documents carry the value on the ledger."""
    opening = [CIFInventoryAdjustment(**_src("OpeningBalances", f"{ledger.opening_key}:{item}"), kind="opening",
                                      adjustment_date=ledger.cutover, item_external_id=item, quantity=qty, value=value)
               for item, (qty, value) in ledger.opening_stock.items()]
    opening += [CIFInventoryAdjustment(**_src("StartingBalances", f"{STARTING}:{item}"), kind="opening",
                                       adjustment_date=book.journals[STARTING].date, item_external_id=item,
                                       quantity=qty, value=value)
                for item, (qty, value) in ledger.starting_stock.items()]
    moved = [CIFInventoryAdjustment(**_src(m.source_type, f"{m.key}:stock:{n}", m.ref), kind="adjustment",
                                    adjustment_date=m.date, item_external_id=ln.item, quantity=ln.quantity,
                                    value=ln.value)
             for m in ledger.moves for n, ln in enumerate(m.lines, 1)]
    return [*opening, *moved]


def _attachments(book: Book, screened: Screened) -> list[CIFAttachment]:
    return [CIFAttachment(**_src("Attachment", a.key), file_name=a.name, declared_content_type=a.content_type,
                          size_bytes=a.size, sha256=a.sha256, target_source_type=a.target_type,
                          target_source_external_id=a.target)
            for a in screened.accepted.values()]


def carried(book: Book, ledger: Ledger) -> set[str]:
    """Keys of the source records this migration imports under their own id: attachment targets."""
    imported = {p.record for p in ledger.imported_postings() if p.part in ("transfer", "journal")}
    return {*book.contacts, *book.items, *ledger.documents, *ledger.orders, *ledger.settlement_amounts, *imported}


def build_bundle(book: Book, ledger: Ledger, screened: Screened) -> CIFImportBundle:
    """The CIF bundle for one migration. Accepted attachments must already target imported records."""
    movements = _movements(book)
    return CIFImportBundle(
        company=_company(book),
        currencies=_currencies(book),
        locations=_locations(book),
        accounts=_accounts(book),
        tax_codes=_tax_codes(book),
        contacts=_contacts(book),
        items=_items(book),
        documents=[*(_document(book, ledger, book.documents[k], movements) for k in ledger.documents),
                   *(_order(book, ledger, book.orders[k]) for k in ledger.orders)],
        settlements=_settlements(book, ledger),
        bank_transfers=_transfers(book, ledger),
        journals=_journals(book, ledger),
        inventory_adjustments=_stock(book, ledger),
        attachments=_attachments(book, screened),
    )

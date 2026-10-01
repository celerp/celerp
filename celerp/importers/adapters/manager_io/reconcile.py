# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Source-side reconciliation expectations for a Manager migration.

Computed from the decoded book and its ledger postings only, never from the
CIF bundle, so the import is checked against figures that did not pass
through the conversion it verifies. Stock is recomputed here from the
quantities each record states, without the linked and valued movements the
conversion carries, so a mis-carried movement cannot also move the figure it
is checked against. Money is base currency unless a measure states the
record's own currency.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from celerp.importers.adapters.manager_io.book import AP, AR, INBOUND, JOURNAL_DOCUMENTS, Book
from celerp.importers.adapters.manager_io.ledger import Ledger
from celerp.importers.schema import (
    CIFTolerance,
    ReconciliationExpectation,
    ReconciliationExpectations,
    ReconciliationMeasure as M,
)
from celerp.services.money import round_money

ZERO = Decimal(0)
CONTROL_MEASURES = {"receivable": M.AR_CONTROL, "payable": M.AP_CONTROL, "tax": M.TAX_CONTROL}
CREDIT_TYPES = frozenset({"liability", "equity", "revenue"})


def _stock_from_source(book: Book) -> dict[str, tuple[Decimal, Decimal]]:
    """Quantity and base value held per item after every stock movement in the book, in the
    order stock moved, receipts first on a day: goods received carry their share of the bill
    line's net, goods delivered their share of the cost of sales the invoice line booked."""
    code = book.base_code or ""
    events: list[tuple] = []
    for key, movement in book.movements.items():
        doc = book.documents.get(movement.document) if movement.document else None
        if book.is_blocked(key) or doc is None:
            continue
        merged: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for item, quantity in movement.source_lines:
            merged[item] += quantity
        lines = [(item, quantity, next(i for i, line in enumerate(doc.lines) if line.item == item))
                 for item, quantity in merged.items()]
        events.append((movement.date, movement.source_type not in INBOUND, key, doc, lines))
    for key, doc in book.documents.items():
        if doc.moves_stock and not book.is_blocked(key):
            sign = 1 if doc.source_type in INBOUND else -1
            lines = [(line.item, sign * line.quantity, i) for i, line in enumerate(doc.lines) if line.item]
            if lines:
                events.append((doc.date, doc.source_type not in INBOUND, key, doc, lines))
    held: dict[str, tuple[Decimal, Decimal]] = {}
    taken: dict[tuple[str, int], Decimal] = defaultdict(lambda: ZERO)
    for _, _, _, doc, lines in sorted(events, key=lambda e: e[:3]):
        for item, quantity, index in lines:
            on_hand, worth = held.get(item, (ZERO, ZERO))
            source = doc.lines[index]
            booked = source.net if quantity > 0 else -source.cost
            before = taken[(doc.key, index)]
            value = (round_money(booked * (before + abs(quantity)) / source.quantity, code)
                     - round_money(booked * before / source.quantity, code))
            taken[(doc.key, index)] = before + abs(quantity)
            held[item] = (on_hand + quantity, worth + value)
    return held


def expectations_from(book: Book, ledger: Ledger) -> ReconciliationExpectations:
    """Every figure the source states about the ledger in scope and the records imported."""
    exact = CIFTolerance(kind="exact")
    base = book.base_code
    rows: list[ReconciliationExpectation] = []

    def add(measure: M, key: str, currency: str | None, expected: Decimal, label: str = "",
            credit_normal: bool = False) -> None:
        rows.append(ReconciliationExpectation(measure=measure, key=key, currency=currency, expected=expected,
                                              tolerance=exact, label=label, credit_normal=credit_normal))

    balances = ledger.balances()
    add(M.DEBITS_EQUAL_CREDITS, "", base, sum(balances.values(), ZERO))
    party: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in ledger.postings:
        if p.account in (AR, AP) and p.contact:
            party[p.contact] += p.amount
    for key, account in sorted(book.accounts.items()):
        balance = balances.get(key, ZERO)
        label = f"{account.code} {account.name}" if account.code else account.name
        credit = account.account_type in CREDIT_TYPES
        add(M.TRIAL_BALANCE, key, base, balance, label, credit)
        if account.control in CONTROL_MEASURES:
            add(CONTROL_MEASURES[account.control], key, base, balance, label, credit)
        if account.control == "bank":
            add(M.BANK_CASH, key, book.currency_code(account.currency), balance, label, credit)
    for key, contact in sorted(book.contacts.items()):
        customer = contact.source_type == "Customer"
        add(M.AR_BY_CUSTOMER if customer else M.AP_BY_SUPPLIER, key, base, party[key], contact.name, not customer)

    held = _stock_from_source(book)
    for key, item in sorted(book.items.items()):
        qty, value = held.get(key, (ZERO, ZERO))
        label = f"{item.name} ({item.code})" if item.code else item.name
        add(M.INVENTORY_QUANTITY, key, None, qty, label)
        add(M.INVENTORY_VALUE, key, base, value, label)

    counts: Counter = Counter()
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    statuses: Counter = Counter()
    for key in ledger.documents:
        doc = book.documents[key]
        if doc.source_type in JOURNAL_DOCUMENTS:
            continue
        group = (doc.doc_type, book.currency_code(doc.currency))
        counts[group] += 1
        totals[group] += doc.total
        statuses[f"{doc.doc_type}:{ledger.states[key].status}"] += 1
    for (doc_type, currency), count in sorted(counts.items()):
        add(M.DOCUMENT_COUNT, doc_type, currency, Decimal(count))
        add(M.DOCUMENT_TOTAL, doc_type, currency, totals[(doc_type, currency)])
    for key, count in sorted(statuses.items()):
        add(M.DOCUMENT_STATUS, key, None, Decimal(count))

    allocated: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for key, lines in ledger.allocations.items():
        s = book.settlements[key]
        allocated[(s.kind, book.currency_code(s.currency))] += sum((amount for _, amount in lines), ZERO)
    for (kind, currency), amount in sorted(allocated.items()):
        add(M.SETTLEMENT_ALLOCATION, kind, currency, amount)
    return ReconciliationExpectations(expectations=rows)

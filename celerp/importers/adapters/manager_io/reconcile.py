# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Source-side reconciliation expectations for a Manager migration.

Computed from the decoded book and its ledger postings only, never from the
CIF bundle, so the import is checked against figures that did not pass
through the conversion it verifies. Money is base currency unless a measure
states the record's own currency.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from celerp.importers.adapters.manager_io.book import AP, AR, JOURNAL_DOCUMENTS, Book
from celerp.importers.adapters.manager_io.ledger import Ledger, stock
from celerp.importers.schema import (
    CIFTolerance,
    ReconciliationExpectation,
    ReconciliationExpectations,
    ReconciliationMeasure as M,
)

ZERO = Decimal(0)
CONTROL_MEASURES = {"receivable": M.AR_CONTROL, "payable": M.AP_CONTROL, "tax": M.TAX_CONTROL}


def expectations_from(book: Book, ledger: Ledger) -> ReconciliationExpectations:
    """Every figure the source states about the ledger in scope and the records imported."""
    exact = CIFTolerance(kind="exact")
    base = book.base_code
    rows: list[ReconciliationExpectation] = []

    def add(measure: M, key: str, currency: str | None, expected: Decimal) -> None:
        rows.append(ReconciliationExpectation(measure=measure, key=key, currency=currency, expected=expected,
                                              tolerance=exact))

    balances = ledger.balances()
    add(M.DEBITS_EQUAL_CREDITS, "", base, sum(balances.values(), ZERO))
    party: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in ledger.postings:
        if p.account in (AR, AP) and p.contact:
            party[p.contact] += p.amount
    for key, account in sorted(book.accounts.items()):
        balance = balances.get(key, ZERO)
        add(M.TRIAL_BALANCE, key, base, balance)
        if account.control in CONTROL_MEASURES:
            add(CONTROL_MEASURES[account.control], key, base, balance)
        if account.control == "bank":
            add(M.BANK_CASH, key, book.currency_code(account.currency), balance)
    for key, contact in sorted(book.contacts.items()):
        add(M.AR_BY_CUSTOMER if contact.source_type == "Customer" else M.AP_BY_SUPPLIER, key, base, party[key])

    held = stock(ledger.postings)
    for key in sorted(book.items):
        qty, value = held.get(key, (ZERO, ZERO))
        add(M.INVENTORY_QUANTITY, key, None, qty)
        add(M.INVENTORY_VALUE, key, base, value)

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

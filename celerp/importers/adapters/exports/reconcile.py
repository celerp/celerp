# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Source-side reconciliation expectations for an export-set migration.

Taken from the source's own reports, never from the conversion: the trial balance at
the last month end and at every month end before it, the aged receivables and payables
per contact, the stock on hand per item where the source states it, and the invoices and
bills list's own totals, statuses and amount due on each document. The
ledger decides only which documents the migration imports and the rate each was booked
at, so an import is checked against figures that did not pass through it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from celerp.importers.adapters.exports.book import CREDIT_TYPES, ZERO, Book, Document
from celerp.importers.adapters.exports.ledger import Ledger
from celerp.importers.schema import (
    CIFTolerance,
    ReconciliationExpectation,
    ReconciliationExpectations,
    ReconciliationMeasure as M,
)
from celerp.services.money import round_money

CONTROL_MEASURES = {"receivable": M.AR_CONTROL, "payable": M.AP_CONTROL, "tax": M.TAX_CONTROL}
# Document types Celerp carries as a journal on the document they apply to, not as documents.
JOURNAL_DOCUMENTS = frozenset({"debit_note"})
# Sign of a document's amount due in the open balance of its side.
OPEN_SIGN = {"invoice": 1, "credit_note": -1, "bill": 1, "debit_note": -1}


def _status(doc: Document) -> str:
    if doc.status != "posted":
        return doc.status
    return "paid" if doc.amount_due == 0 else "awaiting_payment"


def expectations_from(book: Book, ledger: Ledger) -> ReconciliationExpectations:
    """Every figure the source's reports state about the books after the migration."""
    exact = CIFTolerance(kind="exact")
    base = book.base
    rows: list[ReconciliationExpectation] = []

    def add(measure: M, key: str, currency: str | None, expected: Decimal, label: str = "",
            credit_normal: bool = False) -> None:
        rows.append(ReconciliationExpectation(measure=measure, key=key, currency=currency, expected=expected,
                                              tolerance=exact, label=label, credit_normal=credit_normal))

    end = book.trial_balances[max(book.trial_balances)] if book.trial_balances else {}
    add(M.DEBITS_EQUAL_CREDITS, "", base, sum(end.values(), ZERO))
    for code, account in sorted(book.accounts.items()):
        balance = end.get(code, ZERO)
        credit = account.account_type in CREDIT_TYPES
        add(M.TRIAL_BALANCE, code, base, balance, account.label, credit)
        if account.control in CONTROL_MEASURES:
            add(CONTROL_MEASURES[account.control], code, base, balance, account.label, credit)
        if account.control == "bank":
            add(M.BANK_CASH, code, base, balance, account.label, credit)
    for on, balances in sorted(book.trial_balances.items()):
        if ledger.cutover is not None and on < ledger.cutover:
            continue
        for code, account in sorted(book.accounts.items()):
            add(M.TRIAL_BALANCE_AT, f"{code}@{on.isoformat()}", base, balances.get(code, ZERO),
                f"{account.label}, {on.isoformat()}", account.account_type in CREDIT_TYPES)

    for side, measure, role in (("receivable", M.AR_BY_CUSTOMER, "customer"),
                                ("payable", M.AP_BY_SUPPLIER, "supplier")):
        owed = book.aged.get(side, (None, {}))[1]
        for name, contact in sorted(book.contacts.items()):
            if role in contact.roles:
                add(measure, name, base, owed.get(name, ZERO), name, side == "payable")

    for code, (quantity, value) in sorted(book.stock.items()):
        label = book.items[code].name
        add(M.INVENTORY_QUANTITY, code, None, quantity, label)
        if value is not None:
            add(M.INVENTORY_VALUE, code, base, value, label)

    open_side: dict[bool, Decimal] = defaultdict(lambda: ZERO)
    counts: Counter = Counter()
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    statuses: Counter = Counter()
    owed: list[Document] = []
    for key in ledger.documents:
        doc = book.documents[key]
        if doc.status == "posted":
            open_side[doc.sales] += OPEN_SIGN[doc.doc_type] * round_money(doc.amount_due * (doc.rate or 1), base)
        if doc.doc_type in JOURNAL_DOCUMENTS:
            continue
        counts[(doc.doc_type, doc.currency)] += 1
        totals[(doc.doc_type, doc.currency)] += doc.total
        statuses[f"{doc.doc_type}:{_status(doc)}"] += 1
        if doc.status == "posted":
            owed.append(doc)
    ar = sum((end.get(code, ZERO) for code in book.control("receivable")), ZERO)
    ap = sum((end.get(code, ZERO) for code in book.control("payable")), ZERO)
    add(M.AR_DOCUMENT_RESIDUAL, "", base, ar - open_side[True])
    add(M.AP_DOCUMENT_RESIDUAL, "", base, ap + open_side[False])
    for (doc_type, currency), count in sorted(counts.items()):
        add(M.DOCUMENT_COUNT, doc_type, currency, Decimal(count))
        add(M.DOCUMENT_TOTAL, doc_type, currency, totals[(doc_type, currency)])
    for key, count in sorted(statuses.items()):
        add(M.DOCUMENT_STATUS, key, None, Decimal(count))
    # Each document on its own, so a wrong open amount on one cannot hide in the totals.
    for doc in owed:
        add(M.DOCUMENT_RESIDUAL, doc.key, doc.currency, doc.amount_due, doc.number)
    return ReconciliationExpectations(expectations=rows)

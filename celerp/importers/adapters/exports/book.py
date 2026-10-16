# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""One company's books as an export set states them, in a form shared by every source.

A source reader fills a `Book` from its files: the chart, contacts, items, tax codes,
the invoices and bills, the general ledger's journals, the trial balance at each
month end and the aged balances per contact. The ledger, the bundle and the
reconciliation read only this model, so a second source differs only in how it
reads its own files.

Amounts are Decimal. Ledger amounts are base currency, debit positive; `foreign` is
the same line in the journal's currency when the source states one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

ZERO = Decimal(0)
SALES = frozenset({"invoice", "credit_note"})
# Sign of the customer or supplier balance posting: an invoice and a debit note raise a debit there.
PARTY_SIGN = {"invoice": 1, "credit_note": -1, "bill": -1, "debit_note": 1}
CREDIT_TYPES = frozenset({"liability", "equity", "revenue"})
PROFIT_AND_LOSS = frozenset({"revenue", "expense", "cogs"})


@dataclass
class Account:
    code: str                                   # the account's key: sources key ledger lines by code
    name: str
    account_type: str
    control: str | None = None
    currency: str | None = None                 # a bank account kept in a foreign currency
    active: bool = True

    @property
    def label(self) -> str:
        return f"{self.code} {self.name}"


@dataclass
class Contact:
    name: str                                   # the contact's key: the ledger and documents name it
    roles: set[str] = field(default_factory=set)
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tax_id: str | None = None
    currency: str | None = None
    listed: bool = True                         # False: named by a record, absent from the contact list


@dataclass
class Item:
    code: str
    name: str
    description: str | None = None
    price: Decimal | None = None
    active: bool = True


@dataclass
class TaxCode:
    name: str
    rate: Decimal
    account: str | None = None


@dataclass
class DocLine:
    item: str | None
    description: str
    quantity: Decimal
    unit_price: Decimal
    discount: Decimal | None
    net: Decimal                                # tax exclusive, document currency
    tax: Decimal
    tax_code: str | None
    account: str
    tracking: dict[str, str] = field(default_factory=dict)


@dataclass
class Document:
    key: str                                    # S:number or P:number, |contact when numbers collide
    number: str
    doc_type: str                               # invoice, credit_note, bill, debit_note
    source_type: str                            # the source's name for the kind of record
    contact: str
    reference: str
    date: date
    due: date | None
    currency: str
    status: str                                 # posted, void or draft
    lines: list[DocLine]
    total: Decimal
    tax_total: Decimal
    paid: Decimal                               # as the export states it
    amount_due: Decimal                         # as the export states it
    file: str = ""
    rate: Decimal | None = None                 # base per document unit, from its ledger journal
    applies_to: str | None = None
    journal: str | None = None                  # the ledger journal that posts it

    @property
    def sales(self) -> bool:
        return self.doc_type in SALES


@dataclass
class GLLine:
    account: str
    amount: Decimal                             # base, debit positive
    foreign: Decimal | None                     # the same line in the journal's currency, when stated
    description: str
    tracking: dict[str, str] = field(default_factory=dict)


@dataclass
class GLJournal:
    key: str                                    # J:<journal number>
    number: str
    date: date
    source: str                                 # the source's own kind of journal
    reference: str
    invoice: str
    contact: str
    currency: str
    lines: list[GLLine]


@dataclass
class Book:
    source_system: str
    company_name: str = ""
    base: str = ""
    accounts: dict[str, Account] = field(default_factory=dict)
    contacts: dict[str, Contact] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    tax_codes: dict[str, TaxCode] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)
    journals: list[GLJournal] = field(default_factory=list)
    # Month end -> account code -> balance from the first record, debit positive.
    trial_balances: dict[date, dict[str, Decimal]] = field(default_factory=dict)
    # receivable / payable -> (as-at date, contact -> base balance, debit positive)
    aged: dict[str, tuple[date, dict[str, Decimal]]] = field(default_factory=dict)
    skipped: Counter = field(default_factory=Counter)       # coverage label -> count of records not carried
    tracking: set[str] = field(default_factory=set)          # tracking category names
    object_counts: Counter = field(default_factory=Counter)

    @property
    def currencies(self) -> tuple[str, ...]:
        found = {self.base, *(d.currency for d in self.documents.values()), *(j.currency for j in self.journals),
                 *(a.currency for a in self.accounts.values() if a.currency)}
        return tuple(sorted(found - {""}))

    def is_foreign(self, currency: str | None) -> bool:
        return bool(currency) and currency != self.base

    def control(self, kind: str) -> list[str]:
        return [a.code for a in self.accounts.values() if a.control == kind]

    @property
    def fx_account(self) -> str | None:
        return next(iter(self.control("exchange_difference")), None)

    @property
    def tax_account(self) -> str | None:
        return next(iter(self.control("tax")), None)

    def tax_account_for(self, code: str | None) -> str | None:
        """Where a line's tax posts: its tax rate's account, else the tax control account."""
        tax = self.tax_codes.get(code or "")
        return (tax.account if tax else None) or self.tax_account

    def party_account(self, doc_type: str) -> str:
        return self.control("receivable" if doc_type in SALES else "payable")[0]

    def contact(self, name: str) -> Contact:
        """The named contact, added when a record names one the contact list leaves out."""
        if name not in self.contacts:
            self.contacts[name] = Contact(name, listed=False)
        return self.contacts[name]

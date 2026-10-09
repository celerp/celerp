# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What a document still owes and when it is overdue: one definition for the document list and
its cards, the dashboard, the aging reports, bulk payment and the overdue reminder.

A document with no recorded balance owes its total, as the payment projection starts it; a
recorded 0 stays 0. Imported documents carry their displayed values under older keys
(``DOC_FIELD_FALLBACKS``), which count the same as the current ones."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from celerp.services.money import round_money, to_decimal

# Older keys a document may carry a displayed value under (imported documents store their
# number, dates and amounts this way). A field that is missing or empty is read from its first
# non-empty older key. A stored 0 is a value, not a gap.
DOC_FIELD_FALLBACKS: dict[str, tuple[str, ...]] = {
    "doc_number": ("ref", "ref_id"),
    "contact_name": ("contact_id", "contact_external_id"),
    "issue_date": ("created_at",),
    "due_date": ("payment_due_date",),
    "total": ("total_amount",),
    "amount_outstanding": ("outstanding_balance",),
}

# Document type spellings older imports carry, by the type they are.
_DOC_TYPE_ALIASES: dict[str, str] = {"Invoice": "invoice", "PO": "purchase_order"}


def canonical_doc_type(doc_type: str) -> str:
    """``doc_type`` under its current name, so an imported document counts as the type it is."""
    return _DOC_TYPE_ALIASES.get(doc_type, doc_type)


# Memo statuses in which goods are still out with the customer.
MEMO_LIVE_STATUSES: frozenset[str] = frozenset({"sent", "final", "partial", "received", "partially_received", "partial_returned"})

# Statuses in which an issued sales document (invoice, proforma, memo) still awaits payment.
_RECEIVABLE_AWAITING: frozenset[str] = frozenset({"final", "sent", "awaiting_payment", "partial"})

# Per-doc-type statuses in which an issued document still awaits payment.
AWAITING_PAYMENT_STATUSES: dict[str, frozenset[str]] = {
    "invoice": _RECEIVABLE_AWAITING,
    "proforma": _RECEIVABLE_AWAITING,
    "memo": _RECEIVABLE_AWAITING,
    "bill": frozenset({"final", "awaiting_payment", "partial", "received", "partially_received",
                       "partial_returned", "returned"}),
    "purchase_order": frozenset({"sent", "final", "awaiting_payment", "partial", "received", "partially_received",
                                 "partial_returned", "returned"}),
}

# Per-doc-type statuses in which a past due date means the document is overdue: an invoice
# or bill still awaiting payment, a memo with goods still out, a consignment still holding
# goods. Draft, void, paid, closed, converted and fully returned documents never are.
OVERDUE_STATUSES: dict[str, frozenset[str]] = {
    "invoice": AWAITING_PAYMENT_STATUSES["invoice"],
    "bill": AWAITING_PAYMENT_STATUSES["bill"],
    "memo": MEMO_LIVE_STATUSES,
    "consignment_in": frozenset({"final", "received", "partially_received", "partial_returned"}),
}

# Doc types whose overdue state also needs an unpaid balance.
_BALANCE_DOC_TYPES: frozenset[str] = frozenset({"invoice", "bill"})


def today_iso() -> str:
    """Today's date (server-local, ISO) that due dates are compared against."""
    return date.today().isoformat()


def doc_value(state: dict, field: str):
    """The value a document shows for ``field``: the field itself, else its first non-empty older
    key (``DOC_FIELD_FALLBACKS``)."""
    value = state.get(field)
    if value in (None, ""):
        value = next((state[k] for k in DOC_FIELD_FALLBACKS.get(field, ()) if state.get(k) not in (None, "")), value)
    return value


def outstanding_balance(state: dict) -> Decimal | None:
    """What a document still owes: its recorded balance, else its total when no balance is
    recorded, else 0. None when the value is not a number, so it is never counted as 0."""
    balance = doc_value(state, "amount_outstanding")
    if balance in (None, ""):
        balance = doc_value(state, "total")
    try:
        amount = to_decimal(balance or 0)
    except (ArithmeticError, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def is_owed(state: dict) -> bool:
    """Whether a document's balance is above zero at its currency's precision, the rule the
    payment projection marks a document paid by."""
    balance = outstanding_balance(state)
    return balance is not None and round_money(balance, str(state.get("currency") or "USD")) > 0


def is_awaiting_payment(doc_type: str | None, status: str | None) -> bool:
    """Whether a document of ``doc_type`` in ``status`` is issued and still awaits payment."""
    return status in AWAITING_PAYMENT_STATUSES.get(doc_type, ())


def awaiting_status_param(doc_type: str) -> str:
    """The ``status_in`` list-filter value that lists the documents of ``doc_type`` awaiting
    payment."""
    return ",".join(sorted(AWAITING_PAYMENT_STATUSES[doc_type]))


def is_overdue_document(state: dict, today: str) -> bool:
    """Whether a document is overdue on ``today`` (ISO date): due strictly before today, in a live
    status of its type (``OVERDUE_STATUSES``), and for an invoice or bill still owed
    (``is_owed``)."""
    doc_type = state.get("doc_type")
    due = doc_value(state, "due_date")
    if not due or str(due) >= today or state.get("status") not in OVERDUE_STATUSES.get(doc_type, ()):
        return False
    return doc_type not in _BALANCE_DOC_TYPES or is_owed(state)

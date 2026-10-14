# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Single source of truth for list-type behaviour — the Open-Closed seam for lists.

Every list type (quotation, transfer, audit, and shipping later) shares ONE lifecycle:

    draft  --finalize-->  finalized  --terminal action-->  closed        (or --> void)

What differs per type is declared HERE as pure data (no rendering, no DB), so the backend
(`celerp_docs`) and the UI (`ui`) both read the same table and can never drift. Adding a new
type is one row here + a print template (UI) + any terminal-action body (backend) — nothing
else in the lifecycle, filters, or badges changes (GDR 2h / SOLID Open-Closed).
"""
from __future__ import annotations

from dataclasses import dataclass

# Uniform status spine for ALL list types. The terminal outcome lives in `result`;
# intra-finalized milestones (quote sent/accepted, transfer issued) are timestamps.
DRAFT, FINALIZED, CLOSED, VOID = "draft", "finalized", "closed", "void"
LIST_STATUSES = (DRAFT, FINALIZED, CLOSED, VOID)

DEFAULT_LIST_TYPE = "quotation"


@dataclass(frozen=True)
class TerminalAction:
    """A button shown on a finalized list that closes it (or, for audit, can be undone)."""
    key: str            # url action segment, e.g. "convert-invoice", "adjust", "receive"
    label: str          # button text
    result: str         # value written to `result` on close (e.g. "converted", "stock_adjusted")
    confirm: str = ""   # hx_confirm text ("" = no confirm)
    manager: bool = False  # require manager/owner (else operator)


@dataclass(frozen=True)
class ListBehavior:
    label: str                              # display name, e.g. "Quotation"
    money: bool                             # show price/discount/tax/total columns
    extra_columns: tuple[str, ...] = ()     # appended after qty, e.g. ("on_hand", "counted")
    finalize_label: str = "Finalize"        # the draft -> finalized button
    finalize_milestone: str | None = None   # timestamp set on finalize, or "freeze_onhand" sentinel
    terminal: tuple[TerminalAction, ...] = ()
    scan_finalized: str = "noop"            # scan behaviour once finalized: count | receive | noop
    scan_quantity: str = "stock"            # what a scanned line takes (ScanBehavior.quantity)
    customs: bool = False                   # value-bearing but non-accounting: per-line customs
                                            # value/HS/origin columns, declared total, no disc/tax


# THE table. One row per type — the only place a type's behaviour is declared.
LIST_BEHAVIOR: dict[str, ListBehavior] = {
    "quotation": ListBehavior(
        label="Quotation", money=True, finalize_label="Issue", finalize_milestone=None,
        # Issue just locks the draft; Send / Mark-as-sent set the sent milestone afterwards (like an
        # invoice). Convert (to invoice/memo) is the closing terminal.
        terminal=(
            TerminalAction("convert-invoice", "Convert to invoice", "converted"),
            TerminalAction("convert-memo", "Convert to memo", "converted"),
        ),
        scan_finalized="noop", scan_quantity="stock_or_one",  # as a quotation document
    ),
    "transfer": ListBehavior(
        label="Transfer", money=False, finalize_label="Issue", finalize_milestone="issued_at",
        # No closing terminal: a finalized transfer's action is "Move all to <location>", which moves
        # stock and stays finalized (repeatable). Handled in the UI/route, not as a close.
        terminal=(),
        scan_finalized="noop",
    ),
    "audit": ListBehavior(
        label="Audit", money=False, extra_columns=("on_hand", "counted"),
        finalize_label="Finalize count", finalize_milestone="freeze_onhand",
        terminal=(TerminalAction(
            "adjust", "Adjust stock", "stock_adjusted",
            confirm="Apply the counted quantities to stock? This posts a journal entry.",
            manager=True),),
        scan_finalized="count", scan_quantity="snapshot",
    ),
    "writeoff": ListBehavior(
        # Event-based (enter the known quantity leaving stock per line), vs audit's count-based
        # reconciliation. Each line names its destination expense/drawings account and a comment;
        # the terminal carves/disposes the stock and posts one journal entry.
        label="Write-off", money=False, extra_columns=("qty_out", "account", "comment"),
        finalize_label="Finalize", finalize_milestone=None,
        terminal=(TerminalAction(
            "write-off", "Write off stock", "written_off",
            confirm="Remove the listed quantities from stock? This posts a journal entry.",
            manager=True),),
        scan_finalized="noop",
    ),
    "shipping_doc": ListBehavior(
        # One shipment record, two printouts: the Delivery Note (no prices, for the receiver)
        # and the Commercial Invoice (customs values + declaration, for cross-border carriers).
        label="Shipping Document", money=False, customs=True, finalize_label="Issue",
        finalize_milestone="issued_at", terminal=(), scan_finalized="noop",
    ),
}

LIST_TYPES: tuple[str, ...] = tuple(LIST_BEHAVIOR.keys())

# Result values that close a list and name its badge; any other result reads as closed.
_RESULT_STATUSES = frozenset({"converted", "stock_adjusted", "received", "expired", "written_off"})


def behavior(list_type: str | None) -> ListBehavior:
    return LIST_BEHAVIOR.get(list_type or DEFAULT_LIST_TYPE, LIST_BEHAVIOR[DEFAULT_LIST_TYPE])


def is_money_list(list_type: str | None) -> bool:
    return behavior(list_type).money


@dataclass(frozen=True)
class ScanBehavior:
    """What one scan does on a record, for the one scan primitive (lists and documents)."""
    finalized: str  # once past draft: count (check off a locked manifest) | noop (scanning refused)
    priced: bool    # added lines carry a unit price from the chosen price list
    lots: str       # how a product code (GTIN/SKU) picks its lot: single | consolidate | sold
    # What a scanned lot's line takes, and what a repeat scan of it does:
    #   stock         its on-hand; nothing on hand is refused; a repeat is reported as already there
    #   stock_or_one  as stock, but nothing on hand takes one and each repeat counts one more
    #   count         one unit per scan, stocked or not; each repeat counts one more on its line
    #   snapshot      its on-hand as it stands, zero included (a stock count)
    # An item kept in no stock (a service) counts one per scan except under snapshot, and an RFID
    # tag names one piece, so its repeat is always reported.
    quantity: str = "stock"


# The record field holding its recent scan runs, so a retried run replays instead of re-adding.
# It belongs to the record it was scanned on: a conversion or a copy starts without it.
SCAN_RUNS_FIELD = "scan_runs"

# Every document type's scan; a type not listed scans as a sale. A shipping document is a list
# type and scans through LIST_BEHAVIOR.
_DOC_SCAN_SALE = ScanBehavior(finalized="noop", priced=True, lots="consolidate")
_DOC_SCAN_BUY = ScanBehavior(finalized="noop", priced=True, lots="consolidate", quantity="count")
DOC_SCAN_BEHAVIOR: dict[str, ScanBehavior] = {
    # Goods sold or taken out of stock: a lot takes what is on hand, once.
    "invoice": _DOC_SCAN_SALE,
    "memo": _DOC_SCAN_SALE,
    "proforma": _DOC_SCAN_SALE,
    "subscription_invoice": _DOC_SCAN_SALE,
    "debit_note": _DOC_SCAN_SALE,
    "production_order": _DOC_SCAN_SALE,
    "credit_note": ScanBehavior(finalized="noop", priced=True, lots="sold"),  # a return takes the sold lot
    # A quote may offer goods not yet in stock.
    "quotation": ScanBehavior(finalized="noop", priced=True, lots="consolidate", quantity="stock_or_one"),
    # Goods being bought in: each scan is one more unit.
    "purchase_order": _DOC_SCAN_BUY,
    "subscription_po": _DOC_SCAN_BUY,
    "bill": _DOC_SCAN_BUY,
    "consignment_in": _DOC_SCAN_BUY,
}


def scan_behavior(kind: str, doc_type: str | None) -> ScanBehavior:
    """The scan behaviour of a list (kind "lists", by list_type) or a document (kind "docs")."""
    if kind == "lists":
        b = behavior(doc_type)
        return ScanBehavior(finalized=b.scan_finalized, priced=b.money, lots="single", quantity=b.scan_quantity)
    return DOC_SCAN_BEHAVIOR.get(doc_type or "", _DOC_SCAN_SALE)


def terminal_action(list_type: str | None, key: str) -> TerminalAction | None:
    return next((a for a in behavior(list_type).terminal if a.key == key), None)


def status_key(state: dict) -> str:
    """The badge status token for a list, from (status, list_type, result, milestones).

    Recovers the per-type richness the flattened spine drops: a finalized quotation reads
    "sent" or "accepted", a finalized audit "counting". The UI renders the token through
    the ``enum.doc_status`` catalog, like a document's status.
    """
    status = state.get("status") or DRAFT
    if status in (DRAFT, VOID):
        return status
    if status == CLOSED:
        result = state.get("result") or ""
        return result if result in _RESULT_STATUSES else CLOSED
    # finalized: interpret per type / milestone
    lt = state.get("list_type") or DEFAULT_LIST_TYPE
    if lt == "audit":
        return "counting"
    if lt == "transfer":
        return "in_transit"
    if lt == "shipping_doc":
        return "issued"
    if lt == "quotation":
        if state.get("accepted_at"):
            return "accepted"
        return "sent" if state.get("sent_at") else "issued"
    return FINALIZED

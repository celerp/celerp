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
    scan_unstocked: str = "refuse"          # a scanned lot with nothing on hand (ScanBehavior.unstocked)
    scan_sell_qty: bool = False             # a scan places the item's sell quantity (ScanBehavior.sell_qty)
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
        scan_finalized="noop", scan_unstocked="one", scan_sell_qty=True,  # as a quotation document
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
        scan_finalized="count", scan_unstocked="snapshot",
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
    priced: bool    # added lines carry a unit price (pricing.line_price_list: cost on a purchase document)
    lots: str       # how a product code (GTIN/SKU) picks its lot: single | consolidate | sold
    # A splittable lot or an item kept in no stock (a service) raises its line by one per scan (by
    # its sell quantity where `sell_qty`), a lot that may not be split takes all it has on hand. A scanned lot by `unstocked`:
    #   refuse    nothing on hand is reported, and no line takes more than is on hand (goods
    #             leaving stock)
    #   one       nothing on hand is taken at one (goods bought in, or offered before they are
    #             stocked)
    #   snapshot  every lot takes its on-hand as it stands, zero included (a stock count)
    unstocked: str = "refuse"
    # A repeat of a lot that may not be split, by `repeats`: refuse reports it as already on the
    # record (it is one physical lot), append adds it as another line (another lot bought in). An
    # RFID tag from an earlier run names the one piece already there, so it is always reported.
    repeats: str = "refuse"
    # Goods sold or offered: a scan of a splittable lot places the item's sell quantity
    # (line_measures.proposed_sell_qty) instead of one. RFID tags still name one piece.
    sell_qty: bool = False


# The record field holding its recent scan runs, so a retried run replays instead of re-adding.
# It belongs to the record it was scanned on: a conversion or a copy starts without it.
SCAN_RUNS_FIELD = "scan_runs"

# Document types whose counterparty is a supplier, the one classification of a document's
# side (tax_regimes.doc_side): its contact is a vendor, a line added to one is priced at what
# the goods cost, and it takes the purchasing taxes. Every other document is on the sales side
# and its contact is a customer.
SUPPLIER_DOC_TYPES: frozenset[str] = frozenset({"purchase_order", "subscription_po", "bill", "consignment_in",
                                                "debit_note"})

# Supplier documents that buy goods in; a supplier credit note sends them back, so it scans
# as goods going out.
PURCHASE_DOC_TYPES: frozenset[str] = SUPPLIER_DOC_TYPES - {"debit_note"}

# Every document type's scan; a type not listed scans as a sale. A shipping document is a list
# type and scans through LIST_BEHAVIOR.
_DOC_SCAN_SALE = ScanBehavior(finalized="noop", priced=True, lots="consolidate", sell_qty=True)
_DOC_SCAN_BUY = ScanBehavior(finalized="noop", priced=True, lots="consolidate", unstocked="one", repeats="append")
DOC_SCAN_BEHAVIOR: dict[str, ScanBehavior] = {
    # Goods sold or taken out of stock: a lot must be on hand, and a lot that may not be split
    # is on the document once.
    "invoice": _DOC_SCAN_SALE,
    "memo": _DOC_SCAN_SALE,
    "proforma": _DOC_SCAN_SALE,
    "subscription_invoice": _DOC_SCAN_SALE,
    "debit_note": _DOC_SCAN_SALE,
    "production_order": _DOC_SCAN_SALE,
    "credit_note": ScanBehavior(finalized="noop", priced=True, lots="sold"),  # a return takes the sold lot
    # A quote may offer goods not yet in stock.
    "quotation": ScanBehavior(finalized="noop", priced=True, lots="consolidate", unstocked="one", sell_qty=True),
    # Goods being bought in: nothing need be on hand, and a repeat of a lot that may not be split
    # is another lot.
    **dict.fromkeys(sorted(PURCHASE_DOC_TYPES), _DOC_SCAN_BUY),
}


def scan_behavior(kind: str, doc_type: str | None) -> ScanBehavior:
    """The scan behaviour of a list (kind "lists", by list_type) or a document (kind "docs")."""
    if kind == "lists":
        b = behavior(doc_type)
        return ScanBehavior(finalized=b.scan_finalized, priced=b.money, lots="single", unstocked=b.scan_unstocked,
                            sell_qty=b.scan_sell_qty)
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

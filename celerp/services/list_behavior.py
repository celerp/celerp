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
        scan_finalized="noop",
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
        scan_finalized="count",
    ),
    "delivery_note": ListBehavior(
        label="Delivery Note", money=False, finalize_label="Issue",
        finalize_milestone="issued_at", terminal=(), scan_finalized="noop",
    ),
}

LIST_TYPES: tuple[str, ...] = tuple(LIST_BEHAVIOR.keys())

# Result values that close a list, mapped to their badge label (single source for status_label).
_RESULT_LABELS = {
    "converted": "Converted",
    "stock_adjusted": "Adjusted",
    "received": "Received",
    "expired": "Expired",
}


def behavior(list_type: str | None) -> ListBehavior:
    return LIST_BEHAVIOR.get(list_type or DEFAULT_LIST_TYPE, LIST_BEHAVIOR[DEFAULT_LIST_TYPE])


def is_money_list(list_type: str | None) -> bool:
    return behavior(list_type).money


def terminal_action(list_type: str | None, key: str) -> TerminalAction | None:
    return next((a for a in behavior(list_type).terminal if a.key == key), None)


def status_label(state: dict) -> str:
    """Human badge text from (status, list_type, result, milestones) — the one DRY renderer.

    Recovers the per-type richness the flattened spine drops (GDR debuggability): a finalized
    quotation reads "Sent"/"Accepted", a finalized audit reads "Counting", etc.
    """
    status = state.get("status") or DRAFT
    if status == DRAFT:
        return "Draft"
    if status == VOID:
        return "Void"
    if status == CLOSED:
        return _RESULT_LABELS.get(state.get("result") or "", "Closed")
    # finalized — interpret per type / milestone
    lt = state.get("list_type") or DEFAULT_LIST_TYPE
    if lt == "audit":
        return "Counting"
    if lt == "transfer":
        return "In transit"
    if lt == "quotation":
        if state.get("accepted_at"):
            return "Accepted"
        return "Sent" if state.get("sent_at") else "Issued"
    return "Finalized"

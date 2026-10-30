# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What a stored document or production run can still move, read from its persisted state.

Core owns this so the meaning of stored records never depends on which modules are running:
a check across modules (a units change) reads a company's documents and runs the same way
whether Documents and Manufacturing are loaded or not. The two modules act on their records
through these same definitions.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from celerp.services.doc_balance import MEMO_LIVE_STATUSES

# A quantity smaller than this is nothing left to move.
EPS = 1e-9

# Per-doc-type allowlist: maps doc_type → set of statuses where fulfill-lines is permitted.
# Only doc types listed here support the fulfill-lines / revert-lines endpoints.
# Adding a new status requires an explicit decision per doc type (true-predicate design).
# Inbound doc types (bill, consignment_in) are intentionally excluded: receiving goods
# is handled by POST /receive (creates parcels). fulfill-lines is outbound-only.
FULFILLABLE_STATUSES: dict[str, frozenset[str]] = {
    "memo":    MEMO_LIVE_STATUSES,
    "invoice": frozenset({"sent", "final", "partial", "paid", "awaiting_payment"}),
}

# Per-doc-type allowlist of the statuses POST /receive takes goods in. An issued purchase
# document receives in every status its payments and earlier receipts or returns can move it
# to; a void, closed or converted one receives nothing. A purchase order may receive while
# still a draft (its receipt books its own entry), and so may a consignment (consigned goods
# are not owned, so their receipt books nothing); a draft bill has not been issued, so it
# receives nothing. The finalized view offers Receive Goods from the same map.
_RECEIVING_STATUSES = frozenset({
    "final", "sent", "awaiting_payment", "partial", "paid",
    "received", "partially_received", "partial_returned", "returned",
})
RECEIVABLE_STATUSES: dict[str, frozenset[str]] = {
    "bill": _RECEIVING_STATUSES,
    "consignment_in": _RECEIVING_STATUSES | {"draft"},
    "purchase_order": _RECEIVING_STATUSES | {"draft"},
}

# A quotation converts in every status but these.
QUOTATION_SPENT_STATUSES = frozenset({"void", "converted"})

# Doc types that are subscription templates (not fulfillable, not part of normal doc counters).
# These are recurring template docs - they should never show a fulfill button.
TEMPLATE_DOC_TYPES: frozenset[str] = frozenset({"subscription_invoice", "subscription_po"})

# The run statuses still needing attention: a completed or cancelled run moves nothing more.
INCOMPLETE_RUN_STATUSES = frozenset({"planned", "in_progress", "on_hold"})


def template_label(state: dict, entity_id: str = "") -> str:
    """A subscription template's name as the subscriptions list shows it: its reference, else its
    number, else its name, else the last part of its id. A template made with New subscription
    has a reference and no name."""
    return state.get("ref_id") or state.get("doc_number") or state.get("name") or entity_id.split(":")[-1]


def sent_out_by(item_state: dict, doc_id: str) -> bool:
    """Whether document ``doc_id`` sent these goods out (sold or out on memo): it
    fulfilled them, or holds them as their status document."""
    return item_state.get("status") in ("sold", "memo_out") and (
        doc_id in (item_state.get("fulfilled_for_docs") or []) or item_state.get("status_doc_id") == doc_id)


def doc_still_moving(state: dict, shipped: Callable[[dict], bool]) -> list[tuple[dict, float]]:
    """Each line of a document with the quantity on it that can still be received, shipped or
    carried into a converted document. A draft and an open quotation carry their whole lines, since
    converting a quotation copies them. A document that receives carries what each line has not
    received yet; one that ships carries the lines whose goods have not gone out (``shipped``),
    and nothing once all of them have. A subscription template carries its whole lines until it
    is cancelled, since it keeps making documents from them. What came in or went out, even if it
    can still be sent back or converted (a memo bills what is out on it, a consignment buys what
    was kept), sits in its item, so a document that can only do that carries nothing. Any other
    document, a converted one among them, is history."""
    doc_type, status = state.get("doc_type"), state.get("status")
    lines = [line for line in state.get("line_items") or [] if isinstance(line, dict)]
    if (doc_type == "quotation" and status not in QUOTATION_SPENT_STATUSES) \
            or (doc_type in TEMPLATE_DOC_TYPES and status != "cancelled"):
        return [(line, line.get("quantity")) for line in lines]
    if status in RECEIVABLE_STATUSES.get(doc_type, ()):
        return [(line, left) for line in lines if (left := _unreceived(line)) > 0]
    if status in FULFILLABLE_STATUSES.get(doc_type, ()):
        if state.get("fulfillment_status") == "fulfilled":
            return []
        return [(line, line.get("quantity")) for line in lines if not shipped(line)]
    if status in (None, "draft"):
        return [(line, line.get("quantity")) for line in lines]
    return []


def _unreceived(line: dict) -> float:
    ordered, received = (Decimal(str(float(line.get(key) or 0))) for key in ("quantity", "quantity_received"))
    return float(ordered - received)


def outstanding_inputs(run_state: dict) -> list[dict]:
    """Per input, the quantity still to issue (required - already issued)."""
    out = []
    for inp in run_state.get("inputs", []):
        rem = float(inp.get("quantity") or 0) - float(inp.get("issued_qty") or 0)
        if rem > EPS:
            out.append({"item_id": inp.get("item_id"), "quantity": round(rem, 6)})
    return out


def outstanding_output(run_state: dict) -> float:
    """Finished-goods quantity still to receive (expected - already received)."""
    expected = float((run_state.get("expected_outputs") or [{}])[0].get("quantity") or 0)
    return max(0.0, round(expected - float(run_state.get("received_qty") or 0), 6))


def run_still_moving(run_state: dict) -> list[tuple[dict, float]]:
    """What an open run can still move, as (line naming its item, quantity): each component's
    quantity still to issue and the output still to receive. A completed or cancelled run
    moves nothing more."""
    if run_state.get("status") not in INCOMPLETE_RUN_STATUSES:
        return []
    lines = [({"item_id": i["item_id"]}, i["quantity"]) for i in outstanding_inputs(run_state)]
    if (left := outstanding_output(run_state)) > EPS:
        lines.append(({"item_id": run_state.get("output_item_id")}, left))
    return lines


def run_name(state: dict) -> str:
    """A run as the user knows it: by the product it makes (the production queue's label)."""
    out = (state.get("expected_outputs") or [{}])[0]
    return out.get("sku") or out.get("name") or state.get("description") or "a production run"

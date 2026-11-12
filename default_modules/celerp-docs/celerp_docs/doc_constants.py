# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Shared constants for the docs module."""

from celerp.services.list_behavior import SCAN_RUNS_FIELD
from celerp.services.record_quantities import FULFILLABLE_STATUSES

# The statuses each outbound document type ships in (FULFILLABLE_STATUSES), POST /receive takes
# goods in (RECEIVABLE_STATUSES) and a quotation converts in (QUOTATION_SPENT_STATUSES), with what
# a stored document can still move, live in core (celerp.services.record_quantities): they give
# stored documents their meaning whether or not this module is running.

# Per-doc-type allowlist for the ledger-neutral reserve-lines action ("Set as reserved" and
# "Set as available" on a reserved line). Reserve never draws stock or posts COGS, so it is
# permitted on the same customer-facing outbound docs and live statuses as Set-as-shipped - it is
# a distinct named map (the reserve wrapper gates on it) that today mirrors the fulfillable set.
# List docs reserve via a separate list-type predicate in routes.py, never through this map.
RESERVABLE_DOC_STATUSES: dict[str, frozenset[str]] = dict(FULFILLABLE_STATUSES)

# Per-doc-type allowlist for revert-lines (Set as available on shipped lines): the statuses
# in which goods this document shipped may be taken back. Draft, void, closed and converted
# documents are terminal for it. A distinct named map, today equal to the fulfillable set.
REVERTIBLE_STATUSES: dict[str, frozenset[str]] = dict(FULFILLABLE_STATUSES)

# Doc types where goods are received via POST /receive (creates inventory parcels).
# These docs must NOT use fulfill-lines / revert-lines — those endpoints are outbound-only.
# Revert-to-draft for these types allows additional statuses (received, partially_received).
INBOUND_DOC_TYPES: frozenset[str] = frozenset({"consignment_in", "bill"})

# The statuses POST /revert-to-draft takes a document back from: an issued one, and for an
# inbound one also a receiving status. A document that sent back every good it received also
# reverts from a returned status (DRAFT_REVERT_RETURNED). Payments, credit notes, goods still
# held and lines out on memo or sold refuse it whatever the status.
DRAFT_REVERT_STATUSES = frozenset({"final", "sent", "awaiting_payment"})
INBOUND_DRAFT_REVERT_STATUSES = DRAFT_REVERT_STATUSES | {"received", "partially_received", "fulfilled"}
DRAFT_REVERT_RETURNED = frozenset({"partial_returned", "returned"})
# The reasons POST /revert-to-draft refuses a document for (routes.revert_check, listed as
# GET /docs/{id} revert_refused) that keep the document page from offering Revert to Draft:
# a document in no status to go back from, and one whose payments or credit notes are undone
# first, each from its own place on the page. Every other refusal names the step that clears
# it, so the button stays and answers with that refusal.
REVERT_HIDDEN_BY = frozenset({"status", "payments", "credit_note"})

# Statuses each document type sends goods back to its supplier in (POST /return-items): once
# goods have come in on it, and until all of them have gone back. A bill or purchase order
# paid in part or in full returns goods too: what was paid for them becomes a supplier credit.
_RETURN_OPEN = frozenset({"received", "partially_received", "partial_returned", "awaiting_payment"})
SUPPLIER_RETURN_STATUSES: dict[str, frozenset[str]] = {
    "consignment_in": _RETURN_OPEN,
    "bill": _RETURN_OPEN | {"partial", "paid"},
    "purchase_order": _RETURN_OPEN | {"partial", "paid"},
}

# Per-doc-type statuses POST /docs/{id}/convert takes a document in. A quotation converts in
# every status but QUOTATION_SPENT_STATUSES; a memo's Invoice all is a draw (MEMO_DRAW_STATUSES).
CONVERTIBLE_STATUSES: dict[str, frozenset[str]] = {
    "consignment_in": frozenset({"final", "sent", "received", "partially_received", "partial_returned", "returned"}),
}


# The payment methods that record a credit note applied to a document rather than money:
# the receiving side names its source (an invoice's "credit_note", a bill's
# "supplier_credit") and the note's own side is "applied". A pair moves no cash, so it is
# never refunded or deleted, only voided as a pair.
CREDIT_APPLICATION_METHODS: frozenset[str] = frozenset({"credit_note", "applied", "supplier_credit"})

# Document types whose line prices are the company's own selling prices, so a
# price that differs from its reference needs set_sales_doc_prices. Purchase and
# receiving documents carry the supplier's prices and are never gated by it; the
# list-side counterpart is list_behavior.is_money_list.
SALES_PRICED_DOC_TYPES: frozenset[str] = frozenset({
    "invoice", "proforma", "quotation", "credit_note", "memo", "subscription_invoice",
})

# Older records named their counterparty customer_id/customer_name (transfers: receiver).
# Writes take them as the contact fields, and replay folds stored ones the same way.
LEGACY_CONTACT_FIELDS: dict[str, str] = {
    "customer_id": "contact_id", "customer_name": "contact_name", "receiver": "contact_name",
}

# What an agreement's schedule operations write (celerp-subscriptions): on a billed
# document, the agreement and period it bills; on the agreement, its period claims, the
# bills it let go, history review, the restart a template paused on an earlier release
# owes, the unattended modes with their consents, and whether its bills recognize over their
# service period. The module's owned_fields_census
# test reads every key it writes and fails on one missing here.
SCHEDULE_OWNED_FIELDS: frozenset[str] = frozenset({
    "source_agreement", "period_index", "service_start", "service_end",
    "periods", "released_bills", "history_review", "history_set_aside", "resume_restarts",
    "consents", "generation_mode", "auto_send", "billing_failure", "recognize_over_service_period",
})
# What an earlier release kept on a template instead of a schedule. Only data that release
# wrote holds them, and moving the template onto a schedule removes them; no writer takes them.
SCHEDULE_REMOVED_FIELDS: frozenset[str] = frozenset({"next_run_date", "generated_doc_ids"})
# An agreement's schedule terms, and what it is (its kind, member and grace days): changed only
# through its schedule, which checks them against the periods already claimed. A draft may
# carry them when it is created.
SCHEDULE_TERM_FIELDS: frozenset[str] = frozenset({
    "anchor_date", "trial_end", "frequency", "custom_interval_days", "billing_timing", "term_start", "term_end",
    "cancel_at_period_end", "catchup_periods", "kind", "beneficiary_contact_id", "grace_days",
})


# State that only lifecycle operations write: finalize, send, payment, credit,
# receive, fulfil, convert, invoicing from a memo, close, void and scan, plus the record identity the ledger assigns.
# Ordinary creation never carries any of it, since a new document or list is an
# unpaid draft, and import-upsert never rewrites it. Only the snapshot import
# routes bring in an issued record, behind their own permission checks.
LIFECYCLE_OWNED_FIELDS: frozenset[str] = frozenset({
    "status", "finalized", "amount_paid", "amount_outstanding", "payments", "credited",
    "sent_to", "sent_via", "finalized_at", "sent_at", "issued_at", "accepted_at",
    "received_items", "received_item_ids", "returned_items", "returned_credit", "supplier_credit", "return_received_items",
    "fulfilled_items", "fulfillment_status", "fulfilled_at", "fulfilled_by", "fulfill_cycle",
    "converted_to", "converted_to_type", "invoiced_on", "source_po_ref", "source_proforma_ref", "source_memo_id", "linked",
    "result", "close_reason", "void_reason", "revert_count", "files",
    "pre_close_status", "pre_void_status", "pre_void_fulfillment", "pre_receipt_status", "pre_convert_status",
    "entity_type", "company_id", "doc_number", SCAN_RUNS_FIELD,
}) | SCHEDULE_OWNED_FIELDS


# Document types that can be shared by link and imported by the recipient. One
# set serves the Share button, the share API, the share page's Import link and
# the importer, so nothing can be shared that the other side cannot import. A
# quotation list travels as a quotation.
SHAREABLE_DOC_TYPES: frozenset[str] = frozenset({
    "invoice", "quotation", "proforma", "purchase_order",
    "credit_note", "bill", "memo", "consignment_in",
})


def share_doc_type(entity_type: str, state: dict) -> str | None:
    """The doc_type a document or list is shared and imported as."""
    if entity_type == "list":
        return "quotation" if state.get("list_type") in ("quote", "quotation") else None
    return state.get("doc_type")


def is_shareable(entity_type: str, state: dict) -> bool:
    return share_doc_type(entity_type, state) in SHAREABLE_DOC_TYPES


# Doc types where Send and Mark as Sent must be hidden entirely.
# Bills and consignment_in are internal receiving documents - never sent to external parties.
# Purchase orders are outbound to vendors and DO need send/mark-as-sent.
# Production orders are internal demand (invoice-to-self) - never sent to a customer.
NO_SEND_DOC_TYPES: frozenset[str] = frozenset({"bill", "consignment_in", "purchase_order", "production_order"})

# Documents whose issue posts their stored total to the books. Once issued, a line can only be
# removed from one through Revert to Draft, which voids that entry and posts the new total on
# the next issue (routes.assert_finalized_edit).
TOTAL_POSTED_DOC_TYPES: frozenset[str] = frozenset({"invoice", "bill", "credit_note", "debit_note"})

# Internal demand documents: not a sale/purchase, so excluded from revenue/AR/AP/sales accounting.
# A production order is "invoice-to-self" demand that feeds the manufacturing queue.
NON_FINANCIAL_DOC_TYPES: frozenset[str] = frozenset({"production_order"})

# Statuses where Send is suppressed even for sendable doc types. A closed memo is
# settled paperwork: re-sending it would silently un-close it, so Send is hidden.
NO_SEND_STATUSES: frozenset[str] = frozenset({"paid", "void", "closed"})

# Account classes a write-off may post to: expense for spoilage, samples and shrinkage, equity for
# owner drawings and family use. Never cogs: cost of sales belongs to sold stock alone. Shared by the
# API check and the UI picker so the two never diverge.
WRITEOFF_ACCOUNT_TYPES: frozenset[str] = frozenset({"expense", "equity"})

# Why each bulk line action leaves a chosen line as it is. Per action (as _line_action names it),
# per kind of line it leaves: (catalog key of the notice naming such lines after the action, key
# of the refusal's reason when every chosen line is of a kind with one, so none was a line the
# action could act on, followed by the action's next step, LINE_REFUSAL_NEXT), each key with a
# _one and a _many form naming {lines}. A choice that held a line
# the action could act on (an idle one) is refused with the notices instead: there was nothing
# to change, and choosing again would not help. Every notice and refusal of an action reads its
# own entry, so a line is never given another action's reason; the confirm built from its preview
# words each kind it leaves with documents.will_skip_<kind>. The kinds an action lists are the lines it leaves: Ship counts a service line shipped,
# so it has no service kind, and leaves a line that has already shipped (one invoiced from a
# memo, whose goods went out on the memo, as drawn) or whose goods are not free to ship. Mark as scanned leaves a line already marked, and Clear scanned one not marked.
_NO_ITEM = ("documents.lines_skipped", "lines.no_stock")
_SERVICE = ("documents.service_lines_skipped", "lines.service_no_stock")
_DRAW = {"memo_no_item": ("documents.memo_lines_skipped", "lines.memo_no_stock")}
LINE_SKIP_REASONS: dict[str, dict[str, tuple[str, str | None]]] = {
    "fulfill": {"no_item": _NO_ITEM, "shipped": ("documents.shipped_lines_skipped", None),
                "drawn": ("documents.drawn_lines_skipped", None),
                "unavailable": ("documents.unavailable_lines_skipped", None)},
    "reserve": {"no_item": _NO_ITEM, "service": _SERVICE},
    "revert": {"no_item": _NO_ITEM, "service": _SERVICE},
    "set-available": {"no_item": _NO_ITEM, "service": _SERVICE,
                      "idle": ("documents.idle_lines_skipped", None)},
    "invoice": _DRAW,
    "convert": _DRAW,
    "back_to_memo": {"own": ("documents.own_lines_skipped", "docs.back_to_memo_not_drawn")},
    "set-scanned": {"no_item": ("documents.scan_lines_skipped", "lines.scan_no_stock"),
                    "scanned": ("documents.scanned_lines_skipped", None),
                    "not_scanned": ("documents.unscanned_lines_skipped", None)},
    # Print labels runs in the page; a choice of only such lines keeps its own error.
    "labels": {"no_item": ("documents.labels_skipped", None)},
}

# The next step a refusal naming every chosen line gives (LINE_SKIP_REASONS), per action with a
# refusal: said once, after the reason for each kind of line, so it holds for every kind (a
# service is an inventory item but holds no stock) and is never repeated. A catalog key with a
# _one and a _many form, by the number of lines refused.
_CHOOSE_STOCK = "lines.choose_stock"
LINE_REFUSAL_NEXT: dict[str, str] = {
    "fulfill": _CHOOSE_STOCK, "reserve": _CHOOSE_STOCK, "revert": _CHOOSE_STOCK, "set-available": _CHOOSE_STOCK,
    "invoice": _CHOOSE_STOCK, "convert": _CHOOSE_STOCK, "set-scanned": _CHOOSE_STOCK,
    "back_to_memo": "docs.back_to_memo_delete_instead",
}


def _order(value) -> tuple:
    """A value as a sort key that orders an empty value (None) first and never compares it
    with a value of its own type."""
    return (value is not None, value if value is not None else 0)


def _shown(entries: list[dict] | None, keys: tuple[str, ...]) -> list[dict]:
    """Each entry's ``keys`` only (None for a key it lacks), ordered by those keys' values in turn."""
    return sorted(({k: e.get(k) for k in keys} for e in entries or []), key=lambda e: [_order(e[k]) for k in keys])


def lot_name(entry: dict) -> str | None:
    """The name a lot taken from another invoice (an answer's ``taken_from`` entry) is shown by:
    its SKU, else its item's name; None when it has neither."""
    return entry.get("sku") or entry.get("name") or None


# How much of a lot another invoice held a shipment takes (a ``taken_from`` entry's ``takes``,
# auto_je.LotTaken): the question asked before it ships and the notice said after, each worded for
# that outcome, all it held (it can no longer ship the lot) or part (it has less of it to ship).
# A ``takes`` the entry lacks reads as part, true in either case.
LOT_TAKEN: dict[str, tuple[str, str]] = {
    "all": ("documents.will_take_from", "documents.cost_moved_with_goods"),
    "part": ("documents.will_take_part_from", "documents.cost_part_moved_with_goods"),
}


def lot_taken_message(shown: dict, when: int, unnamed: str) -> tuple[str, dict]:
    """The message (catalog key, params) for a lot taken from another invoice, from its shown
    entry (OUTCOME_SHOWN's ``taken_from``): asked before shipping (``when`` 0) or said after (1),
    worded for how much of the lot it takes, naming the lot by its name, ``unnamed`` (the
    reader's words for a lot with no SKU or name) when it has none, and never by an id."""
    return LOT_TAKEN[shown["takes"]][when], {"sku": shown["lot"] or unnamed, "doc": shown["doc_number"]}


def _takes(entry: dict) -> str:
    """A ``taken_from`` entry's ``takes`` as a LOT_TAKEN kind."""
    return entry.get("takes") if entry.get("takes") in LOT_TAKEN else "part"


# What a stock line action's preview shows, part by part, from its answer: the lines it acts on,
# each line it leaves with why, the lines whose shipment it takes back and each lot it takes from
# another invoice. Each part holds exactly what its confirm words (a line by number and name, a
# lot by its name (lot_name), the invoice by number and how much of the lot it takes) with the ids
# that make it the same line, lot or invoice, so the binding (routes.outcome_binding) digests what
# the question (the page's _preview_confirm) reads and nothing else: shared by the two so they
# never diverge. A lot carved off is left out, as it is new each time the action runs; a lot taken
# from another invoice is the one it is carved from. A field an entry lacks reads as None.
OUTCOME_SHOWN = {
    "acted": lambda answer: answer["acted"],
    "skipped": lambda answer: _shown(answer.get("skipped"), ("line", "kind", "name", "line_id")),
    "taken_back": lambda answer: _shown(answer.get("taken_back"), ("line", "name", "line_id")),
    "taken_from": lambda answer: _shown([{**e, "lot": lot_name(e), "takes": _takes(e)}
                                         for e in answer.get("taken_from") or []],
                                        ("doc_number", "takes", "lot", "doc_id", "lot_id")),
}


def outcome_shown(answer: dict) -> dict:
    """A stock line action's preview ``answer`` as what it shows (OUTCOME_SHOWN), part by part."""
    return {part: show(answer) for part, show in OUTCOME_SHOWN.items()}

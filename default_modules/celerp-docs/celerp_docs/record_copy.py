# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""What a record made from another record takes: one rule for every door that builds a document
or list out of an existing one (duplicate, convert, buy a consignment).

A copy is the whole source record, minus the facts that belong to the source alone: where it is
in its lifecycle (status, outcome, sending, payments, receipts, scans, restore points), its kind
and number, its dates and where it came from. Those are ``NEVER_COPIED``. Everything a user
entered, custom fields included, is copied, so a field added later is copied without being
listed here. A key is lifecycle when a writer moves the record through its states with it; every
such writer is found by tests/lifecycle_writers.py, and a key one sets outside this set fails
tests/test_record_copy.py.
"""
from __future__ import annotations

from celerp.services.business_time import business_date_of
from celerp.services.payment_terms import company_payment_terms, due_date_for_terms
from celerp_docs.doc_constants import LIFECYCLE_OWNED_FIELDS
from celerp_docs.doc_projections import RESTORE_POINT

NEVER_COPIED: frozenset[str] = LIFECYCLE_OWNED_FIELDS | frozenset({
    # Kind and number: the door names the new record's.
    "doc_type", "list_type", "ref_id", "share_token", "idempotency_key",
    # Dates: a copy is issued the day it is made.
    "issue_date", "date", "due_date", "valid_until",
    # Where the source came from.
    "source_list_id", "source_quotation_id", "source_consignment_id", "original_doc_id", "import_treatment",
    # Lifecycle facts the record's own steps write.
    RESTORE_POINT, "adjust_count", "customs_saved", "moved_at", "moved_to_location_id", "released",
    "start_date", "next_run_date", "generated_doc_ids",
    "woocommerce_status", "woocommerce_transaction_id", "woocommerce_reconciliation_required",
    "woocommerce_reconciliation_signature", "woocommerce_reconciled_signature",
})


def copy_of(state: dict, settings: dict) -> dict:
    """A new draft made today from ``state``: every key but NEVER_COPIED, issued on the company's
    business day and due by the source's payment term from that day. With no term to follow there
    is no due date, never the source's own, set against its earlier issue date. The door adds the
    new record's kind, number and where it came from."""
    issued = business_date_of(None, (settings or {}).get("timezone"))
    due = due_date_for_terms(issued, state.get("payment_terms"), company_payment_terms(settings or {}))
    return {**{k: v for k, v in state.items() if k not in NEVER_COPIED},
            "status": "draft", "issue_date": issued, **({"due_date": due} if due else {})}

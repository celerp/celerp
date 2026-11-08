# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""What a record made from another record inherits: one rule for every door that builds a
document or list out of an existing one (duplicate, convert, buy a consignment).

A new record takes the source's content only: what it says to the counterparty and what it is
made of. Everything else is a fact about the source itself (its status, outcome, sending,
numbering, dates, share links, scans, restore points, payments) and is never inherited. A key
not named in ``INHERITED_CONTENT`` is not inherited, so a key added later stays with its own
record until it is placed here on purpose.
"""
from __future__ import annotations

from celerp.services.business_time import business_date_of
from celerp.services.payment_terms import company_payment_terms, due_date_for_terms
from celerp_contacts.references import contact_snapshot
from celerp_docs.doc_constants import LIFECYCLE_OWNED_FIELDS
from celerp_docs.doc_projections import RESTORE_POINT

INHERITED_CONTENT: frozenset[str] = frozenset({
    # The counterparty, as the source names it.
    "contact_id", "contact_name", *contact_snapshot({}),
    # What it is made of.
    "line_items", "purchase_kind", "source_docs",
    # Terms and notes.
    "payment_terms", "price_list", "terms", "terms_template", "terms_text", "customer_note", "notes", "reference",
    # Money: the inputs, and the figures they give (a door that changes the representation works them out again).
    "currency", "conversion_rate", "discount", "discount_type", "shipping", "tax", "tax_rate", "doc_taxes",
    "subtotal", "discount_amount", "tax_amount", "total",
    # Delivery and customs.
    "carrier", "tracking", "expected_delivery", "from_location_id", "to_location_id", "to_address",
    "incoterms", "package_count", "gross_weight", "reason_for_export", "country_of_export",
    "country_of_destination", "importer",
})

# Every other key a document or list holds, named so a new key has to be placed on one side or
# the other: the record's kind and number (the door names the new one), its dates (a new record
# is issued the day it is made), where it came from, its links and every lifecycle fact.
NOT_INHERITED: frozenset[str] = LIFECYCLE_OWNED_FIELDS | frozenset({
    "doc_type", "list_type", "ref_id", "share_token", "idempotency_key", RESTORE_POINT,
    "issue_date", "date", "due_date", "valid_until",
    "source_list_id", "source_quotation_id", "source_consignment_id", "original_doc_id",
})


def inherited_content(state: dict, settings: dict) -> dict:
    """What a record made today from ``state`` starts with: the source's content (INHERITED_CONTENT),
    issued on the company's business day and due by the source's payment term from that day. With
    no term to follow there is no due date, never the source's own, set against its earlier issue
    date. The door adds the new record's kind, number, status and where it came from."""
    issued = business_date_of(None, (settings or {}).get("timezone"))
    due = due_date_for_terms(issued, state.get("payment_terms"), company_payment_terms(settings or {}))
    return {**{k: v for k, v in state.items() if k in INHERITED_CONTENT},
            "issue_date": issued, **({"due_date": due} if due else {})}

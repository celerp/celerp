# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""What a record made from another record takes: one rule for every door that builds a document
or list out of an existing one (duplicate, convert, buy a consignment).

A copy is the source's parties and its lines, as a new draft. Each line keeps only what a user
enters on a line, which the line save steps own (line_bodies): a count, an adjustment, a write-off,
a draw or a receipt stays with the source line. The record keeps its contact and the currency its
line prices are in; everything else (terms, notes, commission, the From block, a List's location,
custom fields) is for the user to fill on the copy. A conversion (conversion_of) also keeps the money
the other side agreed to, a tax amount with no rate included, and its payment term (CONVERSION_KEYS),
so it charges what was quoted. The door adds the new record's kind, number and where it came from.
"""
from __future__ import annotations

from celerp.services.business_time import business_date_of
from celerp.services.payment_terms import company_payment_terms, due_date_for_terms
from celerp_contacts.references import contact_snapshot
from celerp_docs.doc_money import CONVERSION_KEYS
from celerp_docs.line_bodies import LineItem, WriteoffLineBody

# The line editor sends an item as entity_id and the line keeps it as item_id (LineItem).
LINE_KEYS: frozenset[str] = frozenset(LineItem.model_fields) | frozenset(WriteoffLineBody.model_fields)
# The contact and its snapshot, and the currency and rate the line prices are given in.
RECORD_KEYS: frozenset[str] = frozenset({"contact_id", *contact_snapshot({}), "currency", "conversion_rate"})


def copied_line(line: dict) -> dict:
    """The keys of ``line`` a user enters, its item under item_id."""
    kept = {k: v for k, v in line.items() if k in LINE_KEYS and k != "entity_id"}
    if not kept.get("item_id") and line.get("entity_id"):
        kept["item_id"] = line["entity_id"]
    return kept


def copy_of(state: dict, settings: dict) -> dict:
    """A new draft made today from ``state``: its parties and its lines (copied_line), issued on the
    company's business day and due by the source's payment term from that day. With no term to
    follow there is no due date, never the source's own, set against its earlier issue date."""
    issued = business_date_of(None, (settings or {}).get("timezone"))
    due = due_date_for_terms(issued, state.get("payment_terms"), company_payment_terms(settings or {}))
    return {**{k: state[k] for k in RECORD_KEYS if k in state},
            "line_items": [copied_line(li) for li in state.get("line_items") or [] if isinstance(li, dict)],
            "status": "draft", "issue_date": issued, **({"due_date": due} if due else {})}


def conversion_of(state: dict, settings: dict, *, tax_rate_key: str = "tax_rate") -> dict:
    """A copy of ``state`` (copy_of) that keeps its CONVERSION_KEYS too. A List holds its header tax
    rate as ``tax`` (beside ``tax_amount``), so its door passes ``tax_rate_key="tax"``."""
    kept = {k: state[k] for k in CONVERSION_KEYS - {"tax_rate", tax_rate_key} if k in state}
    if state.get(tax_rate_key) is not None:
        kept["tax_rate"] = state[tax_rate_key]
    return {**copy_of(state, settings), **kept}

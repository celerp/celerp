# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the contact-scoped holdings resolver (celerp.services.holdings).

Pure functions over already-loaded (entity_id, state) rows, so no DB is needed.
These pin the membership predicate and the value basis in isolation; the endpoint
tests then layer the HTTP wiring on top.
"""

from __future__ import annotations

from celerp.services.holdings import consignment_holdings, memo_holdings


# --- memo (consignment out to a customer) -----------------------------------

def test_memo_value_is_quoted_unit_price_times_remaining_qty():
    items = [("item:1", {"status": "memo_out", "quantity": 2, "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]})]
    assert memo_holdings(items, memo_docs) == {"item:1": 200.0}


def test_memo_uses_quoted_override_not_catalog_price():
    # The line was quoted at 90 even though the item's own catalog/retail is irrelevant here:
    # the resolver reads the memo line, never the item's price fields.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:A"],
                          "retail_price": 130.0})]
    memo_docs = [("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 90.0}]})]
    assert memo_holdings(items, memo_docs) == {"item:1": 90.0}


def test_memo_falls_back_to_line_total_when_no_unit_price():
    items = [("item:1", {"status": "memo_out", "quantity": 3, "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"line_items": [{"item_id": "item:1", "line_total": 150.0}]})]
    assert memo_holdings(items, memo_docs) == {"item:1": 150.0}


def test_memo_excludes_returned_item():
    # Reverted memo line: status back to available, doc dropped from fulfilled_for_docs.
    items = [("item:1", {"status": "available", "quantity": 1, "fulfilled_for_docs": []})]
    memo_docs = [("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]})]
    assert memo_holdings(items, memo_docs) == {}


def test_memo_excludes_item_out_to_a_different_customer():
    # item is memo_out, but its doc is not among THIS customer's memo docs.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:OTHER"]})]
    memo_docs = [("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]})]
    assert memo_holdings(items, memo_docs) == {}


def test_memo_aggregates_across_multiple_memos_to_same_customer():
    items = [
        ("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:A"]}),
        ("item:2", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:B"]}),
    ]
    memo_docs = [
        ("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]}),
        ("doc:B", {"line_items": [{"entity_id": "item:2", "unit_price": 250.0}]}),
    ]
    assert memo_holdings(items, memo_docs) == {"item:1": 100.0, "item:2": 250.0}


def test_memo_resolves_current_doc_when_item_reappears_in_two_docs():
    # item:1 was on memo to this customer twice (doc:A then doc:B); only doc:B is live
    # (in fulfilled_for_docs). The value must come from doc:B's line, not doc:A's.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:B"]})]
    memo_docs = [
        ("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]}),
        ("doc:B", {"line_items": [{"entity_id": "item:1", "unit_price": 175.0}]}),
    ]
    assert memo_holdings(items, memo_docs) == {"item:1": 175.0}


# --- consignment (consignment in from a supplier) ---------------------------

def test_consignment_value_is_item_cost():
    items = [("item:1", {"consignment_flag": "in", "cost_total": 400.0})]
    docs = [("doc:C", {"received_item_ids": ["item:1"]})]
    assert consignment_holdings(items, docs) == {"item:1": 400.0}


def test_consignment_cost_from_unit_when_no_cost_total():
    items = [("item:1", {"consignment_flag": "in", "cost_price": 50.0, "quantity": 3})]
    docs = [("doc:C", {"received_item_ids": ["item:1"]})]
    assert consignment_holdings(items, docs) == {"item:1": 150.0}


def test_consignment_excludes_returned_item_flag_cleared():
    # Fully returned to supplier: flag cleared to None, still listed in received_item_ids.
    items = [("item:1", {"consignment_flag": None, "cost_total": 400.0})]
    docs = [("doc:C", {"received_item_ids": ["item:1"]})]
    assert consignment_holdings(items, docs) == {}


def test_consignment_excludes_owned_inventory():
    # Owned item (no consignment flag) even if some doc lists it.
    items = [("item:1", {"cost_total": 400.0})]
    docs = [("doc:C", {"received_item_ids": ["item:1"]})]
    assert consignment_holdings(items, docs) == {}


def test_consignment_excludes_item_from_a_different_supplier():
    items = [("item:1", {"consignment_flag": "in", "cost_total": 400.0})]
    docs = [("doc:C", {"received_item_ids": ["item:OTHER"]})]
    assert consignment_holdings(items, docs) == {}

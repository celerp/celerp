# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the contact-scoped holdings resolver (celerp.services.holdings).

Pure functions over already-loaded (entity_id, state) rows, so no DB is needed.
These pin the membership predicate and the value basis in isolation; the endpoint
tests then layer the HTTP wiring on top.
"""

from __future__ import annotations

import pytest

from celerp.services.holdings import (
    consignment_holdings,
    memo_holdings,
    sold_prices,
    sold_value_total,
    value_total,
)


# --- memo (consignment out to a customer) -----------------------------------

def test_memo_value_is_quoted_unit_price_times_remaining_qty():
    items = [("item:1", {"status": "memo_out", "quantity": 2, "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]})]
    assert memo_holdings(items, memo_docs, "USD") == {"item:1": 200.0}


def test_memo_uses_quoted_override_not_catalog_price():
    # The line was quoted at 90 even though the item's own catalog/retail is irrelevant here:
    # the resolver reads the memo line, never the item's price fields.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:A"],
                          "retail_price": 130.0})]
    memo_docs = [("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 90.0}]})]
    assert memo_holdings(items, memo_docs, "USD") == {"item:1": 90.0}


def test_memo_value_is_none_when_the_line_has_no_per_unit_charge():
    # A line total with no quantity cannot say what the 3 still out are worth.
    items = [("item:1", {"status": "memo_out", "quantity": 3, "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"line_items": [{"item_id": "item:1", "line_total": 150.0}]})]
    assert memo_holdings(items, memo_docs, "USD") == {"item:1": None}


def test_memo_excludes_returned_item():
    # Reverted memo line: status back to available, doc dropped from fulfilled_for_docs.
    items = [("item:1", {"status": "available", "quantity": 1, "fulfilled_for_docs": []})]
    memo_docs = [("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]})]
    assert memo_holdings(items, memo_docs, "USD") == {}


def test_memo_excludes_item_out_to_a_different_customer():
    # item is memo_out, but its doc is not among THIS customer's memo docs.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:OTHER"]})]
    memo_docs = [("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]})]
    assert memo_holdings(items, memo_docs, "USD") == {}


def test_memo_aggregates_across_multiple_memos_to_same_customer():
    items = [
        ("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:A"]}),
        ("item:2", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:B"]}),
    ]
    memo_docs = [
        ("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]}),
        ("doc:B", {"line_items": [{"entity_id": "item:2", "unit_price": 250.0}]}),
    ]
    assert memo_holdings(items, memo_docs, "USD") == {"item:1": 100.0, "item:2": 250.0}


def test_memo_resolves_current_doc_when_item_reappears_in_two_docs():
    # item:1 was on memo to this customer twice (doc:A then doc:B); only doc:B is live
    # (in fulfilled_for_docs). The value must come from doc:B's line, not doc:A's.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:B"]})]
    memo_docs = [
        ("doc:A", {"line_items": [{"entity_id": "item:1", "unit_price": 100.0}]}),
        ("doc:B", {"line_items": [{"entity_id": "item:1", "unit_price": 175.0}]}),
    ]
    assert memo_holdings(items, memo_docs, "USD") == {"item:1": 175.0}


# --- consignment (consignment in from a supplier) ---------------------------

def test_consignment_value_is_item_cost():
    items = [("item:1", {"consignment_flag": "in", "cost_total": 400.0})]
    docs = [("doc:C", {"received_item_ids": ["item:1"]})]
    assert consignment_holdings(items, docs, "USD") == {"item:1": 400.0}


def test_consignment_cost_from_unit_when_no_cost_total():
    items = [("item:1", {"consignment_flag": "in", "cost_price": 50.0, "quantity": 3})]
    docs = [("doc:C", {"received_item_ids": ["item:1"]})]
    assert consignment_holdings(items, docs, "USD") == {"item:1": 150.0}


def test_consignment_excludes_returned_item_flag_cleared():
    # Fully returned to supplier: flag cleared to None, still listed in received_item_ids.
    items = [("item:1", {"consignment_flag": None, "cost_total": 400.0})]
    docs = [("doc:C", {"received_item_ids": ["item:1"]})]
    assert consignment_holdings(items, docs, "USD") == {}


def test_consignment_excludes_owned_inventory():
    # Owned item (no consignment flag) even if some doc lists it.
    items = [("item:1", {"cost_total": 400.0})]
    docs = [("doc:C", {"received_item_ids": ["item:1"]})]
    assert consignment_holdings(items, docs, "USD") == {}


def test_consignment_excludes_item_from_a_different_supplier():
    items = [("item:1", {"consignment_flag": "in", "cost_total": 400.0})]
    docs = [("doc:C", {"received_item_ids": ["item:OTHER"]})]
    assert consignment_holdings(items, docs, "USD") == {}


# --- sold price (realized per-unit sale price of a sold item) ----------------

def test_sold_price_is_selling_line_unit_price():
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {"line_items": [{"item_id": "item:1", "unit_price": 89.96, "line_total": 206.0}]})]
    assert sold_prices(items, docs, "USD") == {"item:1": 89.96}


def test_sold_price_matches_line_by_item_id_when_entity_id_blank():
    # Real docs carry the item reference on the line's item_id; entity_id is empty there.
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {"line_items": [{"entity_id": "", "item_id": "item:1", "unit_price": 50.0}]})]
    assert sold_prices(items, docs, "USD") == {"item:1": 50.0}


def test_sold_price_derives_per_unit_from_line_total_when_no_unit_price():
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {"line_items": [{"item_id": "item:1", "line_total": 206.0, "quantity": 2.0}]})]
    assert sold_prices(items, docs, "USD") == {"item:1": 103.0}


def test_sold_price_matches_line_by_sku_when_line_has_no_item_ref():
    # Engine-fulfilled invoice: the line was created from a SKU and carries no item
    # reference, so the match falls back to the item's own sku.
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A", "sku": "GEM-1"})]
    docs = [("doc:A", {"line_items": [{"sku": "GEM-1", "quantity": 2, "unit_price": 100.0}]})]
    assert sold_prices(items, docs, "USD") == {"item:1": 100.0}


def test_sold_price_is_none_when_selling_line_missing():
    # Item marked sold but no matching line resolves (e.g. deleted doc): honest None, never 0.
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {"line_items": [{"item_id": "item:OTHER", "unit_price": 10.0}]})]
    assert sold_prices(items, docs, "USD") == {"item:1": None}


def test_sold_price_is_none_without_status_doc():
    items = [("item:1", {"status": "sold"})]
    assert sold_prices(items, [], "USD") == {"item:1": None}


def test_sold_price_is_the_discounted_line_amount_per_unit():
    # unit_price is the quoted price before the line discount; line_total is what the line
    # actually charged, so the realized price is line_total / quantity.
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {"line_items": [
        {"item_id": "item:1", "quantity": 10, "unit_price": 10.0, "discount_pct": 10, "line_total": 90.0},
    ]})]
    assert sold_prices(items, docs, "USD") == {"item:1": 9.0}


def test_sold_price_by_sku_is_none_when_same_sku_lines_disagree():
    # Two lines for the sku at different prices: no way to know which one sold this item.
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A", "sku": "GEM-1"})]
    docs = [("doc:A", {"line_items": [
        {"sku": "GEM-1", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        {"sku": "GEM-1", "quantity": 1, "unit_price": 80.0, "line_total": 80.0},
    ]})]
    assert sold_prices(items, docs, "USD") == {"item:1": None}


def test_sold_price_by_sku_resolves_when_same_sku_lines_agree():
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A", "sku": "GEM-1"})]
    docs = [("doc:A", {"line_items": [
        {"sku": "GEM-1", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        {"sku": "GEM-1", "quantity": 2, "unit_price": 100.0, "line_total": 200.0},
    ]})]
    assert sold_prices(items, docs, "USD") == {"item:1": 100.0}


def test_memo_value_is_the_discounted_line_amount_per_unit_times_remaining():
    # Quoted 100 each, 10% off on the line: the customer holds 180 of goods, not 200.
    items = [("item:1", {"status": "memo_out", "quantity": 2, "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"line_items": [
        {"entity_id": "item:1", "quantity": 2, "unit_price": 100.0, "line_total": 180.0},
    ]})]
    assert memo_holdings(items, memo_docs, "USD") == {"item:1": 180.0}


# --- attribution: one resolver, explicit when ambiguous ------------------------

def test_repeated_item_lines_that_disagree_resolve_to_none():
    # The same item on two lines at different prices: neither line can be chosen.
    lines = [{"item_id": "item:1", "quantity": 1, "unit_price": 100.0},
             {"item_id": "item:1", "quantity": 1, "unit_price": 80.0}]
    memo_items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:A"]})]
    sold_items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"})]
    assert memo_holdings(memo_items, [("doc:A", {"line_items": lines})], "USD") == {"item:1": None}
    assert sold_prices(sold_items, [("doc:A", {"line_items": lines})], "USD") == {"item:1": None}


def test_same_sku_lines_are_compared_before_rounding():
    # 100 / 3 and 33.33 both round to 33.33, but they are different charges.
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A", "sku": "GEM-1"})]
    docs = [("doc:A", {"line_items": [
        {"sku": "GEM-1", "quantity": 3, "line_total": 100.0},
        {"sku": "GEM-1", "quantity": 1, "unit_price": 33.33, "line_total": 33.33},
    ]})]
    assert sold_prices(items, docs, "USD") == {"item:1": None}


def test_sku_match_is_none_when_any_same_sku_line_disagrees():
    # item:2 could have gone out on either GEM-1 line, including the one naming item:1.
    items = [("item:2", {"status": "sold", "status_doc_id": "doc:A", "sku": "GEM-1"})]
    docs = [("doc:A", {"line_items": [
        {"item_id": "item:1", "sku": "GEM-1", "quantity": 1, "unit_price": 500.0},
        {"sku": "GEM-1", "quantity": 1, "unit_price": 100.0},
    ]})]
    assert sold_prices(items, docs, "USD") == {"item:2": None}


def test_sibling_lot_sold_on_a_line_naming_another_lot_prices_by_sku():
    # item:2 was split from item:1; the invoice line still names item:1.
    items = [("item:2", {"status": "sold", "status_doc_id": "doc:A", "sku": "GEM-1"})]
    docs = [("doc:A", {"line_items": [
        {"item_id": "item:1", "sku": "GEM-1", "quantity": 2, "unit_price": 100.0},
    ]})]
    assert sold_prices(items, docs, "USD") == {"item:2": 100.0}


def test_exact_item_reference_wins_over_same_sku_lines():
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A", "sku": "GEM-1"})]
    docs = [("doc:A", {"line_items": [
        {"item_id": "item:1", "sku": "GEM-1", "quantity": 1, "unit_price": 500.0},
        {"sku": "GEM-1", "quantity": 1, "unit_price": 100.0},
    ]})]
    assert sold_prices(items, docs, "USD") == {"item:1": 500.0}


def test_memo_line_without_item_reference_resolves_by_sku():
    items = [("item:1", {"status": "memo_out", "quantity": 2, "sku": "GEM-1", "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"line_items": [{"sku": "GEM-1", "quantity": 2, "unit_price": 75.0}]})]
    assert memo_holdings(items, memo_docs, "USD") == {"item:1": 150.0}


def test_memo_value_is_none_not_zero_when_no_line_matches():
    items = [("item:1", {"status": "memo_out", "quantity": 1, "sku": "GEM-1", "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"line_items": [{"sku": "OTHER", "quantity": 1, "unit_price": 75.0}]})]
    assert memo_holdings(items, memo_docs, "USD") == {"item:1": None}


# --- money: company currency, document rate, header discount -------------------

def test_values_convert_at_the_document_rate_into_the_company_currency():
    # A USD memo in a JPY company: 10.50 USD at 150 is 1575 JPY, at JPY's zero decimals.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"currency": "USD", "conversion_rate": 150,
                            "line_items": [{"item_id": "item:1", "quantity": 1, "unit_price": 10.5}]})]
    assert memo_holdings(items, memo_docs, "JPY") == {"item:1": 1575.0}


def test_foreign_document_without_a_rate_has_no_company_currency_value():
    # A USD memo in a THB company with no rate: 10.50 USD is not 10.50 THB.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:A"]}),
             ("item:2", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {"currency": "USD", "line_items": [
        {"item_id": "item:1", "quantity": 1, "unit_price": 10.5},
        {"item_id": "item:2", "quantity": 1, "unit_price": 20.0},
    ]})]
    assert memo_holdings(items, docs, "THB") == {"item:1": None}
    assert sold_prices(items[1:], docs, "THB") == {"item:2": None}


@pytest.mark.parametrize("doc", [
    {"currency": "THB", "conversion_rate": 35},
    {"currency": "USD", "conversion_rate": 0},
    {"currency": "USD", "conversion_rate": "abc"},
])
def test_document_with_an_invalid_rate_has_no_company_currency_value(doc):
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {**doc, "line_items": [{"item_id": "item:1", "quantity": 1, "unit_price": 10.0}]})]
    assert sold_prices(items, docs, "THB") == {"item:1": None}


def test_company_currency_document_values_at_one_with_or_without_a_rate():
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"}),
             ("item:2", {"status": "sold", "status_doc_id": "doc:B"})]
    docs = [("doc:A", {"currency": "THB", "line_items": [{"item_id": "item:1", "unit_price": 10.0}]}),
            ("doc:B", {"currency": "thb", "conversion_rate": 1, "line_items": [{"item_id": "item:2", "unit_price": 20.0}]})]
    assert sold_prices(items, docs, "THB") == {"item:1": 10.0, "item:2": 20.0}


def test_values_keep_the_company_currency_precision():
    # KWD carries three decimals: 1.2345 rounds to 1.235, not 1.23.
    items = [("item:1", {"status": "memo_out", "quantity": 1, "fulfilled_for_docs": ["doc:A"]})]
    memo_docs = [("doc:A", {"line_items": [{"item_id": "item:1", "quantity": 1, "unit_price": 1.2345}]})]
    assert memo_holdings(items, memo_docs, "KWD") == {"item:1": 1.235}


def test_sold_price_keeps_rate_precision():
    # A per-unit price is a rate, so it keeps more places than the currency does.
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {"line_items": [{"item_id": "item:1", "quantity": 3, "line_total": 100.0}]})]
    assert sold_prices(items, docs, "USD")["item:1"] > 33.33


def test_header_discount_is_shared_across_lines_by_amount():
    # 10% off the whole document: each line carries 10% less.
    items = [("item:1", {"status": "sold", "status_doc_id": "doc:A"}),
             ("item:2", {"status": "sold", "status_doc_id": "doc:A"})]
    docs = [("doc:A", {"subtotal": 300.0, "discount_amount": 30.0, "line_items": [
        {"item_id": "item:1", "quantity": 1, "unit_price": 100.0, "line_total": 100.0},
        {"item_id": "item:2", "quantity": 1, "unit_price": 200.0, "line_total": 200.0},
    ]})]
    assert sold_prices(items, docs, "USD") == {"item:1": 90.0, "item:2": 180.0}


def test_sold_total_rounds_at_the_company_currency():
    rows = [{"id": "item:1", "quantity": 3}, {"id": "item:2", "quantity": 1}]
    total, missing = sold_value_total(rows, {"item:1": 0.3333, "item:2": None}, "JPY")
    assert (total, missing) == (1.0, 1)


def test_value_total_counts_unresolved_values():
    assert value_total([10.005, None, 0.1], "KWD") == (10.105, 1)


def test_holdings_banner_says_how_many_items_have_no_value():
    from fasthtml.common import to_xml

    from ui.routes.inventory import _holdings_scope_banner

    html = to_xml(_holdings_scope_banner({"on_memo_to": "contact:1"}, 150.0, "USD", 2))
    assert "$150.00 (2 without a price)" in html
    assert "without a price" not in to_xml(_holdings_scope_banner({"on_memo_to": "contact:1"}, 150.0, "USD"))

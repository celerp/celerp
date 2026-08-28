# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for the inventory search grammar.

The matcher is a pure function over a flattened item dict, so the grammar
(numeric-exact, the `-` range operator, the `&` AND operator, `,` OR groups,
and literal SKU/text substrings) is tested here without a database.
"""
from __future__ import annotations

from celerp_inventory.routes import item_matches_query, query_match_reasons


def _item(**overrides) -> dict:
    base = {"name": "Widget", "sku": "WDGT", "barcode": "", "description": "",
            "category": "Raw", "quantity": 0, "weight": 0, "pieces": 0}
    base.update(overrides)
    return base


def test_inventory_search_numeric_exact():
    """A bare number matches items whose numeric column equals it exactly, and
    never by numeric substring (so `5` does not match quantity 50)."""
    qty5 = _item(name="Widget", sku="WDGT", quantity=5)
    qty50 = _item(name="Gadget", sku="GDGT", quantity=50)
    assert item_matches_query(qty5, "5")
    assert not item_matches_query(qty50, "5")
    # The bare number still does the existing text substring match.
    assert item_matches_query(_item(name="Item 5 pack", sku="P5PACK", quantity=0), "5")


def test_inventory_search_range():
    """`5-10` matches an item with any numeric column in [5, 10] and excludes one
    outside it; a hyphenated SKU stays a literal text search (regression guard)."""
    in_range = _item(name="Bolt", sku="B7", quantity=7)
    out_range = _item(name="Bolt", sku="B20", quantity=20)
    assert item_matches_query(in_range, "5-10")
    assert not item_matches_query(out_range, "5-10")
    # Range matches on weight and pieces too, not only quantity.
    assert item_matches_query(_item(name="W", sku="WW", weight=8.5), "5-10")
    assert item_matches_query(_item(name="P", sku="PP", pieces=6), "5-10")
    # Regression: a hyphenated SKU is not a numeric range; it stays literal text.
    sku_item = _item(name="Scope", sku="SHOT274-005", quantity=0)
    assert item_matches_query(sku_item, "SHOT274-005")
    assert not item_matches_query(in_range, "SHOT274-005")


def test_inventory_search_and_operator():
    """`bolt & 5-10` requires both a text match and a numeric-range match; `,` still
    ORs groups."""
    bolt7 = _item(name="Bolt", sku="B7", quantity=7)
    bolt50 = _item(name="Bolt", sku="B50", quantity=50)
    washer = _item(name="Washer", sku="WSH", quantity=7)
    assert item_matches_query(bolt7, "bolt & 5-10")
    assert not item_matches_query(bolt50, "bolt & 5-10")   # right text, wrong number
    assert not item_matches_query(washer, "bolt & 5-10")   # right number, wrong text
    # Comma still ORs: either group matches.
    assert item_matches_query(washer, "bolt, washer")
    # An empty AND-term is dropped, not treated as a match-all.
    assert item_matches_query(bolt7, "bolt & ")
    assert not item_matches_query(washer, "bolt & ")


def test_inventory_search_match_reasons():
    """query_match_reasons names the (field, matched text) behind each hit: the term
    for substring hits, the whole number for numeric hits, one reason per AND term
    of the winning group, and None on a miss."""
    item = _item(name="Widget", sku="WDGT", barcode="000255", quantity=37)
    # Substring hit on a hidden field reports that field and the term.
    assert query_match_reasons(item, "255") == [("barcode", "255")]
    # Numeric-exact hit reports the field and the full number.
    assert query_match_reasons(_item(quantity=255), "255") == [("quantity", "255")]
    # A range hit reports the value that fell inside the range.
    assert query_match_reasons(item, "10-50") == [("quantity", "37")]
    # `a & b` returns one reason per AND term, in term order.
    assert query_match_reasons(item, "widget & 255") == [("name", "widget"), ("barcode", "255")]
    # Attribute values (flattened to top level) report their attribute key.
    assert query_match_reasons(_item(color="ruby red"), "ruby") == [("color", "ruby")]
    # A visible-field hit is still reported; the UI decides not to render a tag.
    assert query_match_reasons(item, "widget") == [("name", "widget")]
    assert query_match_reasons(item, "nope") is None
    # item_matches_query stays the boolean view of the same grammar.
    assert item_matches_query(item, "255")
    assert not item_matches_query(item, "nope")


def test_inventory_search_excludes_internal_bookkeeping():
    """Issue #306: search must not reach internal bookkeeping. A term that appears
    only inside the idempotency key, an id, or a lineage reference is not a hit."""
    # The idempotency key preserves the ORIGINAL import barcode (csv:item:bc:100) even
    # after the barcode is edited to 200. Searching the obsolete 100 must not find it.
    item = _item(name="Widget", sku="WDGT", barcode="200",
                 idempotency_key="csv:item:bc:100")
    assert query_match_reasons(item, "100") is None
    assert not item_matches_query(item, "100")
    assert item_matches_query(item, "200")  # the current barcode still matches
    # ids and lineage references (the internal *_id / relationship fields) are excluded.
    leaky = _item(name="Bolt", sku="BLT", parent_id="itm_abc123",
                  status_doc_id="doc_xyz", merged_into="itm_gone")
    assert query_match_reasons(leaky, "abc123") is None
    assert query_match_reasons(leaky, "doc_xyz") is None
    assert query_match_reasons(leaky, "itm_gone") is None


def test_inventory_search_preserves_user_facing_fields():
    """The user-facing text fields a person reads on an item stay searchable - the #306
    fix must not narrow this. status_doc_number in particular is what sold and consigned
    items are traced by (test_fulfillment / test_doc_workflows assert this end to end)."""
    cases = [
        ("name", "Special Widget", "special"),
        ("sku", "WDGT-9", "wdgt-9"),
        ("barcode", "5901234", "5901234"),
        ("description", "brushed nickel", "nickel"),
        ("short_description", "left-hand thread", "thread"),
        ("category", "Fasteners", "fasteners"),
        ("notes", "handle with care", "care"),
        ("hs_code", "7318.15", "7318"),
        ("batch_no", "B-2026-07", "2026-07"),
        ("purchase_name", "Hex Bolt M8", "hex bolt"),
        ("purchase_sku", "SUP-HB-M8", "sup-hb"),
        ("location_name", "Aisle 4 Bay 2", "aisle 4"),
        ("sell_by", "carat", "carat"),
        ("unit", "kilogram", "kilogram"),
        ("weight_unit", "gram", "gram"),
        ("gross_weight_unit", "ounce", "ounce"),
        ("purchase_unit", "dozen", "dozen"),
        ("inventory_type", "consignment", "consignment"),
        ("status_doc_number", "SO-1042", "so-1042"),
        ("lot", "LOT-77A", "lot-77a"),
    ]
    for field, value, term in cases:
        assert query_match_reasons(_item(**{field: value}), term) == [(field, term)], field
        assert item_matches_query(_item(**{field: value}), term), field
    # A dynamic category attribute is not a core key, so it stays searchable too.
    assert query_match_reasons(_item(color="ruby red"), "ruby") == [("color", "ruby")]

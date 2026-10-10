# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""One rule matches a receipt (or return) entry to its document line, for every reader:
the projection, the receiving and return routes, the import, and the opening balances an
imported purchase order carries (document_lines.received_line_index)."""
from __future__ import annotations

import celerp.services.document_lines as document_lines
from celerp.services.document_lines import received_line_index

_TWO = [{"line_id": "L1", "item_id": "X", "sku": "S", "quantity": 1, "unit_price": 5.0},
        {"line_id": "L2", "item_id": "X", "sku": "S", "quantity": 1, "unit_price": 9.0}]


def test_an_entry_recorded_with_its_line_id_names_that_line_wherever_it_now_sits():
    """The lines moved after the receipt: its stored position is 0, its line is L2."""
    entry = {"source_line_id": "L2", "po_line_index": 0, "item_id": "X", "sku": "S", "quantity_received": 1}
    assert received_line_index(_TWO, entry) == 1


def test_there_is_one_rule():
    """The second matcher is gone, so no reader can resolve the same entry differently."""
    assert not hasattr(document_lines, "doc_line_index")


def test_an_older_entry_is_trusted_at_its_position_while_that_line_holds_its_goods():
    """Neighbour: no line id; the line at the stored position holds the goods."""
    assert received_line_index(_TWO, {"po_line_index": 1, "item_id": "X", "sku": "S"}) == 1


def test_an_older_entry_whose_position_moved_finds_the_one_line_holding_its_goods():
    """Neighbour: the line at the stored position now holds other goods; the one line
    holding the entry's goods is its line, matched by item or by SKU."""
    lines = [{"item_id": "Y", "sku": "T"}, {"item_id": "X", "sku": "S"}]
    assert received_line_index(lines, {"po_line_index": 0, "item_id": "X"}) == 1
    assert received_line_index(lines, {"po_line_index": 0, "sku": "S"}) == 1


def test_two_lines_holding_the_goods_leave_an_older_entry_untold():
    """Neighbour: never a guess between two lines."""
    assert received_line_index(_TWO, {"po_line_index": 5, "item_id": "X", "sku": "S"}) is None


def test_the_projection_and_the_routes_read_the_same_rule():
    """Neighbour: the documents module reads the shared rule, not a copy."""
    from celerp_docs import doc_projections

    assert doc_projections.received_line_index is received_line_index

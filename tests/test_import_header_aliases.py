# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A column is suggested for the field its header names, however the header is spelled:
case, spacing, separators and harmless punctuation never change what it names, and
nothing else is guessed."""

from __future__ import annotations

import pytest

from celerp.importers.tabular import MAPPING_ATTRIBUTE, normalize_header, suggest_mapping

TARGETS = ["sku", "name", "quantity", "sell_by", "cost_price", "retail_price", "category"]


@pytest.mark.parametrize("spelling", ["Qty On-Hand", "qty_on_hand", "QTY  (on hand)", "Qty. on hand", "ｑｔｙ on hand"])
def test_spellings_of_one_header_read_the_same(spelling):
    assert normalize_header(spelling) == "qty_on_hand"


def test_different_headers_never_match():
    assert normalize_header("Qty on hand") != normalize_header("Qty on order")
    assert normalize_header("Cost") != normalize_header("Costs")


@pytest.mark.parametrize("header,target", [
    ("Item Name", "name"), ("SKU", "sku"), ("Code", "sku"), ("Qty on hand", "quantity"),
    ("Unit", "sell_by"), ("Cost", "cost_price"), ("Selling Price", "retail_price"),
    ("item-name", "name"), ("SELLING  PRICE", "retail_price"), ("Qty (on hand)", "quantity"),
])
def test_common_headers_are_suggested_for_their_field(header, target):
    assert suggest_mapping([header], TARGETS) == {header: target}


def test_a_header_close_to_a_field_but_not_it_is_not_guessed():
    assert suggest_mapping(["Item Nam"], TARGETS) == {"Item Nam": MAPPING_ATTRIBUTE}

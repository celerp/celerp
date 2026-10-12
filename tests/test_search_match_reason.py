# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The sentence behind the "?" icon on a searched inventory row: which field matched
and what it matched, in the user's display label and language."""
from __future__ import annotations

import pytest

from ui import i18n
from ui.components.table import match_reason_text


@pytest.fixture(autouse=True)
def _english():
    i18n.set_lang("en")
    yield
    i18n.set_lang("en")


def test_match_reason_text_exact_and_partial():
    """Red statement: the helper did not exist. A whole-value hit reads "Matched",
    a part hit names what the value contains."""
    exact = {"barcode": "7436", "q_match": [{"field": "barcode", "match": "7436"}]}
    part = {"barcode": "9907436", "q_match": [{"field": "barcode", "match": "7436"}]}
    labels = {"barcode": "Barcode"}
    assert match_reason_text(exact, labels) == "Matched Barcode: 7436"
    assert match_reason_text(part, labels) == "Barcode contains 7436"


def test_match_reason_exact_identifier_hit_reads_matched():
    """An identifier hit under its canonical key (q_exact) is a whole-value match even
    when the typed text differs in form from the stored value."""
    row = {"barcode": "07436", "q_exact": "barcode",
           "q_match": [{"field": "barcode", "match": "7436"}]}
    assert match_reason_text(row, {"barcode": "Barcode"}) == "Matched Barcode: 07436"


def test_match_reason_uses_custom_field_label():
    """A custom field shows the label the user gave it, never its key."""
    row = {"grade_x": "A+ select", "q_match": [{"field": "grade_x", "match": "A+"}]}
    assert match_reason_text(row, {"grade_x": "Grade"}) == "Grade contains A+"


def test_match_reason_falls_back_to_the_built_in_label():
    """A searched field outside the table schema (unit, lot, document number) still
    reads as a label, never the raw key."""
    assert match_reason_text({"lot": "L-55", "q_match": [{"field": "lot", "match": "55"}]}, {}) \
        == "Lot contains 55"
    assert match_reason_text({"status_doc_number": "INV-0042",
                              "q_match": [{"field": "status_doc_number", "match": "0042"}]}, {}) \
        == "Doc # contains 0042"
    assert match_reason_text({"unit": "box", "q_match": [{"field": "unit", "match": "box"}]}, {}) \
        == "Matched Unit: box"


def test_match_reason_numbers_read_plainly():
    row = {"quantity": 12.0, "q_match": [{"field": "quantity", "match": "12"}]}
    assert match_reason_text(row, {"quantity": "Qty"}) == "Matched Qty: 12"


def test_match_reason_joins_several_reasons():
    row = {"name": "Ruby ring", "barcode": "7436",
           "q_match": [{"field": "name", "match": "ruby"}, {"field": "barcode", "match": "7436"}]}
    assert match_reason_text(row, {"name": "Name", "barcode": "Barcode"}) \
        == "Name contains ruby; Matched Barcode: 7436"


def test_match_reason_hidden_field_renders_no_text():
    """A reason whose field the row does not carry (hidden by field visibility) gives no
    sentence, never an invented one."""
    assert match_reason_text({"q_match": [{"field": "barcode", "match": "7436"}]}, {}) is None
    assert match_reason_text({"barcode": "", "q_match": [{"field": "barcode", "match": "7"}]}, {}) is None
    assert match_reason_text({}, {}) is None


def test_match_reason_is_translated():
    """Red statement: the catalog keys did not exist."""
    i18n.set_lang("es")
    row = {"barcode": "9907436", "q_match": [{"field": "barcode", "match": "7436"}]}
    text = match_reason_text(row, {"barcode": "Código de barras"})
    assert text and "contains" not in text and "7436" in text and "Código de barras" in text

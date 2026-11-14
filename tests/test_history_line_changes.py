"""History's line rows: each line is matched by its line id (a line without one, by its
position), every stored field of the line is compared, and a change no field row can show
reads as a summary row."""
from __future__ import annotations

from ui.components.activity import EMPTY, field_changes, history_rows

A, B, C = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
           "33333333-3333-4333-8333-333333333333")


def _rows(old, new):
    return [c[1:] for c in field_changes({"fields_changed": {"line_items": {"old": old, "new": new}}},
                                         "doc.updated", "USD")]


def _line(lid, **kw):
    return {"line_id": lid, "sku": "RB-1", "name": "Ruby", "quantity": 1, "unit_price": 100, **kw}


def test_two_lines_of_one_sku_stay_two_lines():
    rows = _rows([_line(A), _line(B)], [_line(A), _line(B, quantity=5)])
    assert rows == [("Item 2: Ruby · Qty", "1", "5")]


def test_discount_only_change_is_shown():
    assert _rows([_line(A)], [_line(A, discount_pct=10)]) == [("Item 1: Ruby · Disc %", EMPTY, "10%")]


def test_description_change_is_shown():
    assert _rows([_line(A, description="Red")], [_line(A, description="Deep red")]) == [
        ("Item 1: Ruby · Description", "Red", "Deep red")]


def test_tax_only_change_is_shown():
    vat = [{"code": "VAT", "rate": 7, "amount": 0, "order": 0, "is_compound": False, "label": ""}]
    assert _rows([_line(A, taxes=[])], [_line(A, taxes=vat)]) == [("Item 1: Ruby · Tax %", EMPTY, "VAT 7%")]
    assert _rows([_line(A, tax_rate=0)], [_line(A, tax_rate=7)]) == [("Item 1: Ruby · Tax %", "0%", "7%")]


def test_account_change_is_shown():
    assert _rows([_line(A, account="4000")], [_line(A, account="4100")]) == [
        ("Item 1: Ruby · Account", "4000", "4100")]


def test_reorder_reads_as_one_summary_row():
    assert _rows([_line(A), _line(B, name="Pearl")], [_line(B, name="Pearl"), _line(A)]) == [
        ("Lines reordered", EMPTY, EMPTY)]


def test_added_and_removed_lines():
    rows = _rows([_line(A), _line(B, name="Pearl", quantity=2)], [_line(A), _line(C, name="Opal", quantity=3)])
    assert rows == [("Item 2: Opal", EMPTY, "3 @ $100.00"), ("Item 2: Pearl", "2 @ $100.00", EMPTY)]


def test_legacy_lines_without_ids_match_by_position_and_never_merge():
    old = [{"sku": "RB-1", "name": "Ruby", "quantity": 1}, {"sku": "RB-1", "name": "Ruby", "quantity": 2}]
    new = [{"sku": "RB-1", "name": "Ruby", "quantity": 1}, {"sku": "RB-1", "name": "Ruby", "quantity": 3}]
    assert _rows(old, new) == [("Item 2: Ruby · Qty", "2", "3")]
    # Dropping one of two identical lines is a removal, never "no change".
    assert _rows(old, old[:1]) == [("Item 2: Ruby", "2", EMPTY)]


def test_a_repeated_line_id_never_merges_two_lines():
    assert _rows([_line(A), _line(A)], [_line(A), _line(A, quantity=4)]) == [("Item 2: Ruby · Qty", "1", "4")]


def test_several_fields_in_one_event_each_get_a_row():
    rows = _rows([_line(A)], [_line(A, quantity=2, unit_price=90, description="Cut")])
    assert rows == [("Item 1: Ruby · Qty", "1", "2"), ("Item 1: Ruby · Unit Price", "$100.00", "$90.00"),
                    ("Item 1: Ruby · Description", EMPTY, "Cut")]


def test_a_change_no_field_row_can_show_reads_as_a_summary():
    assert _rows([_line(A)], [_line(A), "garbled"]) == [("Lines edited", EMPTY, EMPTY)]
    assert _rows([_line(A)], [_line(A, pieces={"odd": 1})]) == [("Item 1: Ruby · Pieces", EMPTY, "Pieces updated")]


def test_line_rows_carry_the_line_items_field_key():
    e = {"id": 1, "entity_id": "doc:INV-1", "actor_name": "Ana", "ts": "2026-01-01T00:00:00Z", "event_type": "doc.updated",
         "data": {"fields_changed": {"line_items": {"old": [_line(A)], "new": [_line(A, discount_pct=5)]}}}}
    assert [r["field_key"] for r in history_rows([e])] == ["line_items"]

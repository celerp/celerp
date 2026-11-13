"""Accounting item rows name a line the one way every other surface names it (line_label)."""
import pytest
from celerp.services.line_measures import line_label
from celerp_accounting.routes import _doc_item_lines


@pytest.mark.parametrize('extra', [{'sku': '000001', 'name': 'Steel rod'},
                                   {'sku': '000001', 'description': 'Polished rod', 'name': 'Steel rod'},
                                   {'description': 'Polished rod', 'name': 'Steel rod'},
                                   {'name': 'Steel rod', 'item_id': 'item:abc'},
                                   {'sku': '000001'}])
def test_accounting_item_row_is_named_by_line_label_and_keeps_its_unit(extra):
    line = {**extra, 'quantity': 20, 'unit': 'kg', 'unit_price': 2, 'line_total': 40}
    rows = _doc_item_lines({'currency': 'USD', 'line_items': [line]}, 'USD')
    assert rows[0]['item'] == line_label(line) and rows[0]['quantity'] == 20 and rows[0]['unit'] == 'kg'
    assert 'item:' not in rows[0]['item']

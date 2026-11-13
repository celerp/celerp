"""Accounting display rows prefer names without changing stored line identifiers."""
import pytest
from celerp_accounting.routes import _doc_item_lines

@pytest.mark.parametrize('extra,expected', [({'sku':'000001','name':'Steel rod'},'Steel rod'),({'sku':'000001','description':'Polished rod','name':'Steel rod'},'Polished rod'),({'sku':'000001'},'000001')])
def test_accounting_item_row_uses_a_name_then_a_code_fallback(extra, expected):
    line={**extra,'quantity':20,'unit':'kg','unit_price':2,'line_total':40}
    rows=_doc_item_lines({'currency':'USD','line_items':[line]},'USD')
    assert rows[0]['item']==expected and rows[0]['quantity']==20 and rows[0]['unit']=='kg'
    assert line.get('sku')=='000001'

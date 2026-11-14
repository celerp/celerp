"""Shared payment cards use icons, standard dates and aligned actions."""
import pytest
from bs4 import BeautifulSoup
from fasthtml.common import to_xml
from ui.routes.documents import _payment_section
from ui.components.table import format_value

@pytest.mark.parametrize('kind', ['invoice', 'bill', 'credit_note', 'debit_note'])
@pytest.mark.parametrize('count', [0, 1, 3])
def test_payment_card_has_standard_dates_shared_icon_and_action_container(kind, count):
    payments=[{'index':i,'payment_date':'2026-11-07T09:30:00Z','amount':5,'method':'cash','status':'active'} for i in range(count)]
    doc={'entity_id':'doc:1','doc_type':kind,'status':'final','total':100,'amount_outstanding':100-5*count,'amount_paid':5*count,'payments':payments}
    html=to_xml(_payment_section(doc))
    section=BeautifulSoup(html,'html.parser')
    assert section.select_one('.section-icon svg')
    assert not any(ord(c) in (0x1f4b0,0x1f4b3) for c in html)
    rows=section.select('table tbody tr')
    assert len(rows)==count
    for row in rows:
        assert row.select_one('td').text==format_value('2026-11-07T09:30:00Z','date')
    if kind in ('invoice','bill'):
        assert section.select_one('.form-actions button[type=submit]')


def test_payment_with_no_date_does_not_break_the_card():
    html=to_xml(_payment_section({'entity_id':'doc:1','doc_type':'invoice','status':'final','total':10,'payments':[{'index':0,'amount':1,'recorded_at':None}]}))
    assert 'doc-payments' in html

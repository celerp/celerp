"""Shared controls stay attached to fields and persist new document lines."""
import pytest
from fasthtml.common import to_xml
from playwright.sync_api import expect
from ui.components.table import searchable_select

pytestmark = pytest.mark.browser

@pytest.mark.parametrize('place', ['header', 'row', 'bottom'])
def test_dropdown_anchors_in_a_containing_block_and_tracks_scroll(page, ui_server, place):
    page.goto(ui_server + '/dashboard')
    html = to_xml(searchable_select('selected', [(str(i), f'Choice {i}') for i in range(20)]))
    page.evaluate('''({html, place}) => {
      document.body.insertAdjacentHTML('beforeend', '<div id="test-card" style="position:fixed;left:300px;top:'+(place==='bottom'?'65vh':'70px')+';width:300px;height:180px;overflow:auto;transform:translateZ(0)"><div style="height:800px"><div id="test-control" style="padding:12px">'+html+'</div></div></div>');
      initCombobox(document.querySelector('#test-control .combobox-wrap'));
    }''', {'html': html, 'place': place})
    inp = page.locator('#test-control .combobox-input')
    inp.click()
    panel = page.locator('#test-control .combobox-list')
    expect(panel).to_be_visible()
    def anchored():
        a, b = inp.bounding_box(), panel.bounding_box()
        assert abs(b['x']-a['x']) <= 2
        assert min(abs(b['y']-a['y']-a['height']), abs(b['y']+b['height']-a['y'])) <= 5, (a, b)
    anchored()
    if place == 'bottom':
        assert panel.bounding_box()['y'] < inp.bounding_box()['y']
    page.locator('#test-card').evaluate('(e) => {e.scrollTop=8}')
    page.wait_for_timeout(100)
    anchored()

@pytest.mark.parametrize('resource,kind', [('lists','quotation'), ('docs','invoice'), ('docs','purchase_order'), ('docs','memo')])
@pytest.mark.parametrize('populated', [False, True])
def test_add_document_row_is_editable_and_persists_totals(page, ui_server, api, resource, kind, populated):
    lines = [{'name':'Existing', 'quantity':1, 'unit_price':3}] if populated else []
    r=api.post('/'+resource, json={('list_type' if resource=='lists' else 'doc_type'):kind,'line_items':lines})
    assert r.status_code==200, r.text
    eid=r.json()['id']; errors=[]
    page.on('pageerror', lambda err: errors.append(str(err)))
    page.goto(f'{ui_server}/{resource}/{eid}')
    before=page.locator('#line-body tr').count()
    page.locator('button[onclick="celerpAddLine()"]').click()
    expect(page.locator('#line-body tr')).to_have_count(before+1)
    assert not errors, errors
    row=page.locator('#line-body tr').last
    row.locator('[data-name="description"]').fill('Added service')
    row.locator('[data-name="quantity"]').fill('2')
    row.locator('[data-name="unit_price"]').fill('7')
    row.locator('[data-name="unit_price"]').press('Tab')
    page.evaluate('celerpAutoSave()')
    page.wait_for_function('window._celerpSavedLineRevision >= window._celerpLineRevision')
    state=api.get(f'/{resource}/{eid}').json()
    assert state['line_items'][-1].get('description') == 'Added service'
    assert float(state['total']) == (17 if populated else 14)

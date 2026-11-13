"""Bounded notes, normal History contrast and live record selector links."""
import pytest
from fasthtml.common import to_xml
from playwright.sync_api import expect
from ui.components.notes import notes_tab, note_edit_form
from ui.components.table import searchable_select
from ui.components.activity import history_table

pytestmark=pytest.mark.browser

def mount(page, ui_server, html):
    page.goto(ui_server+'/dashboard')
    page.evaluate('(html)=>{const e=document.createElement("section");e.id="polish-test";e.innerHTML=html;document.body.append(e)}',html)

@pytest.mark.parametrize('long', [False, True])
def test_full_note_remains_readable_and_editable_with_bounded_height(page, ui_server, long):
    text='A note\n'*(300 if long else 1)
    html=to_xml(notes_tab(entity_id='doc:1',notes=[{'note_id':'n1','note':text}],add_url='/noop',edit_url='/noop/{note_id}',delete_url='/noop/{note_id}',refresh_target='#polish-test'))
    html+=to_xml(note_edit_form(note_id='n2',current_text=text,save_url='/noop',cancel_url='/noop',refresh_target='#polish-test'))
    mount(page,ui_server,html)
    note=page.locator('#polish-test .note-text'); box=note.bounding_box()
    assert box['height'] <= 193
    assert note.text_content()==text
    if long:
        assert note.evaluate('(e)=>e.scrollHeight>e.clientHeight')
        note.evaluate('(e)=>e.scrollTop=e.scrollHeight')
        assert note.evaluate('(e)=>e.scrollTop>0')
    editor=page.locator('#polish-test textarea').last
    expect(editor).to_have_value(text)
    editor.fill(text+'Edited')
    expect(editor).to_have_value(text+'Edited')


def test_history_field_values_keep_normal_colour_without_disabled_opacity(page, ui_server):
    ledger=[{'id':1,'event_type':'doc.updated','ts':'2026-11-07','data':{'fields_changed':{'customer_note':{'old':'Old','new':'New'},'due_date':{'old':'2026-11-07','new':'2026-11-08'}}}}]
    mount(page,ui_server,to_xml(history_table(ledger)))
    cells=page.locator('#polish-test .col-old, #polish-test .col-new, #polish-test .col-field')
    assert cells.count()>=6
    for cell in cells.all():
        assert cell.evaluate('(e)=>getComputedStyle(e).opacity')=='1'
        assert cell.evaluate('(e)=>getComputedStyle(e).color===getComputedStyle(document.body).color')


def test_record_link_follows_selection_and_disappears_when_cleared(page, ui_server):
    mount(page,ui_server,to_xml(searchable_select('contact_id',[('contact:1','Customer One')],value='')))
    page.evaluate('initCombobox(document.querySelector("#polish-test .combobox-wrap"))')
    link=page.locator('#polish-test .combobox-record-link')
    expect(link).to_be_hidden()
    page.locator('#polish-test .combobox-input').click()
    page.locator('#polish-test .combobox-option',has_text='Customer One').click()
    expect(link).to_have_attribute('href','/contacts/contact%3A1')
    expect(link).to_be_visible()
    page.locator('#polish-test .combobox-input').fill('')
    expect(link).to_be_hidden()

@pytest.mark.parametrize('kind', ['invoice', 'bill', 'credit_note', 'debit_note'])
@pytest.mark.parametrize('count', [0,3])
def test_payment_actions_align_with_their_form_rows(page, ui_server, kind, count):
    from ui.routes.documents import _payment_section
    payments=[{'index':i,'payment_date':'2026-11-07','amount':5,'method':'cash'} for i in range(count)]
    doc={'entity_id':'doc:1','doc_type':kind,'status':'final','total':100,'amount_outstanding':100-5*count,'payments':payments}
    mount(page,ui_server,to_xml(_payment_section(doc)))
    forms=page.locator('#polish-test .payment-form-section form')
    assert forms.count()>=1
    for form in forms.all():
        row=form.locator('.form-row').first.bounding_box()
        button=form.locator('button[type=submit]').last.bounding_box()
        assert abs(row['x']-button['x'])<=1, (row,button)
        assert button['y']>=row['y']+row['height']

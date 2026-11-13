import pytest
from playwright.sync_api import expect
pytestmark=pytest.mark.browser

@pytest.fixture
def item(api):
    r=api.post('/items',json={'sku':'SPLIT-VALIDATION','name':'Validation item','quantity':10,'status':'available','sell_by':'piece','allow_splitting':True})
    assert r.status_code==200,r.text
    return r.json()['id']

@pytest.mark.parametrize('value',['0','','-1'])
def test_invalid_enter_has_no_split_request(page,ui_server,item,value):
    calls=[];page.on('request',lambda r:calls.append(r.url) if '/split-inline' in r.url else None)
    page.goto(f'{ui_server}/inventory/{item}',wait_until='domcontentloaded')
    page.locator('#item-advanced-panel > summary').click()
    field=page.locator('[name=child_qty]').first
    field.fill(value);field.press('Enter')
    expect(field).to_be_focused()
    assert calls==[]
    expect(page.locator('#item-action-error')).to_be_empty()


def test_valid_confirm_splits_and_preserves_total(page,ui_server,api,item):
    page.goto(f'{ui_server}/inventory/{item}',wait_until='domcontentloaded')
    page.locator('#item-advanced-panel > summary').click()
    form=page.locator('form[data-validates-split]').first
    form.locator('[name=child_qty]').fill('3')
    expect(form.locator('.sp-total-qty')).to_have_text('10')
    with page.expect_response('**/split-inline') as response:form.locator('.sp-confirm-btn').click()
    assert response.value.ok
    assert api.get(f'/items/{item}').json()['quantity']==7

@pytest.mark.parametrize('field,value',[('batch_qty','0'),('batch_qty','-1'),('batch_qty',''),('batch_count','0'),('batch_count','-2'),('batch_count','2.5'),('batch_qty','nonnumeric'),('batch_complement','0'),('batch_complement','-1'),('batch_complement','nonnumeric')])
def test_invalid_batch_never_posts(page,ui_server,item,field,value):
    calls=[];page.on('request',lambda r:calls.append(r.url) if '/batch-split' in r.url else None)
    page.goto(f'{ui_server}/inventory/{item}',wait_until='domcontentloaded')
    page.locator('#item-advanced-panel > summary').click()
    page.locator('.split-mode').select_option('batch')
    form=page.locator('form').filter(has=page.locator('[name=batch_count]'))
    form.locator('[name=batch_qty]').fill('2');form.locator('[name=batch_count]').fill('2')
    if value=='nonnumeric':form.locator('[name='+field+']').press_sequentially('e')
    else:form.locator('[name='+field+']').fill(value)
    form.locator('.btn--batch-submit').click()
    expect(form.locator('[name='+field+']')).to_be_focused()
    assert calls==[]

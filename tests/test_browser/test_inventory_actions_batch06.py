import pytest
from playwright.sync_api import expect
from .conftest import answer_in_app
pytestmark = pytest.mark.browser

@pytest.fixture
def action_item(api):
    r = api.post('/items', json={'sku': 'ACTIONS-ITEM', 'name': 'Actions item', 'status': 'available',
                                'quantity': 10, 'sell_by': 'piece', 'allow_splitting': True})
    assert r.status_code == 200, r.text
    return r.json()['id']


def test_split_modes_and_collapse_at_phone_width(page, ui_server, action_item):
    page.set_viewport_size({'width': 390, 'height': 844})
    page.goto(f'{ui_server}/inventory/{action_item}', wait_until='domcontentloaded')
    actions = page.locator('#item-advanced-panel')
    # The card opens expanded with manual split shown; it collapses and reopens from its header.
    expect(actions.locator('[data-split-mode=manual]')).to_be_visible()
    actions.locator('summary').click()
    expect(actions.locator('.split-mode')).not_to_be_visible()
    actions.locator('summary').click()
    expect(actions.locator('[data-split-mode=manual]')).to_be_visible()
    expect(actions.get_by_role('button', name='Confirm', exact=True)).to_have_count(1)
    assert actions.get_by_role('button', name='Confirm', exact=True).evaluate_all('(bs)=>bs.filter(b=>b.checkVisibility()).length') == 1
    actions.locator('.split-mode').select_option('batch')
    expect(actions.locator('[data-split-mode=manual]')).not_to_be_visible()
    expect(actions.locator('[name=batch_count]')).to_be_visible()
    assert actions.get_by_role('button', name='Confirm', exact=True).evaluate_all('(bs)=>bs.filter(b=>b.checkVisibility()).length') == 1
    actions.locator('summary').click()
    expect(actions.locator('[name=batch_count]')).not_to_be_visible()


@pytest.mark.parametrize('operation,status', [('expire', 'expired'), ('archive', 'archived'), ('revert-to-draft', 'draft')])
def test_lifecycle_requires_confirmation(page, ui_server, api, action_item, operation, status):
    page.goto(f'{ui_server}/inventory/{action_item}', wait_until='domcontentloaded')
    form = page.locator(f'form[hx-post="/api/items/{action_item}/{operation}"]')
    form.locator('button').click()
    expect(page.locator('dialog.confirm-dialog[open]')).to_be_visible()
    assert api.get(f'/items/{action_item}').json()['status'] == 'available'
    answer_in_app(page, False)
    assert api.get(f'/items/{action_item}').json()['status'] == 'available'
    form.locator('button').click()
    with page.expect_response(f"**/api/items/{action_item}/{operation}") as response:
        answer_in_app(page, True)
    assert response.value.ok
    expect(page.locator('dialog.confirm-dialog[open]')).to_have_count(0)
    page.wait_for_url(f'**/inventory/{action_item}', wait_until='domcontentloaded')
    expect(page.locator('form[hx-post$="/revert-to-draft"]')).to_have_count(0) if status == 'draft' else None
    # The redirect completes only after the API accepted the unchanged operation.
    assert api.get(f'/items/{action_item}').json()['status'] == status

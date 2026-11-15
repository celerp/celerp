"""Action layout keeps the existing backend permissions and operations."""
import pytest
from bs4 import BeautifulSoup
from fasthtml.common import to_xml
from ui.routes.inventory import _advanced_panel


def test_actions_use_shared_collapsible_card_and_confirmation_pattern():
    item = {'quantity': 1, 'status': 'available'}
    card = BeautifulSoup(to_xml(_advanced_panel('item:test', item)), 'html.parser')
    assert card.select_one('details.doc-section#item-advanced-panel > summary.section-header')
    for action in ('expire', 'archive', 'revert-to-draft'):
        form = card.select_one(f'form[hx-post$="/{action}"]')
        assert form and form['hx-confirm']
    restricted = to_xml(_advanced_panel('item:test', item, role='viewer'))
    assert '/revert-to-draft' not in restricted


@pytest.mark.asyncio
async def test_viewer_cannot_run_item_actions(client, session):
    from test_entity_files import _headers
    from test_helpers import invite_user
    owner = await _headers(client)
    r = await client.post('/items', headers=owner, json={'sku': 'NO-ACTIONS', 'name': 'Read only',
                                                       'status': 'available', 'quantity': 10, 'sell_by': 'piece'})
    assert r.status_code == 200, r.text
    iid = r.json()['id']
    token = await invite_user(client, session, owner, 'inventory-viewer@example.com', 'viewer')
    viewer = {'Authorization': f'Bearer {token}'}
    for path, body in [(f'/items/{iid}/expire', None), (f'/items/{iid}/status', {'status': 'archived'}),
                       ('/items/bulk/revert-to-draft', {'entity_ids': [iid]}),
                       (f'/items/{iid}/split', {'children': [{'sku': 'NO-CHILD', 'quantity': 1}]})]:
        response = await client.post(path, headers=viewer, json=body)
        assert response.status_code == 403, (path, response.text)
    assert (await client.get(f'/items/{iid}', headers=owner)).json()['status'] == 'available'

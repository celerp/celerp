import pytest
from bs4 import BeautifulSoup
from fasthtml.common import to_xml
from ui.routes.inventory import _inventory_toolbar
from test_inventory_content_degradation import _install_inventory_getters
from test_ui import ui_client, _authed

@pytest.mark.parametrize('status',['sold','archived','disposed','available','','reserved','draft'])
@pytest.mark.parametrize('edit',[False,True])
@pytest.mark.parametrize('exchange',[False,True])
def test_status_and_permissions_determine_toolbar(status,edit,exchange):
    settings={'role_grants':{'edit_inventory':['viewer'] if edit else [],'import_export_data':['viewer'] if exchange else []}}
    toolbar=BeautifulSoup(to_xml(_inventory_toolbar({'status':status},settings,'viewer')),'html.parser')
    read_only=status in {'sold','archived','disposed'}
    assert bool(toolbar.select_one('[hx-post="/inventory/create-blank"]'))==(edit and not read_only)
    assert bool(toolbar.select_one('a[href="/inventory/import"]'))==(exchange and not read_only)
    assert bool(toolbar.select_one('a[href*="category-library"]'))==(exchange and not read_only)
    assert bool(toolbar.select_one('a[href^="/inventory/export/csv"]'))==exchange

@pytest.mark.asyncio
@pytest.mark.parametrize('status',['sold','archived','disposed','available'])
async def test_direct_page_and_status_fragment_use_the_same_toolbar(ui_client,monkeypatch,status):
    _install_inventory_getters(monkeypatch)
    ui_client.cookies.update(_authed())
    for url,headers in [(f'/inventory?status={status}',{}),(f'/inventory/content?status={status}',{'HX-Request':'true'})]:
        r=await ui_client.get(url,headers=headers)
        assert r.status_code==200,r.text
        toolbar=BeautifulSoup(r.text,'html.parser').select_one('#inventory-toolbar-actions')
        assert toolbar
        assert bool(toolbar.select_one('[hx-post="/inventory/create-blank"]'))==(status=='available')
        assert toolbar.select_one('a[href^="/inventory/export/csv"]')
        if headers:assert toolbar['hx-swap-oob']=='outerHTML'

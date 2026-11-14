import pytest
from fasthtml.common import to_xml
from ui.routes.inventory import _split_table_form
from test_entity_files import _headers

@pytest.mark.asyncio
@pytest.mark.parametrize('sell_by,weight,attrs,expected',[('piece',None,{},None),('piece',None,{'weight_g':12},12),('piece',None,{'weight':12},None),('piece',12,{},12),('gram',None,{},10)])
async def test_split_weight_source_is_net_weight_or_weight_quantity(client,sell_by,weight,attrs,expected):
    h=await _headers(client)
    data={'sku':'TRACE-WEIGHT','name':'Weight trace','status':'available','sell_by':sell_by,'quantity':10,'gross_weight':10,'attributes':attrs}
    if weight is not None:data.update(weight=weight,weight_unit='gram')
    r=await client.post('/items',headers=h,json=data);assert r.status_code==200,r.text
    eid=r.json()['id']
    preview=(await client.get(f'/items/{eid}/split-preview',headers=h)).json()
    assert preview.get('parent_weight')==expected,preview
    html=to_xml(_split_table_form(preview,action='/split',target='#result',form_id='test-split',allow_add_child=True,child_sku_editable=False))
    if expected is None:
        assert 'No net weight is recorded' in html and 'sp-total-weight' not in html
    elif sell_by=='piece':
        assert '12.00' in html

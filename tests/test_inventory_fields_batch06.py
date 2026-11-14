"""Inventory fields: core fields own weight and origin, including values earlier presets stored."""
import csv
import io
import json
from pathlib import Path

import pytest
from fasthtml.common import to_xml
from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA
from celerp_inventory.projections import LEGACY_CATEGORY_FIELDS
from ui.components.item_fields import item_detail_schema
from ui.components.table import paired_display_cell
from ui.i18n import field_label


def test_old_preset_fields_never_add_a_second_editor():
    schema = item_detail_schema(DEFAULT_ITEM_SCHEMA, [{'key': 'weight_g'}, {'key': 'origin_country'}], 'packaged_food')
    keys = [f['key'] for f in schema]
    assert keys.count('weight') == keys.count('country_of_origin') == 1
    assert not set(LEGACY_CATEGORY_FIELDS) & set(keys)


async def _legacy_item(client, headers, sku='OLD-1', **core):
    """An item as earlier releases stored it: the old preset keys under attributes."""
    response = await client.post('/items', headers=headers, json={
        'sku': sku, 'name': 'Old rice bag', 'category': 'packaged_food', 'quantity': 5, 'sell_by': 'piece',
        'attributes': {'brand': 'Acme', 'weight_g': 500, 'origin_country': 'Thailand'}, **core,
    })
    assert response.status_code == 200, response.text
    return response.json()['id']


def _no_legacy_keys(item):
    keys = set(item) | set(item.get('attributes') or {})
    assert not keys & set(LEGACY_CATEGORY_FIELDS), keys
    assert not [k for k in keys if k.startswith('_legacy')], keys


@pytest.mark.asyncio
async def test_old_preset_values_are_stored_in_the_core_fields(client):
    from test_entity_files import _headers
    headers = await _headers(client)
    stored = (await client.get('/items/' + await _legacy_item(client, headers), headers=headers)).json()
    assert (stored['weight'], stored['weight_unit'], stored['country_of_origin']) == (500, 'gram', 'Thailand')
    _no_legacy_keys(stored)
    kept = (await client.get('/items/' + await _legacy_item(client, headers, 'OLD-2', weight=4, weight_unit='kilogram',
                                                             country_of_origin='Laos'), headers=headers)).json()
    assert (kept['weight'], kept['weight_unit'], kept['country_of_origin']) == (4, 'kilogram', 'Laos')
    _no_legacy_keys(kept)


@pytest.mark.asyncio
async def test_old_preset_values_reach_the_csv_export(client):
    from test_entity_files import _headers
    headers = await _headers(client)
    await _legacy_item(client, headers)
    response = await client.get('/items/export/csv', headers=headers,
                                params={'category': 'packaged_food', 'cols': 'sku,weight,country_of_origin'})
    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.text)))
    row = next(r for r in rows if r.get('sku') == 'OLD-1')
    assert float(row['weight']) == 500 and row['country_of_origin'] == 'Thailand', row


@pytest.mark.asyncio
async def test_duplicate_of_an_old_item_stores_only_core_fields(client):
    from test_entity_files import _headers
    from ui.routes.inventory import _duplicate_payload
    headers = await _headers(client)
    source = (await client.get('/items/' + await _legacy_item(client, headers), headers=headers)).json()
    payload = _duplicate_payload(source, 'OLD-1-copy', can_set_prices=True)
    _no_legacy_keys(payload)
    response = await client.post('/items', headers=headers, json=payload)
    assert response.status_code == 200, response.text
    copy_id = response.json()['id']
    patch = await client.patch(f'/items/{copy_id}', headers=headers, json={'fields_changed': {'weight_unit': {'old': 'gram', 'new': 'kilogram'}}})
    assert patch.status_code == 200, patch.text
    patch = await client.patch(f'/items/{copy_id}', headers=headers, json={'fields_changed': {'weight': {'old': 500, 'new': 2}}})
    assert patch.status_code == 200, patch.text
    copy = (await client.get(f'/items/{copy_id}', headers=headers)).json()
    assert (copy['weight'], copy['weight_unit'], copy['country_of_origin']) == (2, 'kilogram', 'Thailand')


def test_every_category_avoids_core_keys_and_synonyms():
    core = {f['key'] for f in DEFAULT_ITEM_SCHEMA} | set(LEGACY_CATEGORY_FIELDS)
    for path in Path('default_modules/celerp-verticals/celerp_verticals/categories').glob('*.json'):
        keys = {f['key'] for f in json.loads(path.read_text()).get('fields', [])}
        assert not keys & core, (path.name, keys & core)


def test_packaging_label_and_description_help():
    schema = item_detail_schema(DEFAULT_ITEM_SCHEMA, [{'key': 'type', 'label': 'Type'}], 'packaged_food')
    by_key = {f['key']: f for f in schema}
    assert by_key['type']['label_key'] == 'inventory.packaging_type'
    assert field_label(by_key['inventory_type']) == 'Inventory Type'
    assert 'detailed specifications' in by_key['description']['help_text']
    assert 'compact lists' in by_key['short_description']['help_text']


@pytest.mark.parametrize('editable', [True, False])
def test_weight_without_unit_has_no_stray_empty_mark(editable):
    html = to_xml(paired_display_cell('item:test', 'gross_weight', 10, 'gross_weight_unit', None,
                                     hide_empty_secondary=True, secondary_editable=editable))
    assert '>10<' in html
    assert '--' not in html.replace('paired-secondary--readonly', '').replace('cell--paired', '')

@pytest.mark.asyncio
async def test_new_item_keeps_weight_and_origin_in_core_fields(client):
    from test_entity_files import _headers
    headers = await _headers(client)
    response = await client.post('/items', headers=headers, json={
        'sku': 'CORE-ONLY', 'name': 'Core item', 'category': 'packaged_food', 'weight': 10,
        'weight_unit': 'gram', 'country_of_origin': 'Thailand', 'quantity': 1,
    })
    assert response.status_code == 200, response.text
    stored = (await client.get('/items/' + response.json()['id'], headers=headers)).json()
    assert stored['weight'] == 10
    assert stored['country_of_origin'] == 'Thailand'
    assert not set(LEGACY_CATEGORY_FIELDS) & set(stored.get('attributes') or {})


def test_folding_old_preset_values_leaves_the_event_data_as_recorded():
    """The fold moves old keys on the projected lot only; the event it read keeps them, so a
    rebuild that replays the same event folds it the same way again."""
    from celerp_inventory.projections import apply_item_event
    data = {'sku': 'OLD-2', 'name': 'Old rice bag', 'attributes': {'weight_g': '500', 'origin_country': 'Thailand'}}
    state = apply_item_event({}, 'item.created', data)
    assert state['weight'] == 500.0 and state['attributes'] == {'country_of_origin': 'Thailand'}
    assert data['attributes'] == {'weight_g': '500', 'origin_country': 'Thailand'}

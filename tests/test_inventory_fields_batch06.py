"""Inventory field display preserves historical values without rewriting storage."""
import copy
import json
from pathlib import Path

import pytest
from fasthtml.common import to_xml
from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA
from ui.components.item_fields import item_detail_schema, item_display_values, LEGACY_ITEM_ALIASES
from ui.components.table import paired_display_cell
from ui.i18n import field_label


def test_legacy_synonyms_are_display_only_and_canonical_wins():
    original = {'attributes': {'weight_g': 12, 'origin_country': 'Thailand'}}
    before = copy.deepcopy(original)
    shown = item_display_values(original)
    assert (shown['weight'], shown['weight_unit'], shown['country_of_origin']) == (12, 'gram', 'Thailand')
    assert original == before
    assert item_display_values({**original, 'weight': 4, 'weight_unit': 'kilogram'})['weight'] == 4
    assert item_display_values({**original, 'weight': None})['weight'] is None
    schema = item_detail_schema(DEFAULT_ITEM_SCHEMA, [{'key': 'weight_g'}, {'key': 'origin_country'}], 'packaged_food')
    keys = [f['key'] for f in schema]
    assert keys.count('weight') == keys.count('country_of_origin') == 1
    assert not set(LEGACY_ITEM_ALIASES) & set(keys)


def test_every_category_avoids_core_keys_and_synonyms():
    core = {f['key'] for f in DEFAULT_ITEM_SCHEMA} | set(LEGACY_ITEM_ALIASES)
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
    assert not set(LEGACY_ITEM_ALIASES) & set(stored.get('attributes') or {})

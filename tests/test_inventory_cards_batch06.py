import pytest
from bs4 import BeautifulSoup
from fasthtml.common import to_xml
from ui.components.table import fmt_money
from ui.routes.inventory import _recipe_section, _pricing_form, _ledger_table


@pytest.mark.parametrize('currency', ['THB', 'EUR', 'CHF'])
def test_pricing_and_recipe_use_the_company_currency_formatter(currency):
    item = {'quantity': 1, 'sell_by': 'piece', 'cost_price': 1200,
            'recipe': {'components': [{'item_id': 'item:raw', 'quantity': 1}], 'unit_cost': 1200}}
    pricing = BeautifulSoup(to_xml(_pricing_form('item:test', item, [{'name': 'Cost'}], currency)), 'html.parser')
    recipe = BeautifulSoup(to_xml(_recipe_section('item:test', item, [], currency)), 'html.parser')
    assert pricing.select_one('#derived_total_cost_price').get_text() == fmt_money(1200, currency)
    assert fmt_money(1200, currency) in recipe.select_one('#recipe-cost-card').get_text()
    assert not pricing.select('.input-prefix')  # computed spans already carry their formatted currency


def test_recipe_inputs_share_style_and_computed_total_is_an_output():
    card = BeautifulSoup(to_xml(_recipe_section('item:test', {}, [], 'THB')), 'html.parser')
    for name in ('labor_new_op', 'labor_new_kind', 'labor_new_hours', 'labor_new_rate', 'oh_new_desc', 'oh_new_amount'):
        assert 'form-input' in card.select_one(f'[name={name}]')['class']
    total = card.select_one('output[name=labor_new_total]')
    assert total is not None and not total.has_attr('tabindex')
    assert 'form-input' not in total.get('class', [])
    assert card.select_one('[name=labor_new_amount]').has_attr('hidden')
    assert card.select_one('[name=labor_new_amount]').has_attr('disabled')
    assert card.select('.doc-section > .section-header')


def test_inventory_history_uses_shared_card_and_permission_redaction():
    from copy import deepcopy
    from celerp.services.activity_redaction import redact_entries_for_role
    events = [{'id': 1, 'event_type': 'item.updated', 'entity_id': 'item:card',
               'ts': '2026-11-07T03:00:00+00:00', 'actor_name': 'Owner',
               'data': {'fields_changed': {'cost_price': {'old': 98765, 'new': 87654},
                                            'name': {'old': 'Before', 'new': 'After'}}}}]
    for role, visible in [('operator', False), ('manager', True)]:
        html = to_xml(_ledger_table(redact_entries_for_role(deepcopy(events), {}, role), 'item:card', 'THB'))
        card = BeautifulSoup(html, 'html.parser')
        assert card.select_one('.doc-section > .section-header')
        assert all(label in card.get_text() for label in ('Field', 'Old value', 'New value', 'Before', 'After'))
        assert ('98,765.00' in html) == visible


def test_both_tabs_keep_the_shared_formatters_symbol_position(monkeypatch):
    # A formatter may put the currency after the amount. Neither tab moves it.
    from ui.routes import inventory
    def suffix_money(value, currency):
        return f'{float(value):,.2f} EUR'
    monkeypatch.setattr(inventory, 'fmt_money', suffix_money)
    item = {'quantity': 1, 'sell_by': 'piece', 'cost_price': 1200,
            'recipe': {'components': [{'item_id': 'item:raw', 'quantity': 1}], 'unit_cost': 1200}}
    pricing = BeautifulSoup(to_xml(_pricing_form('item:test', item, [{'name': 'Cost'}], 'EUR')), 'html.parser')
    recipe = BeautifulSoup(to_xml(_recipe_section('item:test', item, [], 'EUR')), 'html.parser')
    assert pricing.select_one('#derived_total_cost_price').get_text() == '1,200.00 EUR'
    assert '1,200.00 EUR' in recipe.select_one('#recipe-cost-card').get_text()

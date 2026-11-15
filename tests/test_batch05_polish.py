"""Shared record links and section icons retain their existing navigation behavior."""
import pytest
from bs4 import BeautifulSoup
from fasthtml.common import to_xml
from ui.components.icons import icon
from ui.components.table import searchable_select
from ui.components.shell import _sidebar

@pytest.mark.parametrize('kind,prefix', [('contact','/contacts/'), ('item','/inventory/'), ('doc','/docs/'), ('list','/lists/')])
@pytest.mark.parametrize('value', ['', 'record:1', '__new__'])
def test_record_selector_links_only_to_a_selected_record(kind, prefix, value):
    section=BeautifulSoup(to_xml(searchable_select('value',[(value,'Chosen')],value=value,record_type=kind)), 'html.parser')
    link=section.select_one('.combobox-record-link')
    assert link.select_one('svg')
    if value and not value.startswith('__'):
        assert link['href']==prefix+'record%3A1' and link['target']=='_blank'
    else:
        assert link.has_attr('hidden') and not link.has_attr('href')


def test_sidebar_group_icons_are_distinct_and_settings_shortcuts_survive():
    html=BeautifulSoup(to_xml(_sidebar('invoices',role='owner',settings={})), 'html.parser')
    expected={'sales_documents':('file-text','/settings/sales'), 'purchasing_documents':('shopping-cart','/settings/purchasing'), 'inventory':('package','/settings/inventory'), 'manufacturing':('factory','/settings/manufacturing'), 'subscriptions':('repeat',None)}
    shapes=[]
    for group,(name,href) in expected.items():
        header=html.select_one(f'[data-group="{group}"] .sidebar-group-header')
        assert header, group
        svg=header.select_one('svg')
        assert str(svg)==str(BeautifulSoup(str(icon(name)), 'html.parser').svg)
        shapes.append(str(svg))
        if href:
            assert header.select_one('a')['href']==href
    assert len(set(shapes))==5

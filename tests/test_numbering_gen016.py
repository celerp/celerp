# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""GEN-016: the four document types that had no Numbering settings (supplier debit notes,
production orders and both subscription documents) are listed once, on the settings page of the
module that owns them, and number through the shared counters."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company
from celerp_docs.routes import _NUMBERING_MODULES as NUMBERING_MODULES
from celerp_docs.sequences import DEFAULT_PATTERN, update_sequence
from company_today_support import freeze
from test_helpers import company_auth, mark_running

TYPES = ('debit_note', 'production_order', 'subscription_invoice', 'subscription_po')
NOW = datetime(2026, 11, 7, 3, tzinfo=timezone.utc)
PAGES = ('/settings/sales?tab=numbering', '/settings/purchasing?tab=numbering',
         '/settings/manufacturing', '/subscriptions?tab=numbering')


@pytest.fixture(autouse=True)
def numbering_clock(monkeypatch, mounted_modules_running):
    freeze(monkeypatch, NOW)
    mark_running(monkeypatch, 'celerp-subscriptions')


async def _settings(session, auth):
    c = await session.get(Company, auth['company_id'], populate_existing=True)
    return deepcopy(c.settings or {})


async def _configure(client, auth, dtype, **values):
    return await client.patch(f'/docs/sequences/{dtype}', headers=auth['headers'], json=values)


async def _view(client, auth, dtype):
    r = await client.get('/docs/sequences', headers=auth['headers'])
    assert r.status_code == 200, r.text
    return next(s for s in r.json() if s['doc_type'] == dtype)


async def _issue(client, auth, dtype):
    r = await client.post('/docs', headers=auth['headers'], json={'doc_type': dtype, 'currency': 'USD'})
    assert r.status_code == 200, r.text
    return r.json()['id'].removeprefix('doc:')


async def _module(session, auth, name, on):
    c = await locked_company(session, auth['company_id'])
    enabled = set((c.settings or {}).get('enabled_modules', (
        'celerp-docs', 'celerp-inventory', 'celerp-accounting', 'celerp-contacts',
        'celerp-manufacturing', 'celerp-subscriptions')))
    enabled.add(name) if on else enabled.discard(name)
    c.settings = {**c.settings, 'enabled_modules': sorted(enabled)}
    await session.commit()


@pytest_asyncio.fixture
async def ui(client, auth, monkeypatch):
    import ui.api_client as api
    from ui.app import app
    from ui.config import COOKIE_NAME

    async def company(token):
        r = await client.get('/companies/me', headers={'Authorization': f'Bearer {token}'})
        if r.status_code != 200:
            raise api.APIError(r.status_code, r.text)
        return r.json()

    async def sequences(token):
        r = await client.get('/docs/sequences', headers={'Authorization': f'Bearer {token}'})
        if r.status_code != 200:
            raise api.APIError(r.status_code, r.text)
        return r.json()

    monkeypatch.setattr(api, 'get_company', company)
    monkeypatch.setattr(api, 'get_doc_sequences', sequences)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://ui') as u:
        u.cookies.set(COOKIE_NAME, auth['headers']['Authorization'].removeprefix('Bearer '))
        yield u


def _rows(html):
    return re.findall(r'data-numbering-type="([^"]+)"', html)


async def test_each_missing_type_listed_exactly_once(ui):
    pages = [await ui.get(p) for p in PAGES]
    assert all(p.status_code == 200 for p in pages), [p.status_code for p in pages]
    counts = Counter(t for p in pages for t in _rows(p.text))
    assert {t: counts[t] for t in TYPES} == dict.fromkeys(TYPES, 1)
    assert all(n == 1 for n in counts.values()), counts


async def test_existing_tabs_unchanged_except_debit_note(ui):
    from ui.routes.settings_sales import _SALES_DOC_TYPES
    from ui.routes.settings_purchasing import _PURCHASING_DOC_TYPES
    assert _SALES_DOC_TYPES == {'invoice', 'proforma', 'memo', 'receipt', 'credit_note', 'list',
                               'quotation', 'transfer', 'audit', 'writeoff', 'shipping_doc'}
    assert _PURCHASING_DOC_TYPES == {'purchase_order', 'bill', 'consignment_in', 'debit_note'}
    sales, purchases = [await ui.get(p) for p in PAGES[:2]]
    # No receipt counter in the registry; the shared List counter has a row only while a company has it.
    assert set(_rows(sales.text)) == _SALES_DOC_TYPES - {'receipt', 'list'}
    assert set(_rows(purchases.text)) == _PURCHASING_DOC_TYPES


async def test_manufacturing_off_hides_production_numbering(ui, client, session, auth):
    await _module(session, auth, 'celerp-manufacturing', False)
    assert 'production_order' not in _rows((await ui.get(PAGES[2])).text)
    r = await _configure(client, auth, 'production_order', prefix='NEW')
    assert r.status_code == 403, r.text
    assert 'sequences' not in await _settings(session, auth)


@pytest.mark.parametrize('dtype', TYPES[2:])
async def test_subscriptions_off_hides_subscription_numbering(ui, client, session, auth, dtype):
    await _module(session, auth, 'celerp-subscriptions', False)
    r = await ui.get(PAGES[3])
    assert dtype not in _rows(r.text)
    assert (await _configure(client, auth, dtype, prefix='NEW')).status_code == 403


@pytest.mark.parametrize('dtype', TYPES)
async def test_role_without_settings_permission(ui, client, session, auth, dtype):
    await session.execute(update(UserCompany).where(UserCompany.company_id == auth['company_id'],
                         UserCompany.user_id == auth['user_id']).values(role='operator'))
    await session.commit()
    page = PAGES[1] if dtype == 'debit_note' else PAGES[2] if dtype == 'production_order' else PAGES[3]
    r = await ui.get(page)
    assert r.status_code in (302, 303, 403), r.text[:200]
    assert (await _configure(client, auth, dtype, prefix='NO')).status_code == 403


@pytest.mark.parametrize('dtype', TYPES)
async def test_roundtrip_prefix_pattern_preview(client, auth, dtype):
    r = await _configure(client, auth, dtype, prefix='NUM', pattern=DEFAULT_PATTERN, next=10)
    assert r.status_code == 200, r.text
    assert r.json()['preview'] == 'NUM-2611-0010'
    assert await _issue(client, auth, dtype) == 'NUM-2611-0010'
    row = await _view(client, auth, dtype)
    assert row['next'] == 11 and row['preview'] == 'NUM-2611-0011'




@pytest.mark.parametrize('dtype', TYPES)
@pytest.mark.parametrize('bad', [
    {'pattern': '{PREFIX}-{YY}'}, {'pattern': '{BOGUS}-{####}'},
    {'prefix': 'A{B'},
])
async def test_invalid_pattern_and_prefix_rejected(client, session, auth, dtype, bad):
    before = await _settings(session, auth)
    r = await _configure(client, auth, dtype, **bad)
    assert r.status_code == 422, r.text
    assert await _settings(session, auth) == before


@pytest.mark.parametrize('dtype', TYPES)
@pytest.mark.parametrize('value', [0, -1, True, 1.5])
async def test_next_number_requires_a_positive_integer(client, session, auth, dtype, value):
    before = await _settings(session, auth)
    r = await _configure(client, auth, dtype, next=value)
    assert r.status_code == 422, r.text
    assert await _settings(session, auth) == before


@pytest.mark.parametrize('dtype', TYPES)
async def test_monthly_reset_uses_company_timezone(monkeypatch, client, session, auth, dtype):
    c = await locked_company(session, auth['company_id'])
    c.settings = {**c.settings, 'timezone': 'Asia/Bangkok'}
    await session.commit()
    await _configure(client, auth, dtype, prefix='MO', pattern=DEFAULT_PATTERN, next=10)
    assert (await _issue(client, auth, dtype)).endswith('-0010')
    freeze(monkeypatch, datetime(2026, 11, 30, 17, 30, tzinfo=timezone.utc))
    assert await _issue(client, auth, dtype) == 'MO-2612-0001'


@pytest.mark.parametrize('dtype', TYPES)
async def test_yearly_reset_uses_company_timezone(monkeypatch, client, session, auth, dtype):
    c = await locked_company(session, auth['company_id'])
    c.settings = {**c.settings, 'timezone': 'Asia/Bangkok'}
    await session.commit()
    await _configure(client, auth, dtype, prefix='YR', pattern='{PREFIX}-{YYYY}-{####}', next=10)
    assert await _issue(client, auth, dtype) == 'YR-2026-0010'
    freeze(monkeypatch, datetime(2026, 12, 31, 17, 30, tzinfo=timezone.utc))
    assert await _issue(client, auth, dtype) == 'YR-2027-0001'




@pytest.mark.parametrize('dtype', TYPES)
async def test_monthly_reset_is_at_the_company_midnight(monkeypatch, client, session, auth, dtype):
    c = await locked_company(session, auth['company_id'])
    c.settings = {**c.settings, 'timezone': 'Asia/Bangkok'}
    await session.commit()
    await _configure(client, auth, dtype, prefix='MB', pattern=DEFAULT_PATTERN, next=10)
    freeze(monkeypatch, datetime(2026, 11, 30, 16, 59, 59, tzinfo=timezone.utc))  # 23:59:59 in Bangkok
    assert await _issue(client, auth, dtype) == 'MB-2611-0010'
    freeze(monkeypatch, datetime(2026, 11, 30, 17, 0, 0, tzinfo=timezone.utc))  # midnight in Bangkok
    assert await _issue(client, auth, dtype) == 'MB-2612-0001'


@pytest.mark.parametrize('dtype', TYPES)
async def test_a_typed_pattern_is_saved_exactly_when_it_is_well_formed(client, session, auth, dtype):
    from test_services.test_sequences import pattern_is_valid, random_patterns
    patterns = random_patterns(4280 + TYPES.index(dtype), 60) + ['{PREFIX}-{####}', '{##}-{####}', '{YY}}-{##}']
    for pattern in patterns:
        before = await _settings(session, auth)
        r = await _configure(client, auth, dtype, pattern=pattern)
        assert r.status_code in (200, 422), (pattern, r.text)
        assert (r.status_code == 200) == pattern_is_valid(pattern), (pattern, r.text)
        if r.status_code == 200:
            assert (await _view(client, auth, dtype))['pattern'] == pattern
        else:
            assert await _settings(session, auth) == before


@pytest.mark.parametrize('dtype', TYPES)
async def test_duplicate_number_cannot_be_issued(client, session, auth, dtype):
    first = await _issue(client, auth, dtype)
    c = await locked_company(session, auth['company_id'])
    update_sequence(c, dtype, next_num=1)  # direct low-level repair, bypassing settings validation
    await session.commit()
    second = await _issue(client, auth, dtype)
    assert second != first and second.endswith('-0002')


@pytest.mark.parametrize('dtype', TYPES)
async def test_repeated_save_is_idempotent(client, session, auth, dtype):
    values = dict(prefix='IDEM', pattern=DEFAULT_PATTERN, next=10)
    assert (await _configure(client, auth, dtype, **values)).status_code == 200
    before = await _settings(session, auth)
    assert (await _configure(client, auth, dtype, **values)).status_code == 200
    assert await _settings(session, auth) == before
    assert (await _configure(client, auth, dtype, prefix='BAD', pattern='invalid', next=999)).status_code == 422
    assert await _settings(session, auth) == before






async def test_get_has_no_side_effects(ui, client, session, auth):
    before = await _settings(session, auth)
    count = await session.scalar(select(func.count()).select_from(Projection).where(Projection.company_id == auth['company_id']))
    for page in PAGES:
        assert (await ui.get(page)).status_code == 200
    assert (await client.get('/docs/sequences', headers=auth['headers'])).status_code == 200
    assert await _settings(session, auth) == before
    assert await session.scalar(select(func.count()).select_from(Projection).where(Projection.company_id == auth['company_id'])) == count


async def test_subscriptions_list_does_not_require_numbering_settings(ui, monkeypatch):
    from unittest.mock import AsyncMock
    import ui.api_client as api
    monkeypatch.setattr(api, 'get_company', AsyncMock(side_effect=api.APIError(503, 'Unavailable')))
    monkeypatch.setattr(api, 'list_subscriptions', AsyncMock(return_value={'items': []}))
    response = await ui.get('/subscriptions')
    assert response.status_code == 200
    assert '/subscriptions?tab=numbering' not in response.text


@pytest.mark.parametrize('dtype', TYPES[1:])
async def test_module_off_then_on_restores_settings(client, session, auth, dtype):
    await _configure(client, auth, dtype, prefix='KEEP', pattern=DEFAULT_PATTERN, next=14)
    before = await _view(client, auth, dtype)
    await _module(session, auth, NUMBERING_MODULES[dtype], False)
    assert (await _configure(client, auth, dtype, prefix='LOST')).status_code == 403
    await _module(session, auth, NUMBERING_MODULES[dtype], True)
    assert await _view(client, auth, dtype) == before


@pytest.mark.parametrize('dtype', TYPES)
async def test_company_isolation(client, session, auth, dtype):
    other = await company_auth(session, uuid.uuid4(), uuid.uuid4())
    assert (await _configure(client, auth, dtype, prefix='ONLYA', next=10)).status_code == 200
    before_b = await _settings(session, other)
    assert (await _view(client, other, dtype))['next'] == 1
    forged = await _configure(client, auth, dtype, prefix='FORGED', company_id=str(other['company_id']))
    assert forged.status_code == 422
    assert await _settings(session, other) == before_b
    assert (await _view(client, auth, dtype))['prefix'] == 'ONLYA'


def test_locale_labels_present_in_all_locales():
    from ui.i18n import _DOC_TYPE_LABEL_KEYS
    root = Path(__file__).resolve().parents[1] / 'ui/locales'
    catalogs = [json.loads(p.read_text()) for p in root.glob('*.json')]
    assert len(catalogs) == 12
    for dtype in TYPES:
        key = _DOC_TYPE_LABEL_KEYS[dtype]
        assert all(key in d for d in catalogs)
        assert all(not re.findall(r'\{\w+\}', d[key]) for d in catalogs)


@pytest.mark.parametrize('value', [0, -3])
async def test_a_next_number_below_one_is_refused_in_words_the_user_reads(client, session, auth, value):
    """The refusal says the number must be 1 or more and never names the internal field."""
    before = await _settings(session, auth)
    r = await _configure(client, auth, TYPES[0], next=value)
    assert r.status_code == 422, r.text
    assert "must be 1 or more" in r.text and "(next)" not in r.text
    assert await _settings(session, auth) == before


@pytest.mark.parametrize('side', ['sales', 'purchasing'])
async def test_subscription_numbering_leads_back_to_the_list_it_was_opened_from(ui, monkeypatch, side):
    from unittest.mock import AsyncMock
    import ui.api_client as api
    monkeypatch.setattr(api, 'list_subscriptions', AsyncMock(return_value={'items': []}))
    listing = (await ui.get(f'/subscriptions?direction={side}')).text
    assert f'href="/subscriptions?tab=numbering&amp;direction={side}"' in listing
    page = await ui.get(f'/subscriptions?tab=numbering&direction={side}')
    assert page.status_code == 200
    assert re.search(rf'<a href="/subscriptions\?direction={side}" class="breadcrumb-link">', page.text)
    # One "Document Numbering" heading: the card's; the page is titled by its list.
    assert len(re.findall(r'<h[13][^>]*>Document Numbering</h[13]>', page.text)) == 1

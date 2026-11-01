"""A page the reader's role may not open says which permission is missing and gives a way back,
never a bare "no permission" line that ends the visit there."""
from __future__ import annotations

from html import escape

import pytest

from celerp.services.permissions import missing_permission_text
from test_helpers import perm_setup, ui_get

pytestmark = pytest.mark.asyncio


async def test_a_report_the_role_cannot_read_names_the_permission_and_a_way_back(client, session):
    ctx = await perm_setup(client, session)
    page = await ui_get(ctx["operator_h"], "/reports/trial-balance")
    assert page.status_code == 200
    assert escape(missing_permission_text("view_financial_reports"), quote=False) in page.text
    assert 'href="/dashboard"' in page.text.split('class="error-banner', 1)[1]


async def test_the_journal_entry_form_names_the_permission_and_a_way_back(client, session):
    ctx = await perm_setup(client, session)
    page = await ui_get(ctx["operator_h"], "/accounting/journal/new")
    assert page.status_code == 200
    assert escape(missing_permission_text("manage_accounting"), quote=False) in page.text
    assert 'href="/dashboard"' in page.text.split('class="error-banner', 1)[1]

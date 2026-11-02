"""A page the reader's role may not open says which permission is missing and gives a way back,
never a bare "no permission" line that ends the visit there."""
from __future__ import annotations

from html import escape

import pytest

from celerp.services.permissions import missing_permission_text
from ui.i18n import t
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


async def test_a_missing_document_opens_inside_the_app_with_a_way_back(client, session):
    ctx = await perm_setup(client, session)
    page = await ui_get(ctx["admin_h"], "/docs/doc:no-such-document")
    assert escape(t("documents.document_not_found"), quote=False) in page.text
    assert 'href="/docs"' in page.text
    assert 'class="sidebar' in page.text  # the app's own navigation, not a bare page


@pytest.mark.parametrize("path", ["/accounting/reconcile/start",
                                  "/accounting/reconcile/00000000-0000-0000-0000-000000000001",
                                  "/accounting/reconcile/history/00000000-0000-0000-0000-000000000001"])
async def test_reconciliation_pages_name_the_permission_instead_of_offering_the_form(client, session, path):
    """A role that may not reconcile is told so on every reconciliation page, not shown a form it
    cannot submit or told the company has no bank accounts."""
    ctx = await perm_setup(client, session)
    page = await ui_get(ctx["operator_h"], path)
    assert page.status_code == 200
    assert escape(missing_permission_text("manage_accounting"), quote=False) in page.text
    assert 'href="/dashboard"' in page.text.split('class="error-banner', 1)[1]
    assert escape(t("acct.no_bank_accounts_configured"), quote=False) not in page.text
    assert 'action="/accounting/reconcile/start"' not in page.text

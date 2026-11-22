# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The AR and AP aging, sales and purchases pages: a user without the financial reports
permission sees the no-access banner naming the missing permission instead of an empty
report, and a credit (a negative amount) links to the party's
statement, where the credit and what it settles are shown."""
from __future__ import annotations

from datetime import date
from html import escape

import pytest
from fasthtml.common import to_xml

from celerp.services.permissions import missing_permission_text
from test_helpers import perm_setup, ui_get
from ui.i18n import t


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/reports/ar-aging", "/reports/ap-aging", "/reports/sales", "/reports/purchases"])
async def test_financial_report_page_without_permission_says_no_access(client, session, path):
    ctx = await perm_setup(client, session)
    refusal = escape(missing_permission_text("view_financial_reports"), quote=False)
    staff = await ui_get(ctx["operator_h"], path)
    assert staff.status_code == 200
    assert refusal in staff.text
    assert 'href="/dashboard"' in staff.text and escape(t("error.back_to_dashboard"), quote=False) in staff.text
    assert '<table class="data-table"' not in staff.text
    manager = await ui_get(ctx["manager_h"], path)
    assert refusal not in manager.text


def _line(party_key: str) -> dict:
    return {f"{party_key}_id": "contact:abc", f"{party_key}_name": "Party", "current": -40.0,
            "d30": 100.0, "d60": 0.0, "d90": 0.0, "d90plus": 0.0, "total": 60.0}


@pytest.mark.parametrize("label, party_key, doc_type", [("AR", "customer", "invoice"), ("AP", "supplier", "purchase_order")])
def test_aging_credit_cell_links_statement(label, party_key, doc_type):
    from ui.routes.reports import _aging_view

    data = {"as_of": date.today().isoformat(), "lines": [_line(party_key)], "buckets": {}}
    html = to_xml(_aging_view(data, label))
    assert 'href="/reports/statement?account=c:contact:abc"' in html
    # The owed amount keeps its document drilldown.
    assert f"doc_type={doc_type}" in html
    assert html.count("/reports/statement?") == 1

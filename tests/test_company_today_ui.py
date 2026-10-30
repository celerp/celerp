# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every screen that decides "today" (date presets, as-of and date defaults, printed dates)
uses the company's business day, never the server's.

Same frozen clock as the API tests: years away from the machine's date, at an instant where
the company's day is ahead of the UTC day (Bangkok) or behind it (Honolulu)."""
from __future__ import annotations

import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fasthtml.common import Div, to_xml

import ui.api_client as api
from company_today_support import CASES, freeze

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(params=CASES)
async def co(request, owner_ui, monkeypatch):
    """The signed-in owner's company in the case's timezone, on the frozen clock."""
    tz, instant, day = request.param
    r = await owner_ui.api.patch("/companies/me", json={"settings": {"timezone": tz}})
    assert r.status_code == 200, r.text
    freeze(monkeypatch, instant)
    return SimpleNamespace(tz=tz, day=day, ui=owner_ui, monkeypatch=monkeypatch)


def _window(html: str) -> tuple[str, str]:
    """The (from, to) the page's date bar shows."""
    found = dict(re.findall(r'name="(from|to)" value="([0-9-]*)"', html))
    return found.get("from", ""), found.get("to", "")


def _spy(co, name: str, result):
    """Replace api.<name> with a recorder that returns ``result``."""
    calls: list[tuple] = []

    async def recorder(*args, **kwargs):
        calls.append(args + tuple(kwargs.values()))
        return result

    co.monkeypatch.setattr(api, name, recorder)
    return calls


PRESET_PAGES = ["/docs", "/lists", "/payments", "/reports/pnl", "/reports/trial-balance",
                "/reports/general-ledger", "/reports/cash-flow", "/reports/ar-aging", "/reports/ap-aging",
                "/reports/sales", "/reports/purchases", "/accounting",
                "/reports/ledger/1000?src=trial-balance&"]


@pytest.mark.parametrize("page", PRESET_PAGES)
async def test_this_month_ends_on_the_company_day(co, page):
    url = page + ("" if page.endswith("&") else "?") + "preset=this_month"
    r = await co.ui.get(url)
    assert r.status_code == 200, (url, r.status_code)
    assert _window(r.text) == (co.day.replace(day=1).isoformat(), co.day.isoformat()), url


@pytest.mark.parametrize("page", ["/docs", "/lists"])
async def test_document_lists_default_window_ends_on_the_company_day(co, page):
    r = await co.ui.get(page)
    assert _window(r.text) == ((co.day - timedelta(days=365)).isoformat(), co.day.isoformat())


async def test_reports_default_to_the_company_fiscal_year_to_date(co):
    assert (await co.ui.api.patch("/companies/me/books", json={"fiscal_year_start": "04-01"})).status_code == 200
    fy_start = date(co.day.year if co.day.month >= 4 else co.day.year - 1, 4, 1)
    r = await co.ui.get("/reports/pnl")
    assert _window(r.text) == (fy_start.isoformat(), co.day.isoformat())


async def test_order_search_and_queues_use_the_company_day(co):
    import ui.routes.manufacturing as manufacturing

    seen: list[str] = []
    real = manufacturing._order_table
    co.monkeypatch.setattr(manufacturing, "_order_table",
                           lambda *a, today, **kw: seen.append(today) or real(*a, today=today, **kw))
    listed = _spy(co, "list_mfg_orders", {"items": []})
    await co.ui.get("/manufacturing/search?preset=this_month")
    await co.ui.get("/manufacturing/production")
    assert listed[0][1]["date_to"] == co.day.isoformat()
    assert seen == [co.day.isoformat()] * 2


async def test_balance_sheet_is_as_of_the_company_day(co):
    asked = _spy(co, "get_balance_sheet", {})
    page = await co.ui.get("/reports/balance-sheet")
    assert f'name="as_of" value="{co.day.isoformat()}"' in page.text
    await co.ui.get("/reports/export/balance-sheet/csv")
    assert [call[1]["as_of"] for call in asked] == [co.day.isoformat()] * 2


async def test_printed_reports_carry_the_company_day(co):
    r = await co.ui.get("/reports/print/pnl?preset=this_month")
    assert re.search(rf'class="report-print-date">[^<]*{co.day.isoformat()}', r.text)


async def test_new_journal_entry_and_reconciliation_start_on_the_company_day(co):
    journal = await co.ui.get("/accounting/journal/new")
    assert f'value="{co.day.isoformat()}"' in journal.text
    reconcile = await co.ui.get("/accounting/reconcile/start")
    assert f'name="statement_date" value="{co.day.isoformat()}"' in reconcile.text


async def test_contact_summary_counts_the_company_fiscal_year(co):
    from ui.routes.contacts import _invoice_totals

    calls = _spy(co, "get_doc_summary", {})
    await _invoice_totals("token", "contact:1", {"timezone": co.tz, "fiscal_year_start": "04-01"})
    ytd = next(params for _, params in calls if "date_from" in params)
    # On 1 April the fiscal year starts that day; on 31 March it is the year that began a year before.
    fy_start = co.day if co.day == date(2031, 4, 1) else date(2030, 4, 1)
    assert (ytd["date_from"], ytd["date_to"]) == (fy_start.isoformat(), co.day.isoformat())


async def test_prints_are_dated_the_company_day(co):
    import ui.routes.inventory as inventory

    dated: list[str] = []
    co.monkeypatch.setattr(inventory, "_run_sheet_print_view", lambda order, items, today: dated.append(today) or Div())
    co.monkeypatch.setattr(inventory, "_worksheet_print_view",
                           lambda eid, item, items, today, category: dated.append(today) or Div())
    _spy(co, "get_mfg_order", {"inputs": []})
    _spy(co, "get_item", {"recipe": {}})
    await co.ui.get("/manufacturing/mfg:1/run-sheet/print")
    await co.ui.get("/inventory/item:1/worksheet/print")
    assert dated == [co.day.isoformat()] * 2


_DRAFT = {"entity_id": "doc:1", "doc_type": "invoice", "status": "final", "contact_id": "contact:1",
          "issue_date": "", "total": 10, "amount_outstanding": 10, "currency": "USD", "line_items": []}


async def test_document_date_defaults_are_the_company_day(co):
    from ui.routes.documents import _payment_section

    day = co.day.isoformat()
    _spy(co, "get_doc", _DRAFT)
    edit = await co.ui.get("/docs/doc:1/field/issue_date/edit")
    assert f'value="{day}"' in edit.text
    panel = await co.ui.get("/docs/bulk-payment-panel?doc_ids=doc:1")
    assert f'name="payment_date" value="{day}"' in panel.text
    assert f'name="payment_date" value="{day}"' in to_xml(_payment_section(_DRAFT, tz=co.tz))


async def test_refund_without_a_date_is_dated_the_company_day(co):
    refunded = _spy(co, "refund_payment", {})
    await co.ui.post("/docs/doc:1/refund", data={"amount": "5", "payment_index": "0", "payment_date": ""})
    assert refunded[0][2]["payment_date"] == co.day.isoformat()


async def test_new_share_link_expires_thirty_company_days_out(co):
    _spy(co, "get_share_status", {"active": False})
    r = await co.ui.get("/docs/doc:1/share")
    assert f'value="{(co.day + timedelta(days=30)).isoformat()}"' in r.text

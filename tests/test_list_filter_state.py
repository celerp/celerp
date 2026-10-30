"""Document and list pages keep every active filter across paging, sorting, status cards,
the date bar and the summary counts, and always highlight one status card."""
from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from celerp.models.projections import Projection
from test_helpers import make_test_token

_COMPANY = {"name": "Test Corp", "currency": "THB", "timezone": "Asia/Bangkok",
            "fiscal_year_start": "01-01", "current_role": "owner", "settings": {},
            "docs_default_preset": "all"}
_NOW = datetime.now(timezone.utc)
_DOC = {"entity_id": "d1", "doc_type": "invoice", "ref_id": "INV-1", "status": "final", "issue_date": "2026-02-01",
        "total": "10.00", "customer_name": "Test Customer", "updated_at": "2026-02-01T00:00:00Z"}
_SUMMARY = {"count_by_status": {"final": 3}, "all_issued_count": 3, "draft_count": 0}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


def _cookies() -> dict:
    return {"celerp_token": make_test_token(role="owner")}


def _hrefs(html: str, cls: str) -> list[str]:
    return re.findall(r'href="([^"]*)"[^>]*class="[^"]*\b' + cls + r'\b', html) + \
        re.findall(r'class="[^"]*\b' + cls + r'\b[^"]*"[^>]*href="([^"]*)"', html)


def _active_card_hrefs(html: str) -> list[str]:
    return [h for h in re.findall(r'<a href="([^"]*)" class="status-card[^"]*status-card--active', html)]


def _docs_patches(list_docs: AsyncMock, summary: AsyncMock):
    return (
        patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)),
        patch("ui.api_client.list_docs", new=list_docs),
        patch("ui.api_client.get_doc_summary", new=summary),
    )


class TestDocListState:
    @pytest.mark.asyncio
    async def test_doc_list_pagination_keeps_filters(self, ui_client):
        list_docs = AsyncMock(return_value={"items": [], "total": 120})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=list_docs), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)):
            r = await ui_client.get(
                "/docs?type=invoice&all_issued=1&overdue_only=1&sort=due_date&dir=asc")
        assert r.status_code == 200
        page2 = [h for h in _hrefs(r.text, "page-btn") if "page=2" in h]
        assert page2, r.text[:500]
        for key in ("all_issued=1", "overdue_only=1", "sort=due_date", "dir=asc", "type=invoice"):
            assert key in page2[0], f"{key} missing from pagination href {page2[0]}"

    @pytest.mark.asyncio
    async def test_doc_status_cards_keep_filters(self, ui_client):
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [_DOC], "total": 1})), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)):
            r = await ui_client.get(
                "/docs?type=invoice&preset=custom&from=2026-01-01&to=2026-03-31&sort=total&dir=asc&q=ruby")
        assert r.status_code == 200
        overdue = [h for h in re.findall(r'<a href="([^"]*)" class="status-card', r.text) if "overdue_only=1" in h]
        assert overdue, "overdue card missing"
        for key in ("from=2026-01-01", "to=2026-03-31", "sort=total", "dir=asc", "q=ruby"):
            assert key in overdue[0], f"{key} missing from card href {overdue[0]}"
        sort_links = re.findall(r'href="([^"]*)" class="sort-link"', r.text)
        assert sort_links and all("from=2026-01-01" in h for h in sort_links), sort_links[:2]

    @pytest.mark.asyncio
    async def test_doc_summary_uses_list_date_window(self, ui_client):
        summary = AsyncMock(return_value=_SUMMARY)
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_doc_summary", new=summary):
            r = await ui_client.get("/docs?type=invoice&preset=custom&from=2026-01-01&to=2026-03-31")
        assert r.status_code == 200
        params = summary.call_args.args[1]
        assert params == {"doc_type": "invoice", "date_from": "2026-01-01", "date_to": "2026-03-31"}, params

    @pytest.mark.asyncio
    async def test_doc_summary_follows_search_and_contact(self, ui_client):
        """The status cards summarise the rows the list shows: the search term and the
        contact filter travel to the summary, the status filter and page never do."""
        summary = AsyncMock(return_value=_SUMMARY)
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_doc_summary", new=summary):
            r = await ui_client.get("/docs?type=invoice&q=ruby&contact_id=c:9&status=paid&page=2")
        assert r.status_code == 200
        params = summary.call_args.args[1]
        assert params.get("q") == "ruby" and params.get("contact_id") == "c:9", params
        assert "status" not in params and "page" not in params and "limit" not in params, params

    @pytest.mark.asyncio
    async def test_doc_list_default_card_active(self, ui_client):
        list_docs = AsyncMock(return_value={"items": [], "total": 0})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=list_docs), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)):
            r = await ui_client.get("/docs?type=invoice")
        assert r.status_code == 200
        active = _active_card_hrefs(r.text)
        assert len(active) == 1 and "all_issued=1" in active[0], active
        assert list_docs.call_args.args[1].get("all_issued") == "1", list_docs.call_args

    @pytest.mark.asyncio
    async def test_doc_search_keeps_filters(self, ui_client):
        list_docs = AsyncMock(return_value={"items": [], "total": 0})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=list_docs), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)):
            r = await ui_client.get("/docs/search?type=invoice&q=ruby&overdue_only=1&from=2026-01-01&to=2026-03-31&preset=custom")
        assert r.status_code == 200
        params = list_docs.call_args.args[1]
        assert params.get("overdue_only") == "1" and params.get("date_from") == "2026-01-01", params


class TestListPageState:
    @pytest.mark.asyncio
    async def test_list_page_default_card_active_and_cards_keep_type(self, ui_client):
        list_lists = AsyncMock(return_value={"items": [], "total": 120})
        summary = AsyncMock(return_value={"count_by_status": {"open": 2}, "all_issued_count": 2})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=list_lists), \
             patch("ui.api_client.get_list_summary", new=summary):
            r = await ui_client.get("/lists?type=audit&preset=custom&from=2026-01-01&to=2026-03-31")
        assert r.status_code == 200
        active = _active_card_hrefs(r.text)
        assert len(active) == 1 and "all_issued=1" in active[0], active
        assert "type=audit" in active[0] and "from=2026-01-01" in active[0], active
        assert list_lists.call_args.args[1].get("all_issued") == "1"
        assert summary.call_args.args[1] == {"list_type": "audit", "date_from": "2026-01-01", "date_to": "2026-03-31"}
        per_page = re.findall(r'<option value="([^"]*per_page=[^"]*)"', r.text)
        assert per_page and all("type=audit" in u and "from=2026-01-01" in u for u in per_page), per_page

    @pytest.mark.asyncio
    async def test_list_draft_card_counts_every_draft_unless_dates_are_chosen(self, ui_client):
        """The Draft card opens the drafts view, which shows every draft, so it counts every draft.
        A date range the user picked applies to it like every other card."""
        summary = AsyncMock(return_value={"count_by_status": {"draft": 1, "open": 2},
                                          "draft_count": 7, "all_issued_count": 2})

        def _draft_count(html: str) -> str:
            m = re.search(r'<a href="[^"]*status=draft"[^>]*>.*?status-card-count">(\d+)<', html, re.S)
            assert m, html
            return m.group(1)

        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_list_summary", new=summary):
            default = await ui_client.get("/lists")
            chosen = await ui_client.get("/lists?preset=custom&from=2026-01-01&to=2026-03-31")
        assert default.status_code == 200 and chosen.status_code == 200
        assert _draft_count(default.text) == "7"
        assert _draft_count(chosen.text) == "1"


def _doc_row(company_id, doc_type: str, status: str, issue_date: str, total: float = 100.0) -> Projection:
    eid = str(uuid.uuid4())
    return Projection(entity_id=eid, company_id=company_id, entity_type="doc", version=1, state={
        "doc_type": doc_type, "status": status, "issue_date": issue_date, "doc_number": f"X-{eid[:6]}",
        "total": total, "amount_outstanding": total, "amount_paid": 0.0, "contact_name": "Test Co",
    }, updated_at=_NOW)


def _list_row(company_id, list_type: str, status: str, issue_date: str) -> Projection:
    eid = str(uuid.uuid4())
    return Projection(entity_id=eid, company_id=company_id, entity_type="list", version=1, state={
        "list_type": list_type, "status": status, "issue_date": issue_date, "created_at": issue_date,
        "ref": f"L-{eid[:6]}", "total": 10.0,
    }, updated_at=_NOW)


async def _register(client) -> tuple[str, uuid.UUID]:
    email = f"state-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/auth/register", json={"company_name": f"StateCo-{email[:6]}", "email": email,
                                                   "name": "Admin", "password": "pw123456"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    company_id = uuid.UUID(json.loads(base64.b64decode(token.split(".")[1] + "=="))["company_id"])
    return token, company_id


class TestSummaryDateWindow:
    @pytest.mark.asyncio
    async def test_doc_summary_respects_date_window(self, client, session):
        token, cid = await _register(client)
        session.add_all([
            _doc_row(cid, "invoice", "final", "2026-01-10"),
            _doc_row(cid, "invoice", "final", "2026-02-10"),
            _doc_row(cid, "invoice", "final", "2025-06-10"),
            _doc_row(cid, "invoice", "draft", "2025-06-11"),
        ])
        await session.commit()
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.get("/docs/summary?doc_type=invoice&date_from=2026-01-01&date_to=2026-03-31", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["all_issued_count"] == 2, body
        assert body["count_by_status"].get("draft", 0) == 0, body
        r = await client.get("/docs/summary?doc_type=invoice", headers=headers)
        assert r.json()["all_issued_count"] == 3

    @pytest.mark.asyncio
    async def test_list_summary_respects_type_and_date_window(self, client, session):
        token, cid = await _register(client)
        session.add_all([
            _list_row(cid, "audit", "open", "2026-01-10"),
            _list_row(cid, "audit", "open", "2025-06-10"),
            _list_row(cid, "shipping_doc", "open", "2026-01-12"),
        ])
        await session.commit()
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.get("/lists/summary?list_type=audit&date_from=2026-01-01&date_to=2026-03-31", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["all_issued_count"] == 1, r.json()
        r = await client.get("/lists/summary", headers=headers)
        assert r.json()["all_issued_count"] == 3


class TestPickedPageSize:
    """The pager's page-size picker changes how many rows the page asks for."""

    @pytest.mark.asyncio
    async def test_lists_honor_per_page(self, ui_client):
        list_lists = AsyncMock(return_value={"items": [], "total": 120})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=list_lists), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get("/lists?type=audit&page=2&per_page=25")
        assert r.status_code == 200
        params = list_lists.call_args.args[1]
        assert (params.get("limit"), params.get("offset")) == (25, 25), params
        assert "26-50 of 120" in r.text
        page_links = re.findall(r'<a href="(/lists\?page=[^"]*)"', r.text)
        assert page_links and all(u.count("per_page=") == 1 and "per_page=25" in u.replace("&amp;", "&")
                                  for u in page_links), page_links

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path, api_fn, prefix", [
        ("/docs/search?type=invoice", "list_docs", "INV"),
        ("/lists/search?type=audit", "list_lists", "AUD"),
    ])
    async def test_search_honors_per_page(self, ui_client, path, api_fn, prefix):
        rows = [{**_DOC, "entity_id": f"d{i}", "ref_id": f"{prefix}-{i:03d}", "doc_number": f"{prefix}-{i:03d}",
                 "list_type": "audit"} for i in range(150)]

        async def _page(_token, params):
            return {"items": rows[params["offset"]:params["offset"] + params["limit"]], "total": len(rows)}

        fetch = AsyncMock(side_effect=_page)
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch(f"ui.api_client.{api_fn}", new=fetch), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(f"{path}&q=a&page=2&per_page=100")
        assert r.status_code == 200
        params = fetch.call_args.args[1]
        assert (params.get("limit"), params.get("offset")) == (100, 100), params
        shown = set(re.findall(rf"{prefix}-\d{{3}}", r.text))
        assert shown == {f"{prefix}-{i:03d}" for i in range(100, 150)}, sorted(shown)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("page, search, filter_", [
        ("/docs?type=invoice", "/docs/search", "type=invoice"),
        ("/lists?type=audit", "/lists/search", "type=audit"),
    ])
    async def test_search_box_keeps_filters_and_page_size(self, ui_client, page, search, filter_):
        empty = AsyncMock(return_value={"items": [], "total": 0})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=empty), patch("ui.api_client.list_lists", new=empty), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(f"{page}&per_page=100")
        assert r.status_code == 200
        boxes = [i for i in re.findall(r"<input[^>]*>", r.text) if 'name="q"' in i]
        urls = [u.replace("&amp;", "&") for b in boxes for u in re.findall(rf'hx-get="({search}[^"]*)"', b)]
        assert urls and all(filter_ in u and "per_page=100" in u for u in urls), urls

    @pytest.mark.asyncio
    async def test_list_type_select_submits_the_panel(self, ui_client):
        """The Type select (it replaced the type tabs) reloads the whole page through the
        filters panel, so its search box, cards and pager all follow the new type."""
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get("/lists?type=audit")
        assert r.status_code == 200
        assert 'id="type-tabs"' not in r.text
        panel = re.search(r'<form[^>]*id="list-filters".*?</form>', r.text, re.S).group(0)
        assert re.search(r'<form[^>]*action="/lists"[^>]*method="get"', panel)
        select = re.search(r'<select name="type"[^>]*>.*?</select>', panel, re.S).group(0)
        assert "submit()" in select and "hx-get" not in select
        assert re.findall(r'<option value="([^"]*)"', select) == ["", "quotation", "transfer", "audit", "writeoff", "shipping_doc"]
        assert re.search(r'<option value="audit" selected>', select)

    @pytest.mark.asyncio
    async def test_list_type_change_keeps_dates_and_page_size_and_drops_status(self, ui_client):
        """Switching type keeps the search, the chosen dates and the page size; the status
        filter fields are marked so the Type select drops them before it submits."""
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get("/lists?type=audit&q=abc&status=void&preset=custom&from=2026-01-01&to=2026-01-31&per_page=100")
        assert r.status_code == 200
        panel = re.search(r'<form[^>]*id="list-filters".*?</form>', r.text, re.S).group(0)
        hidden = dict(re.findall(r'<input type="hidden" name="([^"]*)" value="([^"]*)"', panel))
        assert hidden == {"per_page": "100", "status": "void"}, hidden
        status = re.search(r'<input type="hidden" name="status"[^>]*>', panel).group(0)
        assert "list-status-filter" in status
        select = re.search(r'<select name="type"[^>]*>', panel).group(0)
        assert ".list-status-filter" in select and "submit()" in select
        assert re.search(r'<input type="date" name="from" value="2026-01-01"', panel)
        assert re.search(r'<input type="date" name="to" value="2026-01-31"', panel)
        assert re.search(r'<input[^>]*name="q"[^>]*value="abc"', panel)
        assert 'data-unchosen="1"' not in panel, "a chosen range is kept on a type change"

    @pytest.mark.asyncio
    async def test_list_type_change_does_not_turn_the_default_dates_into_a_choice(self, ui_client):
        """With no dates in the address the panel shows the company default; those fields are
        marked so a type change does not send them as a chosen range."""
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get("/lists?type=audit")
        panel = re.search(r'<form[^>]*id="list-filters".*?</form>', r.text, re.S).group(0)
        for name in ("preset", "from", "to"):
            control = re.search(rf'<(?:select|input)[^>]*name="{name}"[^>]*>', panel).group(0)
            assert 'data-unchosen="1"' in control, control

    @pytest.mark.asyncio
    async def test_subscriptions_honor_per_page(self, ui_client):
        subs = [{"entity_id": f"sub:{i}", "ref_id": f"SUB-{i:03d}", "status": "active"} for i in range(60)]
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": subs})):
            r = await ui_client.get("/subscriptions?page=2&per_page=25")
        assert r.status_code == 200
        assert "26-50 of 60" in r.text
        assert "SUB-025" in r.text and "SUB-050" not in r.text and "SUB-024" not in r.text


class TestLiveSearchResults:
    """Typing in the search box replaces the whole result area, so the pager and the
    status cards describe the matches rather than the list before the search."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("page, content_id", [
        ("/docs?type=invoice", "doc-content"),
        ("/lists?type=audit", "list-content"),
    ])
    async def test_search_box_targets_the_result_area(self, ui_client, page, content_id):
        listed = AsyncMock(return_value={"items": [_DOC], "total": 120})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=listed), patch("ui.api_client.list_lists", new=listed), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(page)
        assert r.status_code == 200
        box = next(i for i in re.findall(r"<input[^>]*>", r.text) if 'id="search-input"' in i)
        assert f'hx-target="#{content_id}"' in box, box
        area = re.search(rf'<div[^>]*id="{content_id}"', r.text)
        assert area, content_id
        after = r.text[area.start():]
        assert "pagination" in after
        # The cards sit above the filters panel, outside the area the search replaces.
        base = content_id.split("-")[0]
        cards = r.text.index(f'id="{base}-cards"')
        assert cards < r.text.index(f'id="{base}-filters"') < area.start()
        assert 'class="status-card' in r.text[cards:area.start()]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path, api_fn, summary_fn, prefix, content_id", [
        ("/docs/search?type=invoice&q=ruby", "list_docs", "get_doc_summary", "INV", "doc-content"),
        ("/lists/search?type=audit&q=ruby", "list_lists", "get_list_summary", "AUD", "list-content"),
    ])
    async def test_search_pager_counts_the_matches(self, ui_client, path, api_fn, summary_fn, prefix, content_id):
        rows = [{**_DOC, "entity_id": f"d{i}", "ref_id": f"{prefix}-{i:03d}", "list_type": "audit"} for i in range(120)]

        async def _page(_token, params):
            return {"items": rows[params["offset"]:params["offset"] + params["limit"]], "total": len(rows)}

        fetch = AsyncMock(side_effect=_page)
        summary = AsyncMock(return_value={"count_by_status": {"final": 120}, "all_issued_count": 120, "draft_count": 0})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch(f"ui.api_client.{api_fn}", new=fetch), \
             patch(f"ui.api_client.{summary_fn}", new=summary):
            r = await ui_client.get(path, headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert re.match(rf'\s*<div[^>]*id="{content_id}"', r.text), r.text[:200]
        limit = fetch.call_args.args[1]["limit"]
        assert f"1-{limit} of 120" in r.text
        page_links = [u.replace("&amp;", "&") for u in re.findall(r'<a href="(/(?:docs|lists)\?[^"]*page=2[^"]*)"', r.text)]
        assert page_links and all("q=ruby" in u for u in page_links), page_links
        assert summary.call_args.args[1].get("q") == "ruby", summary.call_args
        assert 'class="status-card' in r.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path, page_url", [
        ("/docs/search?type=invoice&view=drafts&per_page=100&q=ruby", "/docs?type=invoice&view=drafts&per_page=100&q=ruby"),
        ("/lists/search?type=audit&per_page=100&q=ruby", "/lists?q=ruby&type=audit&per_page=100"),
        ("/contacts/content?type=vendor&q=ruby&sort=name&dir=asc", "/contacts/vendors?q=ruby&sort=name&dir=asc"),
    ])
    async def test_search_address_is_the_page(self, ui_client, path, page_url):
        """The address bar after a search opens the whole page with the same results on reload."""
        from urllib.parse import parse_qs, urlsplit
        empty = AsyncMock(return_value={"items": [], "total": 0})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=empty), patch("ui.api_client.list_lists", new=empty), \
             patch("ui.api_client.list_contacts", new=empty), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(path, headers={"HX-Request": "true"})
        assert r.status_code == 200
        pushed = urlsplit(r.headers.get("HX-Push-Url", ""))
        expected = urlsplit(page_url)
        assert (pushed.path, parse_qs(pushed.query)) == (expected.path, parse_qs(expected.query)), r.headers

    @pytest.mark.asyncio
    @pytest.mark.parametrize("page", ["/docs?type=invoice&q=ruby", "/lists?type=audit&q=ruby", "/contacts/vendors?q=ruby"])
    async def test_search_box_shows_the_active_search(self, ui_client, page):
        listed = AsyncMock(return_value={"items": [_DOC], "total": 1})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=listed), patch("ui.api_client.list_lists", new=listed), \
             patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(page)
        assert r.status_code == 200
        box = next(i for i in re.findall(r"<input[^>]*>", r.text) if 'id="search-input"' in i)
        assert 'value="ruby"' in box, box

    @pytest.mark.asyncio
    async def test_vendor_search_stays_on_vendors(self, ui_client):
        listed = AsyncMock(return_value={"items": [], "total": 0})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_contacts", new=listed):
            r = await ui_client.get("/contacts/vendors")
            box = next(i for i in re.findall(r"<input[^>]*>", r.text) if 'id="search-input"' in i)
            search_url = re.search(r'hx-get="([^"]*)"', box).group(1).replace("&amp;", "&")
            await ui_client.get(f"{search_url}{'&' if '?' in search_url else '?'}q=ruby",
                                headers={"HX-Request": "true"})
        assert listed.call_args.args[1]["contact_type"] == "vendor", listed.call_args

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [
        "/docs/search?type=invoice&q=ruby", "/lists/search?type=audit&q=ruby", "/contacts/content?type=vendor&q=ruby",
    ])
    async def test_expired_session_during_search_opens_login(self, ui_client, path):
        """An expired session while searching takes the whole window to the login page."""
        from ui.api_client import APIError
        expired = AsyncMock(side_effect=APIError(401, "expired"))
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=expired), patch("ui.api_client.list_lists", new=expired), \
             patch("ui.api_client.list_contacts", new=expired), \
             patch("ui.api_client.get_doc_summary", new=expired), \
             patch("ui.api_client.get_list_summary", new=expired):
            r = await ui_client.get(path, headers={"HX-Request": "true"})
        assert r.headers.get("HX-Redirect", "").startswith("/login"), (r.status_code, dict(r.headers))

    @pytest.mark.asyncio
    async def test_docs_search_updates_export_cards_and_summary(self, ui_client):
        """After a search on /docs, Export CSV, the status cards and the header summary follow
        the search, like a reload would. The panel needs nothing: the search box is in it."""
        listed = AsyncMock(return_value={"items": [_DOC], "total": 1})
        summary = AsyncMock(return_value={**_SUMMARY, "ar_outstanding": 40, "ar_total": 90, "invoice_count": 2})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=listed), patch("ui.api_client.get_doc_summary", new=summary):
            full = await ui_client.get("/docs?type=invoice")
            r = await ui_client.get("/docs/search?type=invoice&q=ruby", headers={"HX-Request": "true"})
        assert r.status_code == 200
        for text, oob in ((full.text, False), (r.text, True)):
            for pattern in (r'<a[^>]*href="/docs/export/csv\?[^"]*"[^>]*>', r'<div[^>]*id="doc-cards"[^>]*>',
                            r'<span[^>]*id="docs-summary"[^>]*>'):
                tag = re.search(pattern, text)
                assert tag, (pattern, text[:300])
                assert ('hx-swap-oob="true"' in tag.group(0)) is oob, tag.group(0)
        assert summary.call_args.args[1].get("q") == "ruby"
        assert "2 invoices in this period" in r.text
        assert 'id="doc-filters"' not in r.text and 'id="docs-dates"' not in r.text
        assert "q=ruby" in re.search(r'href="(/docs/export/csv\?[^"]*)"', r.text).group(1)

    @pytest.mark.asyncio
    async def test_lists_search_updates_export_and_cards(self, ui_client):
        """After a search on /lists, Export CSV and the status cards keep the search, like a
        reload would. The panel needs nothing: the search box is in it."""
        listed = AsyncMock(return_value={"items": [_DOC], "total": 1})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=listed), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}, "draft_count": 1})):
            full = await ui_client.get("/lists?type=audit")
            r = await ui_client.get("/lists/search?type=audit&q=ruby", headers={"HX-Request": "true"})
        assert r.status_code == 200
        for text, oob in ((full.text, False), (r.text, True)):
            export = re.search(r'<a[^>]*href="/lists/export/csv\?[^"]*"[^>]*>', text)
            cards = re.search(r'<div[^>]*id="list-cards"[^>]*>', text)
            assert export and cards, text[:300]
            assert ('hx-swap-oob="true"' in export.group(0)) is oob
            assert ('hx-swap-oob="true"' in cards.group(0)) is oob
        assert 'id="list-filters"' not in r.text and 'id="lists-dates"' not in r.text
        assert "q=ruby" in re.search(r'href="(/lists/export/csv\?[^"]*)"', r.text).group(1)
        card_links = [h.replace("&amp;", "&") for h in re.findall(r'<a href="([^"]*)" class="status-card', r.text)]
        assert card_links and all("q=ruby" in h and "type=audit" in h for h in card_links), card_links


class TestDateRangeDropdown:
    """One date control per list page: a preset dropdown, with the from/to range only for Custom."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("page", ["/docs", "/lists"])
    @pytest.mark.parametrize("query, shown", [
        ("?preset=this_month", False),
        ("?preset=custom&from=2026-01-01&to=2026-02-01", True),
    ])
    async def test_custom_range_only_for_custom(self, ui_client, page, query, shown):
        listed = AsyncMock(return_value={"items": [_DOC], "total": 1})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=listed), patch("ui.api_client.list_lists", new=listed), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(page + query)
        assert r.status_code == 200
        assert "preset-btn" not in r.text and "date-range-form" not in r.text
        assert r.text.count('name="preset"') == 1
        custom = re.findall(r'<(?:div|button)[^>]*class="[^"]*period-custom[^"]*"[^>]*>', r.text)
        assert len(custom) == 3, custom  # From, To and Apply
        assert all(("hidden" in tag) is not shown for tag in custom), custom
        preset = re.search(r'<select name="preset".*?</select>', r.text, re.S).group(0)
        assert re.search(r'<option value="([^"]*)" selected>', preset).group(1) == query.split("preset=")[1].split("&")[0]

    @pytest.mark.asyncio
    async def test_lists_filters_are_one_panel_with_labels(self, ui_client):
        """Search, Type and Period sit in one GET panel with a label above each, and Reset
        is a link back to the page."""
        listed = AsyncMock(return_value={"items": [_DOC], "total": 1})
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=listed), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get("/lists")
        assert "list-filter-bar" not in r.text
        panel = re.search(r'<form[^>]*id="list-filters".*?</form>', r.text, re.S).group(0)
        labels = dict((v, k) for k, v in re.findall(r'<label for="([^"]*)" class="filter-field-label">([^<]*)</label>', panel))
        assert labels == {"Search lists": "search-input", "Type": "filter-type", "Period": "filter-preset",
                          "From": "filter-from", "To": "filter-to"}, labels
        for control_id in labels.values():
            assert f'id="{control_id}"' in panel, control_id
        assert re.search(r'<a href="/lists" class="[^"]*filter-reset', panel)
        assert re.search(r'<div class="list-page">', r.text)


class TestDocsFilterPanel:
    """/docs uses the list page kit: header summary, cards, one filters panel, then the table."""

    async def _get(self, ui_client, url, summary=None):
        ui_client.cookies.update(_cookies())
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [_DOC], "total": 1})), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=summary or _SUMMARY)):
            r = await ui_client.get(url)
        assert r.status_code == 200
        return r

    @pytest.mark.asyncio
    async def test_records_select_keeps_accounting_sync_and_the_other_filters(self, ui_client):
        r = await self._get(ui_client, "/docs?type=bill&q=vendor&status=final&sort=total&accounting_sync=excluded")
        panel = re.search(r'<form[^>]*id="doc-filters".*?</form>', r.text, re.S).group(0)
        select = re.search(r'<select name="accounting_sync".*?</select>', panel, re.S).group(0)
        assert re.findall(r'<option value="([^"]*)"', select) == ["", "included", "excluded"]
        assert re.search(r'<option value="excluded" selected>Excluded</option>', select)
        assert re.search(r'<label for="filter-accounting_sync" class="filter-field-label">Records</label>', panel)
        hidden = dict(re.findall(r'<input type="hidden" name="([^"]*)" value="([^"]*)"', panel))
        assert hidden == {"type": "bill", "status": "final", "sort": "total"}, hidden
        assert re.search(r'<input[^>]*name="q"[^>]*value="vendor"', panel)
        assert re.search(r'<a href="/docs\?type=bill" class="[^"]*filter-reset', panel)
        assert "list-filter-bar" not in r.text and "accounting_sync=included" not in r.text

    @pytest.mark.asyncio
    async def test_an_unknown_records_value_still_warns(self, ui_client):
        r = await self._get(ui_client, "/docs?type=invoice&accounting_sync=bogus")
        assert "This sync filter is not one Celerp knows, so every record is shown." in r.text
        select = re.search(r'<select name="accounting_sync".*?</select>', r.text, re.S).group(0)
        assert "selected" not in select.split("</option>", 1)[1]
        ok = await self._get(ui_client, "/docs?type=invoice&accounting_sync=included")
        assert "flash--warning" not in ok.text

    @pytest.mark.asyncio
    async def test_header_summary_shows_ar_billed_and_count(self, ui_client):
        summary = {**_SUMMARY, "ar_outstanding": 1234.5, "ar_total": 5000, "invoice_count": 12, "unvalued_count": 2}
        r = await self._get(ui_client, "/docs?type=invoice", summary)
        header = re.search(r'<div class="page-header">.*?<div class="page-actions">', r.text, re.S).group(0)
        subtitle = re.search(r'<div class="page-subtitle">\s*(<span id="docs-summary".*?)\s*</div>', header, re.S)
        assert subtitle, header
        items = re.findall(r'<span class="doc-summary-item[^"]*">([^<]*)</span>', subtitle.group(1))
        assert items[0].startswith("AR: ") and "1,234.50" in items[0], items
        assert items[1].startswith("Billed: ") and "5,000.00" in items[1], items
        assert items[2] == "12 invoices in this period", items
        assert items[3].startswith("2 not in the totals"), items
        assert "valuation-bar" not in r.text

    @pytest.mark.asyncio
    async def test_header_summary_counts_other_types(self, ui_client):
        r = await self._get(ui_client, "/docs?type=memo", {**_SUMMARY, "memo_count": 4})
        line = re.search(r'<span id="docs-summary" class="doc-summary">(.*?)\s*</div>', r.text, re.S).group(1)
        items = re.findall(r'<span class="doc-summary-item[^"]*">([^<]*)</span>', line)
        assert len(items) == 1 and "AR:" not in items[0] and "4" in items[0], items

    @pytest.mark.asyncio
    async def test_type_is_carried_by_the_menu_not_a_select(self, ui_client):
        r = await self._get(ui_client, "/docs?type=invoice")
        panel = re.search(r'<form[^>]*id="doc-filters".*?</form>', r.text, re.S).group(0)
        assert '<select name="type"' not in panel
        assert '<input type="hidden" name="type" value="invoice">' in panel
        assert re.search(r'<div class="list-page">', r.text)
        order = [r.text.index(m) for m in ('id="docs-summary"', 'id="doc-cards"', 'id="doc-filters"', 'id="doc-content"')]
        assert order == sorted(order), order

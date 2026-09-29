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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=list_docs), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)):
            r = await ui_client.get(
                "/docs?type=invoice&all_issued=1&overdue_only=1&sort=due_date&dir=asc",
                cookies=_cookies())
        assert r.status_code == 200
        page2 = [h for h in _hrefs(r.text, "page-btn") if "page=2" in h]
        assert page2, r.text[:500]
        for key in ("all_issued=1", "overdue_only=1", "sort=due_date", "dir=asc", "type=invoice"):
            assert key in page2[0], f"{key} missing from pagination href {page2[0]}"

    @pytest.mark.asyncio
    async def test_doc_status_cards_keep_filters(self, ui_client):
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [_DOC], "total": 1})), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)):
            r = await ui_client.get(
                "/docs?type=invoice&preset=custom&from=2026-01-01&to=2026-03-31&sort=total&dir=asc&q=ruby",
                cookies=_cookies())
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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_doc_summary", new=summary):
            r = await ui_client.get("/docs?type=invoice&preset=custom&from=2026-01-01&to=2026-03-31",
                                    cookies=_cookies())
        assert r.status_code == 200
        params = summary.call_args.args[1]
        assert params == {"doc_type": "invoice", "date_from": "2026-01-01", "date_to": "2026-03-31"}, params

    @pytest.mark.asyncio
    async def test_doc_summary_follows_search_and_contact(self, ui_client):
        """The status cards summarise the rows the list shows: the search term and the
        contact filter travel to the summary, the status filter and page never do."""
        summary = AsyncMock(return_value=_SUMMARY)
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_doc_summary", new=summary):
            r = await ui_client.get("/docs?type=invoice&q=ruby&contact_id=c:9&status=paid&page=2",
                                    cookies=_cookies())
        assert r.status_code == 200
        params = summary.call_args.args[1]
        assert params.get("q") == "ruby" and params.get("contact_id") == "c:9", params
        assert "status" not in params and "page" not in params and "limit" not in params, params

    @pytest.mark.asyncio
    async def test_doc_list_default_card_active(self, ui_client):
        list_docs = AsyncMock(return_value={"items": [], "total": 0})
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=list_docs), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)):
            r = await ui_client.get("/docs?type=invoice", cookies=_cookies())
        assert r.status_code == 200
        active = _active_card_hrefs(r.text)
        assert len(active) == 1 and "all_issued=1" in active[0], active
        assert list_docs.call_args.args[1].get("all_issued") == "1", list_docs.call_args

    @pytest.mark.asyncio
    async def test_doc_search_keeps_filters(self, ui_client):
        list_docs = AsyncMock(return_value={"items": [], "total": 0})
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=list_docs), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)):
            r = await ui_client.get("/docs/search?type=invoice&q=ruby&overdue_only=1&from=2026-01-01&to=2026-03-31&preset=custom",
                                    cookies=_cookies())
        assert r.status_code == 200
        params = list_docs.call_args.args[1]
        assert params.get("overdue_only") == "1" and params.get("date_from") == "2026-01-01", params


class TestListPageState:
    @pytest.mark.asyncio
    async def test_list_page_default_card_active_and_cards_keep_type(self, ui_client):
        list_lists = AsyncMock(return_value={"items": [], "total": 120})
        summary = AsyncMock(return_value={"count_by_status": {"open": 2}, "all_issued_count": 2})
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=list_lists), \
             patch("ui.api_client.get_list_summary", new=summary):
            r = await ui_client.get("/lists?type=audit&preset=custom&from=2026-01-01&to=2026-03-31",
                                    cookies=_cookies())
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

        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_list_summary", new=summary):
            default = await ui_client.get("/lists", cookies=_cookies())
            chosen = await ui_client.get("/lists?preset=custom&from=2026-01-01&to=2026-03-31", cookies=_cookies())
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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=list_lists), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get("/lists?type=audit&page=2&per_page=25", cookies=_cookies())
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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch(f"ui.api_client.{api_fn}", new=fetch), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(f"{path}&q=a&page=2&per_page=100", cookies=_cookies())
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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=empty), patch("ui.api_client.list_lists", new=empty), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(f"{page}&per_page=100", cookies=_cookies())
        assert r.status_code == 200
        boxes = [i for i in re.findall(r"<input[^>]*>", r.text) if 'name="q"' in i]
        urls = [u.replace("&amp;", "&") for b in boxes for u in re.findall(rf'hx-get="({search}[^"]*)"', b)]
        assert urls and all(filter_ in u and "per_page=100" in u for u in urls), urls

    @pytest.mark.asyncio
    async def test_list_type_tabs_load_the_whole_page(self, ui_client):
        """A type tab opens that type's page, so its search box, cards and pager all
        follow the new type instead of keeping the previous one."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get("/lists?type=audit", cookies=_cookies())
        assert r.status_code == 200
        tabs = re.search(r'<div[^>]*id="type-tabs".*?</div>', r.text, re.S).group(0)
        links = re.findall(r"<a[^>]*>", tabs)
        assert links and all('href="/lists' in a and "hx-get" not in a for a in links), links

    @pytest.mark.asyncio
    async def test_list_type_tabs_keep_dates_and_page_size(self, ui_client):
        """Switching type keeps the chosen date range and page size, and starts a fresh
        search and status filter for the new type."""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_lists", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get("/lists?type=audit&q=abc&status=void&preset=custom&from=2026-01-01&to=2026-01-31&per_page=100",
                                    cookies=_cookies())
        assert r.status_code == 200
        tabs = re.search(r'<div[^>]*id="type-tabs".*?</div>', r.text, re.S).group(0)
        hrefs = [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]*)"', tabs)]
        kept = "per_page=100&preset=custom&from=2026-01-01&to=2026-01-31"
        assert hrefs[0] == f"/lists?{kept}", hrefs
        assert f"/lists?type=quotation&{kept}" in hrefs, hrefs
        assert not any("q=" in h or "status=" in h for h in hrefs), hrefs

    @pytest.mark.asyncio
    async def test_subscriptions_honor_per_page(self, ui_client):
        subs = [{"entity_id": f"sub:{i}", "ref_id": f"SUB-{i:03d}", "status": "active"} for i in range(60)]
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": subs})):
            r = await ui_client.get("/subscriptions?page=2&per_page=25", cookies=_cookies())
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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=listed), patch("ui.api_client.list_lists", new=listed), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(page, cookies=_cookies())
        assert r.status_code == 200
        box = next(i for i in re.findall(r"<input[^>]*>", r.text) if 'id="search-input"' in i)
        assert f'hx-target="#{content_id}"' in box, box
        area = re.search(rf'<div[^>]*id="{content_id}"', r.text)
        assert area, content_id
        after = r.text[area.start():]
        assert 'class="status-card' in after and "pagination" in after

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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch(f"ui.api_client.{api_fn}", new=fetch), \
             patch(f"ui.api_client.{summary_fn}", new=summary):
            r = await ui_client.get(path, cookies=_cookies(), headers={"HX-Request": "true"})
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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=empty), patch("ui.api_client.list_lists", new=empty), \
             patch("ui.api_client.list_contacts", new=empty), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(path, cookies=_cookies(), headers={"HX-Request": "true"})
        assert r.status_code == 200
        pushed = urlsplit(r.headers.get("HX-Push-Url", ""))
        expected = urlsplit(page_url)
        assert (pushed.path, parse_qs(pushed.query)) == (expected.path, parse_qs(expected.query)), r.headers

    @pytest.mark.asyncio
    @pytest.mark.parametrize("page", ["/docs?type=invoice&q=ruby", "/lists?type=audit&q=ruby", "/contacts/vendors?q=ruby"])
    async def test_search_box_shows_the_active_search(self, ui_client, page):
        listed = AsyncMock(return_value={"items": [_DOC], "total": 1})
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=listed), patch("ui.api_client.list_lists", new=listed), \
             patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})), \
             patch("ui.api_client.get_doc_summary", new=AsyncMock(return_value=_SUMMARY)), \
             patch("ui.api_client.get_list_summary", new=AsyncMock(return_value={"count_by_status": {}})):
            r = await ui_client.get(page, cookies=_cookies())
        assert r.status_code == 200
        box = next(i for i in re.findall(r"<input[^>]*>", r.text) if 'id="search-input"' in i)
        assert 'value="ruby"' in box, box

    @pytest.mark.asyncio
    async def test_vendor_search_stays_on_vendors(self, ui_client):
        listed = AsyncMock(return_value={"items": [], "total": 0})
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_contacts", new=listed):
            r = await ui_client.get("/contacts/vendors", cookies=_cookies())
            box = next(i for i in re.findall(r"<input[^>]*>", r.text) if 'id="search-input"' in i)
            search_url = re.search(r'hx-get="([^"]*)"', box).group(1).replace("&amp;", "&")
            await ui_client.get(f"{search_url}{'&' if '?' in search_url else '?'}q=ruby", cookies=_cookies(),
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
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
             patch("ui.api_client.list_docs", new=expired), patch("ui.api_client.list_lists", new=expired), \
             patch("ui.api_client.list_contacts", new=expired), \
             patch("ui.api_client.get_doc_summary", new=expired), \
             patch("ui.api_client.get_list_summary", new=expired):
            r = await ui_client.get(path, cookies=_cookies(), headers={"HX-Request": "true"})
        assert r.headers.get("HX-Redirect", "").startswith("/login"), (r.status_code, dict(r.headers))

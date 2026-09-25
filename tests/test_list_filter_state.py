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
        list_lists = AsyncMock(return_value={"items": [], "total": 0})
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
        page_hrefs = _hrefs(r.text, "page-btn")
        per_page = re.findall(r"window.location='([^\"]*)\"", r.text)
        assert per_page and "type=audit" in per_page[0] and "from=2026-01-01" in per_page[0], per_page


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

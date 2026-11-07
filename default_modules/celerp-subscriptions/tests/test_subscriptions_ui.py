# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""UI route coverage for celerp-subscriptions (ui/routes/subscriptions.py)."""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from bs4 import BeautifulSoup
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token
from ui.i18n import sentence, t


def _loads_with(r, page: str, message: str) -> None:
    """A done action loads ``page`` again (no fragment, so the browser does load it) and says
    what it did."""
    assert r.status_code == 204 and r.headers["HX-Redirect"] == page, (r.status_code, r.headers)
    assert json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"] == message


def _authed(role: str = "owner") -> dict:
    return {"celerp_token": make_test_token(role=role)}


_CONTACTS = [{"entity_id": "ct:1", "name": "Alice", "phone": "555", "email": "a@b.c",
              "tax_id": "T1", "credit_limit": 1000, "contact_type": "customer"}]

_SUB_DOC = {
    "entity_id": "sub-001",
    "id": "sub-001",
    "doc_type": "subscription_invoice",
    "name": "Monthly Retainer",
    "status": "active",
    "frequency": "monthly",
    "start_date": "2026-01-01",
    "anchor_date": "2026-01-01",
    "contact_id": "ct:1",
    "contact_name": "Alice",
    "currency": "USD",
    "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100}],
    "periods": {},
}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


class TestSubscriptionsUI:
    """UI route coverage for ui/routes/subscriptions.py."""

    async def test_list_unauthenticated_redirects(self, ui_client):
        r = await ui_client.get("/subscriptions")
        assert r.status_code == 302
        assert "/login" in r.headers["location"]

    async def test_list_sales_authenticated(self, ui_client):
        ui_client.cookies.update(_authed())
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": [_SUB_DOC]})):
            r = await ui_client.get("/subscriptions?direction=sales")
        assert r.status_code == 200
        assert "Sales Subscriptions" in r.text

    async def test_list_purchasing_authenticated(self, ui_client):
        ui_client.cookies.update(_authed())
        with patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": []})):
            r = await ui_client.get("/subscriptions?direction=purchasing")
        assert r.status_code == 200
        assert "Purchasing Subscriptions" in r.text

    async def test_new_subscription_unauthenticated_redirects(self, ui_client):
        r = await ui_client.post("/subscriptions/new?direction=sales")
        assert r.status_code == 302
        assert "/login" in r.headers["location"]

    async def test_create_subscription_redirects_to_detail(self, ui_client):
        ui_client.cookies.update(_authed())
        with patch("ui.api_client.create_subscription", new=AsyncMock(return_value=_SUB_DOC)):
            r = await ui_client.post(
                "/subscriptions/new?direction=sales",
            )
        _loads_with(r, "/subscriptions/sub-001", t("subscriptions.done_created", "en"))

    async def test_a_refused_new_agreement_says_why_where_the_user_is(self, ui_client):
        from ui.api_client import APIError
        ui_client.cookies.update(_authed())
        with patch("ui.api_client.create_subscription", new=AsyncMock(side_effect=APIError(403, "Not allowed here"))):
            r = await ui_client.post("/subscriptions/new?direction=sales")
        assert "HX-Redirect" not in r.headers
        assert "Not allowed here" in json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]

    async def test_detail_page_renders(self, ui_client):
        ui_client.cookies.update(_authed())
        with (
            patch("ui.api_client.get_subscription", new=AsyncMock(return_value=_SUB_DOC)),
            patch("ui.api_client.get_subscription_periods",
                  new=AsyncMock(return_value={"next_bill_date": "2026-02-01", "oldest_open_index": 0, "rows": []})),
            patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "ACME", "currency": "USD", "timezone": "UTC"})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_doc_notes", new=AsyncMock(return_value=[])),
        ):
            r = await ui_client.get("/subscriptions/sub-001")
        assert r.status_code == 200
        assert "sub-001" in r.text  # ref_id shown in breadcrumb/header
        assert "Generate due" in r.text  # active-status action visible
        from html import unescape
        assert '"period_index": 0' in unescape(r.text)

    async def test_oldest_due_period_and_older_pages_reachable(self, ui_client):
        ui_client.cookies.update(_authed())
        due = {"period_index": 0, "service_start": "2026-01-01",
               "service_end": "2026-01-31", "bill_date": "2026-01-01",
               "state": None, "doc_id": None, "status": "overdue", "reopened": False}
        recent = {**due, "period_index": 74, "service_start": "2032-03-01",
                  "service_end": "2032-03-31", "bill_date": "2032-03-01", "status": "upcoming"}
        schedule = {"next_bill_date": "2026-01-01", "oldest_open_index": 0,
                    "rows": [recent], "oldest_due_row": due, "older_before": 74}
        periods = AsyncMock(return_value=schedule)
        with (
            patch("ui.api_client.get_subscription", new=AsyncMock(return_value=_SUB_DOC)),
            patch("ui.api_client.get_subscription_periods", new=periods),
            patch("ui.api_client.get_company", new=AsyncMock(
                return_value={"name": "ACME", "currency": "USD", "timezone": "UTC"})),
            patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": []})),
            patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[])),
            patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
            patch("ui.api_client.list_doc_notes", new=AsyncMock(return_value=[])),
        ):
            first = await ui_client.get("/subscriptions/sub-001")
            assert first.status_code == 200, first.text
            assert "/subscriptions/sub-001?period_before=74" in first.text
            assert "/subscriptions/sub-001/periods/0/skip" in first.text
            older = await ui_client.get("/subscriptions/sub-001?period_before=74")
            assert older.status_code == 200, older.text
            assert periods.await_args_list[-1].kwargs["before"] == 74
            assert 'href="/subscriptions/sub-001"' in older.text


    async def test_detail_page_unauthenticated_redirects(self, ui_client):
        r = await ui_client.get("/subscriptions/sub-001")
        assert r.status_code == 302
        assert "/login" in r.headers["location"]

    async def test_detail_page_api_error_redirects_to_list(self, ui_client):
        from ui.api_client import APIError
        ui_client.cookies.update(_authed())
        with patch("ui.api_client.get_subscription", new=AsyncMock(side_effect=APIError(404, "not found"))):
            r = await ui_client.get("/subscriptions/bad-id")
        assert r.status_code == 302
        assert "/subscriptions" in r.headers["location"]


async def _active_subscription(ui) -> str:
    r = await ui.api.post("/docs", json={
        "doc_type": "subscription_invoice", "contact_id": "contact:test-001",
        "frequency": "monthly", "start_date": "2026-01-01",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0}],
    })
    assert r.status_code in (200, 201), r.text
    eid = r.json()["id"]
    assert (await ui.api.post(f"/subscriptions/{eid}/activate")).status_code == 200
    return eid


class TestSubscriptionActions:
    """Lifecycle buttons on the detail page, through the UI to the real API."""

    async def test_a_refused_action_says_why_and_stays_on_the_page(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        assert (await owner_ui.api.post(f"/subscriptions/{eid}/cancel")).status_code == 200
        r = await owner_ui.post(f"/subscriptions/{eid}/generate-due", data={"idempotency_key": "cancelled-check"})
        assert r.status_code == 200
        assert "location" not in r.headers and "HX-Redirect" not in r.headers
        assert t("subscriptions.err_generate_cancelled", "en") in json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]

    async def test_generate_sent_twice_with_its_key_creates_one_document(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        for _ in range(2):
            await owner_ui.post(f"/subscriptions/{eid}/generate-due", data={"idempotency_key": "gen-once"})
        sub = (await owner_ui.api.get(f"/docs/{eid}")).json()
        assert [e["state"] for e in sub["periods"].values()].count("billed") == 1

    async def test_period_skip_and_unskip_through_ui_persist(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        r = await owner_ui.post(f"/subscriptions/{eid}/periods/0/skip", data={"reason": "Closed"})
        assert r.status_code == 204, r.text
        assert (await owner_ui.api.get(f"/docs/{eid}")).json()["periods"]["0"]["state"] == "skipped"

        r = await owner_ui.post(f"/subscriptions/{eid}/periods/0/unskip")
        assert r.status_code == 204, r.text
        assert (await owner_ui.api.get(f"/docs/{eid}")).json()["periods"]["0"]["state"] == "released"

    async def test_an_unknown_action_is_not_found_and_calls_nothing(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        with patch("ui.api_client._client", side_effect=AssertionError("API called")):
            r = await owner_ui.post(f"/subscriptions/{eid}/delete")
        assert r.status_code == 404

    async def test_the_detail_page_buttons_post_in_place(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        html = (await owner_ui.get(f"/subscriptions/{eid}")).text
        assert f'hx-post="/subscriptions/{eid}/generate-due"' in html
        assert "idempotency_key" in html

    async def test_a_done_action_returns_to_the_subscription(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        r = await owner_ui.post(f"/subscriptions/{eid}/pause")
        assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/subscriptions/{eid}"
        assert (await owner_ui.api.get(f"/docs/{eid}")).json()["status"] == "paused"

    async def test_resume_toast_says_what_now_runs_automatically(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        r = await owner_ui.api.patch(f"/subscriptions/{eid}/schedule", json={"generation_mode": "auto_invoice"})
        assert r.status_code == 200, r.text
        assert (await owner_ui.api.post(f"/subscriptions/{eid}/pause")).status_code == 200
        r = await owner_ui.post(f"/subscriptions/{eid}/resume")
        assert r.status_code == 204, r.text
        toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
        answer = (await owner_ui.api.get(f"/docs/{eid}")).json()
        assert answer["status"] == "active" and set(answer["consents"]) == {"generation"}
        assert t("subscriptions.done_resumed", "en") in toast["message"] and "Owner" in toast["message"], toast
        assert toast["type"] == "success", toast


class TestBillingPages:
    """Generate all due, the catch-up page, the summary, the Billing section and history
    review, through the UI to the real API."""

    async def test_generate_all_due_previews_then_bills_each_agreement(self, owner_ui, monkeypatch):
        from agreement_kit import agreement, freeze_day
        freeze_day(monkeypatch, "2026-03-15")
        eid = await agreement(owner_ui.api, {}, start="2026-02-01")  # periods 0 and 1 due
        panel = (await owner_ui.get("/subscriptions/due?direction=sales")).text
        assert f'value="{eid}|0"' in panel
        soup = BeautifulSoup(panel, "html.parser")
        more = [th.get_text(strip=True) for th in soup.select("thead th")].index(t("subscriptions.th_more_due", "en"))
        assert soup.select_one(f'input[value="{eid}|0"]').find_parent("tr").select("td")[more].get_text(strip=True) == "1"
        assert 'name="idempotency_key"' in panel
        r = await owner_ui.post("/subscriptions/generate-due", data={"item": [f"{eid}|0"], "idempotency_key": "due-click"})
        assert t("subscriptions.due_result", "en", billed=1, refused=0) in r.text
        sub = (await owner_ui.api.get(f"/docs/{eid}")).json()
        assert [k for k, e in sub["periods"].items() if e["state"] == "billed"] == ["0"]

    async def test_a_refused_agreement_shows_why_with_retry(self, owner_ui, monkeypatch):
        from agreement_kit import agreement, freeze_day
        freeze_day(monkeypatch, "2026-03-15")
        eid = await agreement(owner_ui.api, {}, start="2026-03-01")
        assert (await owner_ui.api.post(f"/subscriptions/{eid}/pause")).status_code == 200
        r = await owner_ui.post("/subscriptions/generate-due", data={"item": [f"{eid}|0"], "idempotency_key": "due-click"})
        assert t("subscriptions.err_bill_paused", "en") in r.text
        assert t("btn.retry", "en") in r.text and "#sub-due-panel" in r.text
        assert "due-click" in r.text  # retry preserves the original batch identity

    async def test_nothing_due_names_the_next_bill_date_and_its_agreement(self, owner_ui, monkeypatch):
        from agreement_kit import agreement, freeze_day
        freeze_day(monkeypatch, "2026-03-15")
        eid = await agreement(owner_ui.api, {}, start="2026-04-01")
        panel = (await owner_ui.get("/subscriptions/due?direction=sales")).text
        assert "Nothing is due. The next bill date is 1 Apr 2026" in panel
        assert f'href="/subscriptions/{eid}"' in panel

    async def test_billing_mode_records_consent_and_shows_the_badge(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/generation_mode", data={"value": "auto_invoice"})
        assert t("subscriptions.bills_automatically", "en") in r.text and 'hx-swap-oob="true"' in r.text
        assert "Automatic billing authorized on" in r.text
        r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/generation_mode", data={"value": "manual_due"})
        assert t("subscriptions.bills_automatically", "en") not in r.text
        html = (await owner_ui.get(f"/subscriptions/{eid}")).text
        assert 'id="sub-billing"' in html and 'id="sub-auto-badge"' in html

    async def test_catch_up_page_bills_the_held_periods_and_the_list_says_so(self, owner_ui, session, monkeypatch):
        from sqlalchemy import select
        from agreement_kit import agreement, company_id, freeze_day, schedule, tick
        from celerp.models.notification import Notification
        freeze_day(monkeypatch, "2026-01-01")
        eid = await agreement(owner_ui.api, {}, start="2026-01-01")
        assert (await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice")).status_code == 200
        freeze_day(monkeypatch, "2026-03-15")
        await tick(session, await company_id(owner_ui.api, {}), "2026-03-15")
        notices = (await session.execute(select(Notification.action_url))).scalars().all()
        assert "/subscriptions/catch-up" in notices
        listing = (await owner_ui.get("/subscriptions?direction=sales")).text
        assert t("subscriptions.catchup_banner", "en", n=1, m=3) in listing
        page = (await owner_ui.get("/subscriptions/catch-up")).text
        assert f'value="{eid}|0"' in page and f'value="{eid}|2"' in page
        r = await owner_ui.post("/subscriptions/catch-up/run", data={"item": [f"{eid}|0", f"{eid}|1", f"{eid}|2"], "idempotency_key": "catchup-click"})
        assert len(BeautifulSoup(r.text, "html.parser").select("#catchup-card td a[href^='/docs/']")) == 3
        assert t("subscriptions.catchup_empty", "en") in r.text

    async def test_a_catch_up_run_reports_the_batches_it_billed_when_a_later_one_fails(
            self, owner_ui, session, monkeypatch):
        """The catch-up page bills the chosen periods in batches. The first batch bills 50; the
        second fails. The answer lists the 50 invoices beside the failure, never a refusal (P2e-2)."""
        import ui.api_client as api
        from agreement_kit import agreement, billed_docs, freeze_day, schedule
        freeze_day(monkeypatch, "2026-09-01")
        eids = []
        for _ in range(2):
            eid = await agreement(owner_ui.api, {}, start="2026-01-05", frequency="weekly")
            assert (await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
            eids.append(eid)
        preview = (await owner_ui.api.get("/subscriptions/catch-up")).json()["items"]
        items = [f"{i['entity_id']}|{p['period_index']}" for i in preview for p in i["periods"]]
        assert len(items) > 50
        real, calls = api.run_subscriptions_catch_up, []

        async def second_fails(token, chunk):
            calls.append(len(chunk))
            if len(calls) > 1:
                raise api.APIError(503, "Service temporarily unavailable")
            return await real(token, chunk)
        monkeypatch.setattr(api, "run_subscriptions_catch_up", second_fails)
        r = await owner_ui.post("/subscriptions/catch-up/run", data={"item": items, "idempotency_key": "two-batches"})
        assert r.status_code == 200 and r.headers.get("HX-Reswap") != "none"
        assert "error" != json.loads(r.headers.get("HX-Trigger") or "{}").get("celerpToast", {}).get("type")
        billed = [d for e in eids for d in await billed_docs(session, e)]
        links = BeautifulSoup(r.text, "html.parser").select("#catchup-card td a[href^='/docs/']")
        assert len(billed) == 50 and {a["href"] for a in links} == {f"/docs/{d['entity_id']}" for d in billed}
        assert r.text.count("Service temporarily unavailable") == len(items) - 50

    async def test_catch_up_skip_needs_a_reason_per_period(self, owner_ui, monkeypatch):
        from agreement_kit import agreement, freeze_day, schedule
        freeze_day(monkeypatch, "2026-01-01")
        eid = await agreement(owner_ui.api, {}, start="2026-01-01")
        assert (await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice")).status_code == 200
        freeze_day(monkeypatch, "2026-03-15")
        r = await owner_ui.post("/subscriptions/catch-up/skip", data={"item": [f"{eid}|0"], "reason": ""})
        assert t("subscriptions.err_skip_reason", "en") in r.text
        r = await owner_ui.post("/subscriptions/catch-up/skip", data={"item": [f"{eid}|0"], "reason": "Closed"})
        assert sentence("subscriptions.done_skipped", "en", start="2026-01-01", end="2026-01-31") in r.text
        sub = (await owner_ui.api.get(f"/docs/{eid}")).json()
        assert sub["periods"]["0"]["state"] == "skipped"

    async def test_summary_page_lists_unattended_rows_and_mark_as_read_empties_it(self, owner_ui, session, monkeypatch):
        from agreement_kit import agreement, company_id, freeze_day, schedule, tick
        freeze_day(monkeypatch, "2026-03-01")
        eid = await agreement(owner_ui.api, {}, start="2026-03-01")
        assert (await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice")).status_code == 200
        await tick(session, await company_id(owner_ui.api, {}), "2026-03-01")
        listing = (await owner_ui.get("/subscriptions?direction=sales")).text
        assert t("subscriptions.summary_banner", "en", generated=1, sent=0, failed=0) in listing
        page = (await owner_ui.get("/subscriptions/summary")).text
        assert f'href="/subscriptions/{eid}"' in page and t("subscriptions.summary_ack", "en") in page
        r = await owner_ui.post("/subscriptions/summary/ack")
        _loads_with(r, "/subscriptions/summary", t("subscriptions.done_summary_read", "en"))
        assert t("subscriptions.summary_empty", "en") in (await owner_ui.get("/subscriptions/summary")).text
        assert f'href="/subscriptions/{eid}"' in (await owner_ui.get("/subscriptions/summary?all=1")).text

    async def test_history_review_places_a_document_and_withdraw_returns_it(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        review = {"next_bill_date": None, "rows": [], "review": [
            {"doc_id": "doc:old", "ref_id": "INV-OLD", "candidates": [
                {"period_index": -2, "service_start": "2025-11-01", "service_end": "2025-11-30", "bill_date": "2025-11-01"}]}]}
        with patch("ui.api_client.get_subscription_periods", new=AsyncMock(return_value=review)):
            html = (await owner_ui.get(f"/subscriptions/{eid}")).text
        assert 'id="history-review"' in html and "INV-OLD" in html
        assert f'hx-post="/subscriptions/{eid}/history/doc:old/confirm"' in html
        confirm = AsyncMock(return_value={"ok": True})
        with patch("ui.api_client.subscription_history_action", new=confirm):
            r = await owner_ui.post(f"/subscriptions/{eid}/history/doc:old/confirm", data={"period_index": "-2"})
        _loads_with(r, f"/subscriptions/{eid}", t("subscriptions.done_history_placed", "en"))
        assert confirm.await_args.args[2:] == ("doc:old", "confirm", {"period_index": -2})
        r = await owner_ui.post(f"/subscriptions/{eid}/history/doc:old/withdraw")
        assert t("subscriptions.err_history_not_confirmed", "en") in json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]

    async def test_history_review_sets_aside_a_document_no_period_can_take(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        review = {"next_bill_date": None, "rows": [], "review": [{"doc_id": "doc:gone", "ref_id": None, "candidates": []}],
                  "set_aside": [{"doc_id": "doc:dup", "ref_id": "INV-DUP"}]}
        with patch("ui.api_client.get_subscription_periods", new=AsyncMock(return_value=review)):
            html = (await owner_ui.get(f"/subscriptions/{eid}")).text
        assert html.count(t("subscriptions.history_set_aside", "en")) >= 1 and "no issue date" in html
        assert f'hx-post="/subscriptions/{eid}/history/doc:gone/set-aside"' in html
        assert "INV-DUP" in html and f'hx-post="/subscriptions/{eid}/history/doc:dup/withdraw"' in html
        action = AsyncMock(return_value={"ok": True})
        with patch("ui.api_client.subscription_history_action", new=action):
            r = await owner_ui.post(f"/subscriptions/{eid}/history/doc:gone/set-aside")
        _loads_with(r, f"/subscriptions/{eid}", t("subscriptions.done_history_set_aside", "en"))
        assert action.await_args.args[2:] == ("doc:gone", "set-aside", None)
        with patch("ui.api_client.subscription_history_action", new=action):
            r = await owner_ui.post(f"/subscriptions/{eid}/history/doc:dup/withdraw")
        _loads_with(r, f"/subscriptions/{eid}", t("subscriptions.done_history_returned", "en"))


async def test_history_set_aside_through_ui_persists(owner_ui, session):
    from uuid import UUID, uuid4
    from celerp.events.engine import emit_event
    from agreement_kit import company_id

    eid = await _active_subscription(owner_ui)
    cid = UUID(await company_id(owner_ui.api, {}))
    await emit_event(
        session, company_id=cid, entity_id=eid, entity_type="doc", event_type="doc.updated",
        data={"fields_changed": {"history_review": {
            "new": [{"doc_id": "doc:missing", "candidates": []}]}}},
        actor_id=None, location_id=None, source="test", idempotency_key=str(uuid4()),
    )
    await session.commit()

    r = await owner_ui.post(f"/subscriptions/{eid}/history/doc:missing/set-aside")
    _loads_with(r, f"/subscriptions/{eid}", t("subscriptions.done_history_set_aside", "en"))
    state = (await owner_ui.api.get(f"/docs/{eid}")).json()
    assert any(row["doc_id"] == "doc:missing" for row in state["history_set_aside"])
    # The page it loads again offers Return to review, not Set aside, for that document.
    page = (await owner_ui.get(r.headers["HX-Redirect"])).text
    assert f'/history/doc:missing/withdraw"' in page and f'/history/doc:missing/set-aside"' not in page


@pytest.mark.parametrize("method,path,api_call,form", [
    ("post", "/subscriptions/{eid}/periods/0/skip", "subscription_period_action", {"reason": "Closed"}),
    ("patch", "/subscriptions/{eid}/schedule/generation_mode", "update_subscription_schedule", {"value": "manual_due"}),
    ("post", "/subscriptions/generate-due", "generate_all_due", {"item": ["{eid}|0"], "idempotency_key": "replay-key"}),
    ("post", "/subscriptions/summary/ack", "acknowledge_subscriptions_summary", {}),
    ("get", "/subscriptions/catch-up", "subscriptions_catch_up", None),
    ("get", "/subscriptions/summary", "subscriptions_summary", None),
])
async def test_an_ended_session_on_a_billing_action_goes_to_sign_in(owner_ui, method, path, api_call, form):
    """A billing action whose session has ended sends the browser to sign in, as every other
    screen does, instead of showing the sign-in refusal as a failed action."""
    from ui.api_client import APIError

    eid = await _active_subscription(owner_ui)
    sent = {} if form is None else {"data": {k: ([x.format(eid=eid) for x in v] if isinstance(v, list) else v)
                                             for k, v in form.items()}, "headers": {"HX-Request": "true"}}
    with patch(f"ui.api_client.{api_call}", AsyncMock(side_effect=APIError(401, "Session expired"))):
        r = await getattr(owner_ui, method)(path.format(eid=eid), **sent)
    to = r.headers.get("HX-Redirect") or r.headers.get("location", "")
    assert to.startswith("/login"), (r.status_code, r.headers, r.text[:200])

@pytest.mark.asyncio
async def test_api_client_batches_large_due_selection(monkeypatch):
    from contextlib import asynccontextmanager
    from httpx import Request, Response
    from ui import api_client

    batches = []

    class Client:
        async def post(self, path, json):
            assert path == "/subscriptions/generate-due"
            batches.append(json["items"])
            return Response(200, request=Request("POST", "http://test" + path), json={
                "results": [{"entity_id": row["entity_id"], "period_index": row["period_index"],
                             "ok": True, "doc_id": f"doc:{row['entity_id']}"}
                            for row in json["items"]]
            })

    @asynccontextmanager
    async def client(_token):
        yield Client()

    monkeypatch.setattr(api_client, "_api_client", client)
    items = [{"entity_id": f"sub:{i}", "period_index": 0, "request_key": f"bulk:{i}"} for i in range(120)]
    result = await api_client.generate_all_due("token", items)
    assert [len(b) for b in batches] == [50, 50, 20]
    assert result["billed"] == 120 and result["refused"] == 0
    assert [r["entity_id"] for r in result["results"]] == [i["entity_id"] for i in items]


@pytest.mark.asyncio
async def test_ui_posts_the_original_period_with_a_request_key(ui_client):
    ui_client.cookies.update(_authed())
    billed = {"doc_id": "doc:1", "ref_id": "INV-1", "period_index": 0, "service_start": "2026-01-01",
              "service_end": "2026-01-31", "bill_date": "2026-01-01", "already_billed": False}
    with patch("ui.api_client.subscription_action", new=AsyncMock(return_value=billed)) as action:
        r = await ui_client.post("/subscriptions/sub-001/generate-due",
                                 data={"idempotency_key": "user-click", "period_index": "0"})
    assert r.status_code == 204
    action.assert_awaited_once_with(
        ui_client.cookies.get("celerp_token"), "sub-001", "generate-due",
        {"request_key": "user-click", "period_index": 0})


@pytest.mark.asyncio
async def test_subscription_read_errors_are_not_shown_as_empty(ui_client):
    from ui.api_client import APIError

    ui_client.cookies.update(_authed())
    cases = [
        ("/subscriptions", "ui.api_client.list_subscriptions"),
        ("/subscriptions/search", "ui.api_client.list_subscriptions"),
        ("/subscriptions/catch-up", "ui.api_client.subscriptions_catch_up"),
        ("/subscriptions/summary", "ui.api_client.subscriptions_summary"),
    ]
    for path, method in cases:
        with patch(method, new=AsyncMock(side_effect=APIError(503, "Service unavailable"))):
            response = await ui_client.get(path)
        assert response.status_code == 200, path
        assert "Service unavailable" in response.text, path


@pytest.mark.asyncio
async def test_subscription_detail_requires_its_periods(ui_client):
    from ui.api_client import APIError

    ui_client.cookies.update(_authed())
    with (
        patch("ui.api_client.get_subscription", new=AsyncMock(return_value=_SUB_DOC)),
        patch("ui.api_client.get_subscription_periods",
              new=AsyncMock(side_effect=APIError(503, "Period service unavailable"))),
        patch("ui.api_client.get_company", new=AsyncMock(return_value={"name": "ACME", "timezone": "UTC"})),
        patch("ui.api_client.list_ledger", new=AsyncMock(return_value={"items": []})),
        patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[])),
        patch("ui.api_client.get_taxes", new=AsyncMock(return_value=[])),
        patch("ui.api_client.list_doc_notes", new=AsyncMock(return_value=[])),
    ):
        response = await ui_client.get("/subscriptions/sub-001")
    assert response.status_code == 200
    assert "Period service unavailable" in response.text
    assert "Generate due" not in response.text


@pytest.mark.asyncio
async def test_optional_billing_summary_failure_is_visible_on_list(ui_client):
    from ui.api_client import APIError

    ui_client.cookies.update(_authed())
    with (
        patch("ui.api_client.list_subscriptions", new=AsyncMock(return_value={"items": [], "total": 0})),
        patch("ui.api_client.subscriptions_catch_up", new=AsyncMock(return_value={"items": []})),
        patch("ui.api_client.subscriptions_summary",
              new=AsyncMock(side_effect=APIError(503, "Billing summary unavailable"))),
    ):
        response = await ui_client.get("/subscriptions")
    assert response.status_code == 200
    assert "Billing summary unavailable" in response.text

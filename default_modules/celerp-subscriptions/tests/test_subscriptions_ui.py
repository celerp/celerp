# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""UI route coverage for celerp-subscriptions (ui/routes/subscriptions.py)."""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token
from ui.i18n import t


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
        # Route returns 204 + HX-Redirect so HTMX can follow the redirect
        assert r.status_code == 204
        assert "/subscriptions/sub-001" in r.headers["hx-redirect"]

    async def test_detail_page_renders(self, ui_client):
        ui_client.cookies.update(_authed())
        with (
            patch("ui.api_client.get_subscription", new=AsyncMock(return_value=_SUB_DOC)),
            patch("ui.api_client.get_subscription_periods",
                  new=AsyncMock(return_value={"next_bill_date": "2026-02-01", "rows": []})),
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
        r = await owner_ui.post(f"/subscriptions/{eid}/generate-due")
        assert r.status_code == 200
        assert "location" not in r.headers and "HX-Redirect" not in r.headers
        assert t("subscriptions.err_generate_cancelled", "en") in json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]

    async def test_generate_sent_twice_with_its_key_creates_one_document(self, owner_ui):
        eid = await _active_subscription(owner_ui)
        for _ in range(2):
            await owner_ui.post(f"/subscriptions/{eid}/generate-due", data={"idempotency_key": "gen-once"})
        sub = (await owner_ui.api.get(f"/docs/{eid}")).json()
        assert [e["state"] for e in sub["periods"].values()].count("billed") == 1

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


class TestBillingPages:
    """Generate all due, the catch-up page, the summary, the Billing section and history
    review, through the UI to the real API."""

    async def test_generate_all_due_previews_then_bills_each_agreement(self, owner_ui, monkeypatch):
        from agreement_kit import agreement, freeze_day
        freeze_day(monkeypatch, "2026-03-15")
        eid = await agreement(owner_ui.api, {}, start="2026-02-01")  # periods 0 and 1 due
        panel = (await owner_ui.get("/subscriptions/due?direction=sales")).text
        assert f'value="{eid}"' in panel and t("subscriptions.due_more", "en", n=1) in panel
        r = await owner_ui.post("/subscriptions/generate-due", data={"ids": [eid]})
        assert t("subscriptions.due_result", "en", billed=1, refused=0) in r.text
        sub = (await owner_ui.api.get(f"/docs/{eid}")).json()
        assert [k for k, e in sub["periods"].items() if e["state"] == "billed"] == ["0"]

    async def test_a_refused_agreement_shows_why_with_retry(self, owner_ui, monkeypatch):
        from agreement_kit import agreement, freeze_day
        freeze_day(monkeypatch, "2026-03-15")
        eid = await agreement(owner_ui.api, {}, start="2026-03-01")
        assert (await owner_ui.api.post(f"/subscriptions/{eid}/pause")).status_code == 200
        r = await owner_ui.post("/subscriptions/generate-due", data={"ids": [eid]})
        assert t("subscriptions.err_bill_paused", "en") in r.text
        assert t("btn.retry", "en") in r.text and "#sub-due-panel" in r.text

    async def test_nothing_due_names_the_next_bill_date_and_its_agreement(self, owner_ui, monkeypatch):
        from agreement_kit import agreement, freeze_day
        freeze_day(monkeypatch, "2026-03-15")
        eid = await agreement(owner_ui.api, {}, start="2026-04-01")
        panel = (await owner_ui.get("/subscriptions/due?direction=sales")).text
        assert "Nothing is due. The next bill date is 2026-04-01" in panel
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
        r = await owner_ui.post("/subscriptions/catch-up/run", data={"item": [f"{eid}|0", f"{eid}|1", f"{eid}|2"]})
        assert t("subscriptions.catchup_result", "en", done=3, refused=0) in r.text
        assert t("subscriptions.catchup_empty", "en") in r.text

    async def test_catch_up_skip_needs_a_reason_per_period(self, owner_ui, monkeypatch):
        from agreement_kit import agreement, freeze_day, schedule
        freeze_day(monkeypatch, "2026-01-01")
        eid = await agreement(owner_ui.api, {}, start="2026-01-01")
        assert (await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice")).status_code == 200
        freeze_day(monkeypatch, "2026-03-15")
        r = await owner_ui.post("/subscriptions/catch-up/skip", data={"item": [f"{eid}|0"], "reason": ""})
        assert t("subscriptions.catchup_result", "en", done=0, refused=1) in r.text
        r = await owner_ui.post("/subscriptions/catch-up/skip", data={"item": [f"{eid}|0"], "reason": "Closed"})
        assert t("subscriptions.catchup_result", "en", done=1, refused=0) in r.text
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
        assert r.status_code == 204 and r.headers["HX-Redirect"] == "/subscriptions/summary"
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
        assert r.headers["HX-Redirect"] == f"/subscriptions/{eid}#history-review"
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
        assert r.headers["HX-Redirect"] == f"/subscriptions/{eid}#history-review"
        assert action.await_args.args[2:] == ("doc:gone", "set-aside", None)


@pytest.mark.parametrize("method,path,api_call,form", [
    ("post", "/subscriptions/{eid}/periods/0/skip", "subscription_period_action", {"reason": "Closed"}),
    ("patch", "/subscriptions/{eid}/schedule/generation_mode", "update_subscription_schedule", {"value": "manual_due"}),
    ("post", "/subscriptions/generate-due", "generate_all_due", {"ids": ["{eid}"]}),
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

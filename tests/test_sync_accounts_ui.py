# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The Sync accounts page: a connected company's records held for an account, each with
its one-click fix; the accounts sync added, each kept, archived or removed; and the
firm's first-pairing review of a client's accounts. Every action answers in place."""
from __future__ import annotations

import json

import pytest
from fasthtml.common import to_xml
from fastapi import FastAPI

from ui import api_client
from ui.routes import sync_accounts as routes

pytestmark = pytest.mark.asyncio

OTHER = {"id": "a-1", "code": "6150", "name": "Courier", "account_type": "expense", "currency": "USD"}
HOLDS = [
    {"id": "h-collision", "source_company_id": "c-1", "label": "Courier run", "at": "2026-10-16T10:00:00",
     "reason": "Demo Trading's account 6150 Courier has the same code as 6150 Subscriptions in your chart.",
     "fix": {"kind": "collision", "other": OTHER, "local": "6150", "suggested": "6151", "add": True}},
    {"id": "h-type", "source_company_id": "c-1", "label": "Deposit", "at": "2026-10-16T09:00:00",
     "reason": "Demo Trading's account 6180 Deposits paid is an asset account.",
     "fix": {"kind": "type", "other": {**OTHER, "code": "6180", "name": "Deposits paid", "account_type": "asset"},
             "local": "6180", "suggested": "6181", "add": True}},
    {"id": "h-archived", "source_company_id": "c-1", "label": "Insurance accrual", "at": "2026-10-16T08:00:00",
     "reason": "Account 6700 Insurance, which you archived, is where Firm Practice's 6700 Insurance posts.",
     "fix": {"kind": "archived", "other": {**OTHER, "code": "6700", "name": "Insurance"}, "local": "6700",
             "add": True}},
    {"id": "h-role", "source_company_id": "c-1", "label": "Sales return", "at": "2026-10-16T07:00:00",
     "reason": "1200 Receivable is the Receivable account of Firm Practice.",
     "fix": {"kind": "role", "other": OTHER, "local": None, "add": False,
             "href": "/settings/accounting#posting-accounts"}},
    {"id": "h-currency", "source_company_id": "c-1", "label": "Euro deposit", "at": "2026-10-16T06:00:00",
     "reason": "Demo Trading's account 1119 Euro account is kept in EUR.",
     "fix": {"kind": "currency", "other": {**OTHER, "code": "1119", "currency": "EUR"}, "local": None,
             "add": False}},
    {"id": "h-review", "source_company_id": "c-1", "label": "Rent", "at": "2026-10-16T05:00:00",
     "reason": "Waiting for Demo Books to review accounts.",
     "fix": {"kind": "review", "other": OTHER, "add": False, "accounts": [OTHER]}},
]
ADDED = [
    {"id": "n-1", "label": "6160 Audit adjustments", "code": "6160", "removable": True, "why": None,
     "reason": "Added by sync from Firm Practice with the journal Adjust.", "at": "2026-10-16T10:00:00"},
    {"id": "n-2", "label": "6150 Courier", "code": "6150", "removable": False,
     "why": "It has entries, so it can be archived, not removed.",
     "reason": "Added by sync from Demo Trading with the journal Courier run.", "at": "2026-10-16T09:00:00"},
]
REVIEW = [{"source_company_id": "c-1", "client": "Demo Trading", "accounts": [
    {"id": "a-1", "code": "6150", "name": "Courier", "account_type": "expense",
     "why": "Named Subscriptions here", "suggested": "6155", "previous": True, "add_code": "6151"},
    {"id": "a-2", "code": "6190", "name": "Tolls", "account_type": "expense",
     "why": "Only in the client's chart", "suggested": None, "previous": False, "add_code": "6190"},
]}]
CHART = {"items": [
    {"code": "6150", "name": "Subscriptions", "account_type": "expense", "is_active": True},
    {"code": "6155", "name": "Couriers", "account_type": "expense", "is_active": True},
    {"code": "1140", "name": "Deposits", "account_type": "asset", "is_active": True},
    {"code": "6999", "name": "Old", "account_type": "expense", "is_active": False},
]}


class Request:
    headers = {"HX-Request": "true"}
    cookies: dict = {}
    query_params: dict = {}

    def __init__(self, form: dict | None = None):
        self._form = form or {}

    async def form(self):
        return self._form


def _app(monkeypatch, *, holds=HOLDS, added=ADDED, review=REVIEW, refuse=None, syncs=True):
    calls = []

    async def peer_holds(token):
        return {"items": holds, "added": added}

    async def peer_review(token):
        return {"companies": review}

    async def get_chart(token):
        return CHART

    async def fix_peer_hold(token, item_id, body):
        calls.append(("fix", item_id, body))
        if refuse:
            raise api_client.APIError(422, refuse)
        return {"ok": True}

    async def settle_added_account(token, item_id, action):
        calls.append(("added", item_id, action))
        if refuse:
            raise api_client.APIError(409, refuse)
        return {"ok": True}

    async def answer_peer_review(token, body):
        calls.append(("review", body))
        return {"ok": True}

    async def start_connector_sync(token, platform):
        calls.append(("sync", platform))
        return {"ok": syncs, "detail": None if syncs else "This company has no Celerp connection."}

    for name, fn in dict(peer_holds=peer_holds, peer_review=peer_review, get_chart=get_chart,
                         fix_peer_hold=fix_peer_hold, settle_added_account=settle_added_account,
                         answer_peer_review=answer_peer_review,
                         start_connector_sync=start_connector_sync).items():
        monkeypatch.setattr(api_client, name, fn)
    monkeypatch.setattr(routes, "_token", lambda request: "test-token")
    monkeypatch.setattr(routes, "get_lang", lambda request: "en")
    app = FastAPI()
    routes.setup_routes(app)
    return {r.path: r.endpoint for r in app.routes if hasattr(r, "path")}, calls


async def test_held_records_each_offer_their_fix(monkeypatch):
    _app(monkeypatch)
    html = to_xml(await routes.sync_accounts_body("test-token", "en"))
    order = [html.index(label) for label in ("Courier run", "Deposit", "Insurance accrual", "Sales return",
                                             "Euro deposit")]
    assert order == sorted(order)  # newest first, as the books return them
    assert "Use 6150 Subscriptions" in html and "Add as 6151" in html  # T23 code taken
    assert "Use another account" in html and "Add a new account" in html  # T23 type
    assert "Unarchive 6700" in html  # T23 archived
    assert 'href="/settings/accounting#posting-accounts"' in html and "Choose posting account" in html
    euro = html[html.index("Euro deposit"):]
    assert "Add a new account" not in euro.split("</tr>")[0]  # a currency lives on a bank account
    assert "6999" not in html  # archived accounts are not offered


async def test_a_fixed_record_waits_for_the_next_sync_without_its_fix(monkeypatch):
    fixed = [{**HOLDS[0], "fix": {**HOLDS[0]["fix"], "fixed": True}}]
    _app(monkeypatch, holds=fixed, added=[], review=[])
    html = to_xml(await routes.sync_accounts_body("test-token", "en"))
    row = html[html.index("Courier run"):].split("</tr>")[0]
    assert "Fixed. It posts on the next sync." in row
    assert "<button" not in row and "Use another account" not in row


async def test_added_accounts_offer_remove_only_when_unused(monkeypatch):
    _app(monkeypatch)
    html = to_xml(await routes.sync_accounts_body("test-token", "en"))
    added = html[html.index("Accounts added by sync"):]
    unused = added[added.index("6160 Audit adjustments"):].split("</tr>")[0]
    used = added[added.index("6150 Courier"):].split("</tr>")[0]
    assert ">Remove<" in unused and ">Archive<" not in unused
    assert "cannot be undone" in unused and "added again by the next record" in unused
    assert ">Remove<" not in used and ">Archive<" in used
    assert "It has entries, so it can be archived, not removed." in used
    assert ">Keep<" in unused and ">Keep<" in used
    assert 'href="/reports/ledger/6160"' in unused  # Open account


async def test_review_lists_only_rows_needing_an_answer(monkeypatch):
    _app(monkeypatch)
    html = to_xml(await routes.sync_accounts_body("test-token", "en"))
    assert "Review Demo Trading&#x27;s accounts before their records come in." in html \
        or "Review Demo Trading's accounts before their records come in." in html
    assert "2 accounts to review" in html
    assert "From your previous clients" in html
    assert 'value="6155"' in html  # the previous answer is preselected
    assert "Add as new (6190)" in html
    assert "Confirm accounts" in html and "Save and finish later" in html
    assert "Waiting for Demo Books to review accounts." not in html  # review holds are not listed twice


def test_review_counts_one_account_in_the_singular():
    company = {**REVIEW[0], "accounts": REVIEW[0]["accounts"][1:]}
    html = to_xml(routes._review(company, [], "en"))
    assert "1 account to review" in html and "1 accounts" not in html


async def test_nothing_to_fix_says_so(monkeypatch):
    _app(monkeypatch, holds=[], added=[], review=[])
    html = to_xml(await routes.sync_accounts_body("test-token", "en"))
    assert "Nothing to fix." in html and "<table" not in html


async def test_a_fix_on_the_client_syncs_at_once(monkeypatch):
    paths, calls = _app(monkeypatch)
    endpoint = paths["/accounting/sync-accounts/holds/{item_id}"]
    response = await endpoint(Request({"action": "add", "code": "6151"}), "h-collision")
    assert calls[0] == ("fix", "h-collision", {"action": "add", "code": "6151"})
    assert ("sync", "celerp") in calls
    assert 'id="sync-accounts"' in response.body.decode()
    toast = json.loads(response.headers["HX-Trigger"])["celerpToast"]
    assert toast["message"] == "Fixed. Syncing now, so the record posts on this run."


async def test_a_fix_on_the_firm_posts_on_the_clients_next_run(monkeypatch):
    paths, calls = _app(monkeypatch, syncs=False)
    endpoint = paths["/accounting/sync-accounts/holds/{item_id}"]
    response = await endpoint(Request({"action": "use", "account": "6155"}), "h-type")
    toast = json.loads(response.headers["HX-Trigger"])["celerpToast"]
    assert toast["message"] == "Fixed. The record posts on the connected company's next sync."


async def test_a_refused_fix_is_shown_in_place(monkeypatch):
    paths, calls = _app(monkeypatch, refuse="6180 Deposits paid is an asset account. Use an asset account.")
    endpoint = paths["/accounting/sync-accounts/holds/{item_id}"]
    html = to_xml(await endpoint(Request({"action": "use", "account": "6150"}), "h-type"))
    assert "Use an asset account." in html and 'id="sync-accounts"' in html
    assert not [c for c in calls if c[0] == "sync"]


async def test_added_account_actions(monkeypatch):
    paths, calls = _app(monkeypatch)
    endpoint = paths["/accounting/sync-accounts/added/{item_id}"]
    response = await endpoint(Request({"action": "remove"}), "n-1")
    assert calls == [("added", "n-1", "remove")]
    assert json.loads(response.headers["HX-Trigger"])["celerpToast"]["message"] == "Removed 6160 Audit adjustments."


async def test_review_answers_are_sent(monkeypatch):
    paths, calls = _app(monkeypatch)
    endpoint = paths["/accounting/sync-accounts/review"]
    await endpoint(Request({"source_company_id": "c-1", "finish": "1",
                            "answer_a-1": "6155", "answer_a-2": "add:6190"}))
    assert calls == [("review", {"source_company_id": "c-1", "finish": True, "answers": [
        {"account": "a-1", "use": "6155"}, {"account": "a-2", "add": "6190"}]})]


async def test_unanswered_rows_are_left_for_later(monkeypatch):
    paths, calls = _app(monkeypatch)
    endpoint = paths["/accounting/sync-accounts/review"]
    await endpoint(Request({"source_company_id": "c-1", "answer_a-1": "6155", "answer_a-2": ""}))
    assert calls == [("review", {"source_company_id": "c-1", "finish": False, "answers": [
        {"account": "a-1", "use": "6155"}]})]


async def test_the_ledger_names_the_account_as_the_other_company_does(monkeypatch):
    from ui.routes.financial_reports import elsewhere_note
    html = to_xml(elsewhere_note([{"company": "Demo Trading", "code": "6150", "name": "Courier"},
                                  {"company": None, "code": "6151", "name": "Post"}]))
    assert "Demo Trading calls this account 6150 Courier." in html
    assert "The connected company calls this account 6151 Post." in html
    assert elsewhere_note([]) == ""


async def test_firm_view_links_account_items_to_the_page():
    from ui.routes.firm_view import _attention_link
    row = {"company_id": "c-9"}
    html = to_xml(_attention_link(row, {"kind": "refused", "href": "/accounting/sync-accounts"}, "en"))
    assert "/accounting/sync-accounts" in html

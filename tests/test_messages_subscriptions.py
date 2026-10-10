# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Subscription messages say why an action was refused and what to do instead."""
from __future__ import annotations

import uuid

import pytest

from ui.i18n import t


async def _token(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "SubMsgCo", "email": f"sub-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_missing_subscription_says_refresh(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    r = await client.post("/subscriptions/doc:missing/pause", headers=h)
    assert r.status_code == 404
    assert r.json()["detail"] == t("subscriptions.err_not_found", "en")


@pytest.mark.asyncio
async def test_pausing_a_draft_says_only_active_can_pause(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    r = await client.post("/docs", headers=h, json={
        "doc_type": "subscription_invoice", "contact_id": "contact:test-001", "frequency": "monthly",
        "start_date": "2026-01-01",
        "line_items": [{"description": "S", "quantity": 1, "unit_price": 1.0, "line_total": 1.0}]})
    assert r.status_code in {200, 201}
    r = await client.post(f"/subscriptions/{r.json()['id']}/pause", headers=h)
    assert r.status_code == 409
    assert r.json()["detail"] == t("subscriptions.err_pause_not_active", "en")


@pytest.mark.parametrize("key, says", [
    ("subscriptions.err_not_found", "Refresh the list."),
    ("subscriptions.err_generate_cancelled", "Create a new subscription"),
    ("subscriptions.err_pause_not_active", "Only an active subscription can be paused."),
    ("subscriptions.err_resume_not_paused", "Only a paused subscription can be resumed."),
    ("subscriptions.err_already_cancelled", "Nothing more to do."),
    ("subscriptions.err_activate_not_draft", "Only a draft subscription can be activated."),
    ("subscriptions.err_frequency_required", "then activate it"),
    ("msg.could_not_load_quota_data", "Refresh the page."),
    ("msg.could_not_load_usage_data", "Refresh the page."),
    ("error.api_not_found", "use the menu"),
])
def test_subscription_messages_say_what_to_do(key, says):
    assert says in t(key, "en")


@pytest.mark.asyncio
async def test_unknown_api_address_gets_the_generic_message(client):
    r = await client.get("/no-such-route-anywhere")
    assert r.status_code == 404
    assert r.json()["detail"] == t("error.api_not_found", "en")

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Turning a module off from the Modules page while the company's work in it is still
running: the page waits longer than the turn-off does, so the user always sees the
outcome, here the message to try again, never the "not available" panel."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import ui.api_client as api_client
from celerp.capacity import MODULE_SETTLE_SECONDS
from celerp.modules import boundary
from celerp.modules.registry import uses_module

pytestmark = pytest.mark.asyncio

LABELS = "celerp-labels"


async def test_the_modules_page_shows_the_busy_message_and_waits_out_the_turn_off(owner_ui, monkeypatch):
    company = (await owner_ui.api.get("/companies/me")).json()
    cid = str(company["id"])
    bridged = api_client._client
    waits: list[float] = []

    def _recording(token, timeout=10.0):
        waits.append(timeout)
        return bridged(token, timeout)

    # The company's Labels work still running, and a short wait so the test is quick.
    monkeypatch.setattr(boundary, "MODULE_SETTLE_SECONDS", 0.2)
    monkeypatch.setitem(boundary._RUNNING, (cid, LABELS), 1)

    # Scoped to the request: owner_ui's own patch of _client must be what is undone last.
    with patch.object(api_client, "_client", _recording):
        r = await owner_ui.post(f"/modules/{LABELS}/disable", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    assert toast["type"] == "error"
    assert toast["message"] == ("The Label Printing module is still finishing work for your company, such as a store sync "
                                "or a payment. Try again in a minute.")
    assert uses_module((await owner_ui.api.get("/companies/me")).json().get("settings"), LABELS)
    # The turn-off request waits longer than the turn-off itself can.
    assert waits and waits[0] > MODULE_SETTLE_SECONDS, waits

    # Once the work is done, the same click turns it off.
    boundary._RUNNING.pop((cid, LABELS))
    r = await owner_ui.post(f"/modules/{LABELS}/disable", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "HX-Trigger" not in r.headers

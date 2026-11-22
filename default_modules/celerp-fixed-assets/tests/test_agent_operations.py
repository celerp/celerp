# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The assistant makes an asset change the way the screen does: it reviews the change,
proposes it with what the review returned, and the change is made once, when the user
confirms the card."""
from __future__ import annotations

import json

import pytest

from fa_fixtures import FA, _agent_change, _agent_tools

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("tools_open_when_called")]

_MANIFEST = {"name": "celerp-fixed-assets", "api_routes": "celerp_fixed_assets.routes", "first_party": True}


def _tool_results(body: dict) -> list[dict]:
    return [json.loads(m["content"]) for m in body["messages"] if m.get("role") == "tool"]


async def test_the_assistant_reviews_an_asset_then_adds_it_once_the_user_confirms(fa: FA, monkeypatch, drained_jobs):
    from ai_fake_cloud import FakeCloud, answer, install, tool_turn
    from celerp.ai import tools as ai_tools
    from test_helpers import answered_continuation, settled_question

    monkeypatch.setattr(ai_tools, "loaded_modules", lambda: [_MANIFEST])
    await fa.ok(await fa.patch("/settings", {"first_managed_period": "2026-01-01"}))
    asset = {"kind": "contributed", "class_id": (await fa.classes())["Vehicles"]["id"], "name": "Owner's car",
             "available_date": "2026-01-15", "cost": "12000", "credit_account": "3100"}
    change = _agent_change("/assets")[0]["name"]
    review = change + "_preview"
    assert review in _agent_tools()

    def script(identity: str, body: dict) -> dict:
        seen = _tool_results(body)
        if not seen:
            return tool_turn((review, {"body": asset}, "call-review"))
        if len(seen) == 1:
            return tool_turn((change, {"body": {**asset, "fingerprint": seen[0]["data"]["fingerprint"]}}, "call-add"))
        return answer("Done.")

    install(monkeypatch, FakeCloud(script))
    before = len(await fa.entities("fa_asset"))
    conv = (await fa.ok(await fa.client.post("/ai/conversations", headers=fa.h, json={"title": None}), 201))["id"]
    question = "6f1d2c1e-8a51-4c43-9c1e-2f1a3b4c5d6e"
    r = await fa.client.post(f"/ai/conversations/{conv}/query", headers=fa.h,
                             json={"query": "Add the owner's car as an asset", "operation_id": question})
    assert r.status_code == 202, r.text
    state = await settled_question(fa.client, fa.h, conv, question)
    (card,) = state["message"]["pending_actions"]
    assert card["name"] == change and len(await fa.entities("fa_asset")) == before

    async with answered_continuation():
        confirmed = await fa.client.post(f"/ai/conversations/{conv}/confirm", headers=fa.h,
                                         json={"message_id": state["message"]["id"], "tool_call_id": card["id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["ok"] is True, confirmed.json()
    assert len(await fa.entities("fa_asset")) == before + 1

    async with answered_continuation():
        await fa.client.post(f"/ai/conversations/{conv}/confirm", headers=fa.h,
                             json={"message_id": state["message"]["id"], "tool_call_id": card["id"]})
    assert len(await fa.entities("fa_asset")) == before + 1


async def test_a_change_confirmed_without_a_review_is_refused_with_its_reason(fa: FA):
    """The screen always reviews first; a confirm that never did is told to, not taken as stale."""
    class_id = (await fa.classes())["Vehicles"]["id"]
    r = await fa.post("/assets", {"kind": "contributed", "class_id": class_id, "name": "Van", "op_key": "no-review",
                                  "available_date": "2026-01-15", "cost": "9000", "credit_account": "3100"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message_key"] == "fixed_assets.err_review_first"

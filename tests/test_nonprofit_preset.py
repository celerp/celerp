# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The Nonprofit preset and its demo company, and presets applied over each other.

Initial conditions per test: a newly registered company (POST /auth/register) and its admin's
headers. Presets are applied through POST /companies/me/apply-preset, the business type through
POST /companies/me/business-type and the demo records through POST /companies/me/demo/reseed;
what they wrote is read back from GET /companies/me and the item and agreement lists.
"""
from __future__ import annotations

import json
import uuid

import pytest

from celerp.services.vertical_presets import list_presets, load_preset

pytestmark = pytest.mark.asyncio

_NONPROFIT_CATEGORIES = {"membership", "donation", "event_ticket"}


async def _admin(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": f"Club-{uuid.uuid4().hex[:6]}", "email": f"np-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _settings(client, h) -> dict:
    r = await client.get("/companies/me", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["settings"]


def _shape(settings: dict) -> str:
    """What a preset writes, as one comparable text."""
    keys = ("category_schemas", "category_display_names", "gift_categories", "enabled_modules", "units")
    return json.dumps({k: settings.get(k) for k in keys}, sort_keys=True, default=str)


async def test_nonprofit_preset_categories_and_demo(client):
    preset = load_preset("nonprofit")
    assert preset is not None and preset["name"] in {p["name"] for p in list_presets()}
    assert {"celerp-docs", "celerp-contacts", "celerp-accounting", "celerp-subscriptions"} <= set(preset["modules"])
    assert set(preset["categories"]) == _NONPROFIT_CATEGORIES
    h = await _admin(client)
    r = await client.post("/companies/me/apply-preset", headers=h, params={"vertical": "nonprofit"})
    assert r.status_code == 200, r.text
    settings = await _settings(client, h)
    assert _NONPROFIT_CATEGORIES <= set(settings["category_schemas"])
    assert settings.get("gift_categories") == ["donation"]
    assert (await client.post("/companies/me/business-type", headers=h, json={"vertical": "nonprofit"})).status_code == 200
    r = await client.post("/companies/me/demo/reseed", headers=h)
    assert r.status_code == 200 and r.json()["ok"], r.text
    items = (await client.get("/items", headers=h, params={"limit": 200})).json()["items"]
    by_category = {}
    for item in items:
        by_category.setdefault(item.get("category"), []).append(item)
    assert _NONPROFIT_CATEGORIES <= set(by_category), sorted(by_category, key=str)
    assert any((i.get("attributes") or {}).get("membership_term") == "Lifetime" for i in by_category["membership"])
    settings = await _settings(client, h)
    assert any("member" in (term.get("description") or term["name"]).lower() for term in settings["payment_terms"])
    assert any("donation" in tc["text"].lower() or "gift" in tc["text"].lower() for tc in settings["terms_conditions"])
    agreements = (await client.get("/subscriptions", headers=h, params={"kind": "membership"})).json()["items"]
    assert agreements, "the demo company has membership agreements"


async def test_consulting_demo_has_retainers(client):
    h = await _admin(client)
    assert (await client.post("/companies/me/business-type", headers=h, json={"vertical": "consulting"})).status_code == 200
    assert (await client.post("/companies/me/demo/reseed", headers=h)).status_code == 200
    retainers = (await client.get("/subscriptions", headers=h, params={"kind": "retainer"})).json()["items"]
    assert retainers and "celerp-subscriptions" in load_preset("consulting")["modules"]


async def test_preset_switch_additive_idempotent(client):
    """Nonprofit then Consulting, then both again: the second round adds nothing, and the
    categories of both stay."""
    h = await _admin(client)
    shapes = []
    for _round in range(2):
        for vertical in ("nonprofit", "consulting"):
            r = await client.post("/companies/me/apply-preset", headers=h, params={"vertical": vertical})
            assert r.status_code == 200, r.text
            if _round == 1:
                assert r.json()["categories"] == 0 and r.json()["company_settings"] == {}, r.json()
        shapes.append(_shape(await _settings(client, h)))
    assert shapes[0] == shapes[1]
    settings = await _settings(client, h)
    assert _NONPROFIT_CATEGORIES | {"consulting_service"} <= set(settings["category_schemas"])
    assert settings.get("gift_categories") == ["donation"]

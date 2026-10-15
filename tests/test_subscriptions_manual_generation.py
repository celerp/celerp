# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Subscriptions generate their documents when someone chooses Generate Now, never on
their own, and every description of them says so."""
from __future__ import annotations

import pathlib
import uuid

import pytest

from ui.i18n import t

_ROOT = pathlib.Path(__file__).resolve().parents[1]


async def _headers(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "SubManualCo", "email": f"sub-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _invoices(client, h) -> list[dict]:
    r = await client.get("/docs", headers=h, params={"doc_type": "invoice"})
    assert r.status_code == 200, r.text
    body = r.json()
    return [d for d in (body.get("items") if isinstance(body, dict) else body) if d.get("doc_type") == "invoice"]


@pytest.mark.asyncio
async def test_a_due_subscription_generates_nothing_until_generate_now(client):
    h = await _headers(client)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "subscription_invoice", "contact_id": "contact:test-001", "frequency": "monthly",
        "start_date": "2026-01-01",
        "line_items": [{"description": "Support", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}]})
    assert r.status_code in {200, 201}, r.text
    sub_id = r.json()["id"]
    r = await client.post(f"/subscriptions/{sub_id}/activate", headers=h)
    assert r.status_code == 200, r.text
    due = r.json()["next_run_date"]
    assert due < "2026-10-15"

    assert (await client.get("/subscriptions", headers=h)).status_code == 200
    template = (await client.get(f"/docs/{sub_id}", headers=h)).json()
    assert template["next_run_date"] == due and not template.get("generated_doc_ids")
    assert await _invoices(client, h) == []

    r = await client.post(f"/subscriptions/{sub_id}/generate", headers=h)
    assert r.status_code == 200, r.text
    assert [d["id"] for d in await _invoices(client, h)] == [r.json()["doc_id"]]


def test_the_subscriptions_module_runs_nothing_by_itself():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_subs_manifest", _ROOT / "default_modules" / "celerp-subscriptions" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    manifest = mod.PLUGIN_MANIFEST
    assert set(manifest["slots"]) == {"nav", "search_provider"}
    assert "generate" in manifest["description"].lower() and "automatic" not in manifest["description"].lower()


def test_subscription_copy_promises_no_automatic_generation():
    from celerp.main import app

    tag = next(x["description"] for x in app.openapi_tags if x["name"] == "subscriptions")
    readme = (_ROOT / "README.md").read_text()
    for text in (tag, *[ln for ln in readme.splitlines() if "ubscription" in ln and "No subscription" not in ln]):
        assert "automatic" not in text.lower() and "auto-" not in text.lower(), text
        assert "generate" in text.lower(), text


def test_the_list_shows_when_each_subscription_is_next_due():
    from fasthtml.common import to_xml

    from ui.routes.subscriptions import _sub_table

    html = to_xml(_sub_table([{"id": "doc:s1", "ref_id": "SUB-1", "frequency": "monthly",
                               "next_run_date": "2026-02-01", "status": "active"}], "sales"))
    assert f">{t('th.next_due')}<" in html and t("th.next_due") == "Next Due"
    assert "Next Run" not in html and "2026-02-01" in html

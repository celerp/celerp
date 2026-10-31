# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A subscription bills only when someone chooses Generate due, until someone allowed to
turns on automatic billing for it, and every description of it says documents are
generated when they are due."""
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
async def test_a_due_subscription_generates_nothing_until_generate_due(client):
    h = await _headers(client)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "subscription_invoice", "contact_id": "contact:test-001", "frequency": "monthly",
        "start_date": "2026-01-01",
        "line_items": [{"description": "Support", "quantity": 1, "unit_price": 50.0, "line_total": 50.0}]})
    assert r.status_code in {200, 201}, r.text
    sub_id = r.json()["id"]
    r = await client.post(f"/subscriptions/{sub_id}/activate", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["next_bill_date"] == "2026-01-01"

    assert (await client.get("/subscriptions", headers=h)).status_code == 200
    template = (await client.get(f"/docs/{sub_id}", headers=h)).json()
    assert not template.get("periods") and template.get("generation_mode") in (None, "manual_due")
    assert await _invoices(client, h) == []

    r = await client.post(f"/subscriptions/{sub_id}/generate-due", headers=h, json={})
    assert r.status_code == 200, r.text
    assert [d["id"] for d in await _invoices(client, h)] == [r.json()["doc_id"]]


def test_the_subscriptions_module_bills_on_its_own_only_through_the_dispatcher():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_subs_manifest", _ROOT / "default_modules" / "celerp-subscriptions" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    manifest = mod.PLUGIN_MANIFEST
    # Unattended billing runs only as the dispatcher's periodic operation, which bills an
    # agreement only in an automatic mode someone allowed to has turned on.
    assert set(manifest["slots"]) == {"nav", "search_provider", "periodic_operation", "on_modules_ready"}
    assert manifest["slots"]["periodic_operation"]["handler"] == "celerp_subscriptions.billing:run_due"
    assert "billed once" in manifest["description"].lower()


def test_subscription_copy_says_documents_are_generated_when_due():
    from celerp.main import app

    tag = next(x["description"] for x in app.openapi_tags if x["name"] == "subscriptions")
    readme = (_ROOT / "README.md").read_text()
    for text in (tag, *[ln for ln in readme.splitlines() if "ubscription" in ln and "No subscription" not in ln]):
        assert "generate" in text.lower(), text


def test_the_list_shows_when_each_subscription_is_next_due():
    from fasthtml.common import to_xml

    from ui.routes.subscriptions import _sub_table

    html = to_xml(_sub_table([{"id": "doc:s1", "ref_id": "SUB-1", "frequency": "monthly",
                               "next_bill_date": "2026-02-01", "status": "active"}], "sales"))
    assert f">{t('th.next_due')}<" in html and t("th.next_due") == "Next Due"
    assert "Next Run" not in html and "2026-02-01" in html

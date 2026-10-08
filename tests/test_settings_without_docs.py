"""Company settings stay editable on an install where Documents is not running yet.

A packaged install loads only the modules its business type enables, and the
first-run setup sends its currency and timezone before the business type is set.
"""
from __future__ import annotations

import sys
import uuid

import pytest


@pytest.fixture(params=["not installed", "installed but not loaded"])
def docs_not_running(request, monkeypatch):
    """The process state of an install that has not loaded Documents."""
    from celerp.modules import loader
    monkeypatch.setattr(loader, "_loaded", [m for m in loader._loaded if m["name"] != "celerp-docs"])
    if request.param == "not installed":
        for name in [m for m in sys.modules if m == "celerp_docs" or m.startswith("celerp_docs.")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, "celerp_docs", None)


async def _owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "FreshCo", "email": f"nodocs-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_setup_settings_save_without_documents(client, docs_not_running):
    h = await _owner(client)
    r = await client.patch("/companies/me", json={"settings": {"currency": "USD", "timezone": "UTC"}}, headers=h)
    assert r.status_code == 200, r.text
    assert (await client.get("/companies/me", headers=h)).json()["settings"]["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_deposit_account_without_documents_is_refused_unchanged(client, docs_not_running):
    h = await _owner(client)
    r = await client.patch("/companies/me", json={"settings": {"stripe_deposit_account": "1000", "timezone": "UTC"}},
                           headers=h)
    assert r.status_code == 422, r.text
    assert "Documents" in r.text
    settings = (await client.get("/companies/me", headers=h)).json()["settings"]
    assert "stripe_deposit_account" not in settings
    assert settings.get("timezone") != "UTC"

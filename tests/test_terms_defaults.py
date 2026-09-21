# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Terms defaults are deterministic document-creation policy."""
from __future__ import annotations

import uuid

import pytest

from celerp.services.terms import default_terms_for, normalize_terms_templates


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": "Terms Co",
        "email": f"terms-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner",
        "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_doc_create_applies_default_terms_atomically(client):
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={"doc_type": "invoice"})
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=_h(token))).json()
    assert doc["terms_template"] == "Standard Sales Terms"
    assert "seller until paid" in doc["terms_text"]
    assert doc["company_name"] == "Terms Co"


@pytest.mark.asyncio
async def test_explicit_blank_terms_suppress_default(client):
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "terms_text": "",
    })
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=_h(token))).json()
    assert doc.get("terms_text", "") == ""
    assert not doc.get("terms_template")


@pytest.mark.asyncio
async def test_legacy_terms_field_is_explicit_and_suppresses_default(client):
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice", "terms": "Legacy customer terms.",
    })
    assert r.status_code == 200, r.text
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=_h(token))).json()
    assert doc["terms_text"] == "Legacy customer terms."
    assert not doc.get("terms_template")


@pytest.mark.asyncio
async def test_configured_default_is_used(client):
    token = await _register(client)
    h = _h(token)
    patch = await client.patch("/companies/me/terms-conditions", headers=h, json={
        "templates": [{
            "name": "My Invoice Terms",
            "text": "Customer-facing custom terms.",
            "doc_types": ["invoice"],
            "default_for": ["invoice"],
        }],
    })
    assert patch.status_code == 200, patch.text
    created = await client.post("/docs", headers=h, json={"doc_type": "invoice"})
    assert created.status_code == 200, created.text
    doc = (await client.get(f"/docs/{created.json()['id']}", headers=h)).json()
    assert doc["terms_template"] == "My Invoice Terms"
    assert doc["terms_text"] == "Customer-facing custom terms."


def test_legacy_is_default_normalizes_without_mutating_source():
    source = [{"name": "Legacy", "text": "T", "doc_types": ["invoice"], "is_default": True}]
    normalized = normalize_terms_templates(source)
    assert normalized[0]["default_for"] == ["invoice"]
    assert "is_default" not in normalized[0]
    assert source[0]["is_default"] is True
    assert default_terms_for({"terms_conditions": source}, "invoice")["name"] == "Legacy"

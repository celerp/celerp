# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for GET /items/field-values.

Covers:
- Allowlist enforcement (only categorical fields, no FK/blob fields)
- Distinct values returned from item state
- Values surfaced from attributes dict (flattened)
- Sorted output
- Empty/null values excluded
- Tenant isolation (company A's values invisible to company B)
"""

from __future__ import annotations

import uuid
import pytest


async def _reg(client, name: str | None = None) -> str:
    addr = f"{uuid.uuid4().hex[:8]}@fv.test"
    cname = name or f"FVCo-{uuid.uuid4().hex[:6]}"
    r = await client.post(
        "/auth/register",
        json={"company_name": cname, "email": addr, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _add_company(client, bootstrap_tok: str, name: str) -> str:
    """Create a second company using the bootstrap token's /companies endpoint."""
    r = await client.post("/companies", json={"name": name}, headers=_h(bootstrap_tok))
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.asyncio
async def test_field_values_allowed_field_returns_200(client):
    tok = await _reg(client)
    r = await client.get("/items/field-values?field=category", headers=_h(tok))
    assert r.status_code == 200
    data = r.json()
    assert "values" in data
    assert isinstance(data["values"], list)


@pytest.mark.asyncio
async def test_field_values_disallowed_fk_field_returns_400(client):
    """location_id is a FK - must be rejected."""
    tok = await _reg(client)
    r = await client.get("/items/field-values?field=location_id", headers=_h(tok))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_field_values_unknown_field_returns_400(client):
    tok = await _reg(client)
    r = await client.get("/items/field-values?field=__evil__", headers=_h(tok))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_field_values_name_field_rejected(client):
    """name is a free-text blob - not in suggestion allowlist."""
    tok = await _reg(client)
    r = await client.get("/items/field-values?field=name", headers=_h(tok))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_field_values_returns_distinct_values(client):
    tok = await _reg(client)
    h = _h(tok)
    for sku, cat in [("FV-D1", "Gems"), ("FV-D2", "Gems"), ("FV-D3", "Metals")]:
        await client.post("/items", json={"sku": sku, "name": sku, "category": cat, "sell_by": "piece"}, headers=h)
    r = await client.get("/items/field-values?field=category", headers=h)
    assert r.status_code == 200
    values = r.json()["values"]
    assert "Gems" in values
    assert "Metals" in values
    assert len(values) == len(set(values)), "Values must be distinct"


@pytest.mark.asyncio
async def test_field_values_from_attributes(client):
    """Values in the attributes dict must surface via _flatten_item."""
    tok = await _reg(client)
    h = _h(tok)
    await client.post("/items", json={
        "sku": "FV-ATTR-1", "name": "Test Stone", "sell_by": "piece",
        "attributes": {"stone_type": "Sapphire"},
    }, headers=h)
    r = await client.get("/items/field-values?field=stone_type", headers=h)
    assert r.status_code == 200
    assert "Sapphire" in r.json()["values"]


@pytest.mark.asyncio
async def test_field_values_sorted(client):
    tok = await _reg(client)
    h = _h(tok)
    for sku, cat in [("FV-S1", "Zircon"), ("FV-S2", "Amber"), ("FV-S3", "Emerald")]:
        await client.post("/items", json={"sku": sku, "name": sku, "category": cat, "sell_by": "piece"}, headers=h)
    r = await client.get("/items/field-values?field=category", headers=h)
    values = r.json()["values"]
    assert values == sorted(values), "Values must be in alphabetical order"


@pytest.mark.asyncio
async def test_field_values_excludes_empty_strings(client):
    """Items with no category must not contribute empty string to results."""
    tok = await _reg(client)
    h = _h(tok)
    await client.post("/items", json={"sku": "FV-EMPTY", "name": "No Category", "sell_by": "piece"}, headers=h)
    r = await client.get("/items/field-values?field=category", headers=h)
    assert r.status_code == 200
    assert "" not in r.json()["values"]


@pytest.mark.asyncio
async def test_field_values_excludes_null(client):
    """Items with null category must not contribute None to results."""
    tok = await _reg(client)
    h = _h(tok)
    await client.post("/items", json={"sku": "FV-NULL", "name": "Null Cat", "category": None, "sell_by": "piece"}, headers=h)
    r = await client.get("/items/field-values?field=category", headers=h)
    assert None not in r.json()["values"]


@pytest.mark.asyncio
async def test_field_values_empty_when_no_items(client):
    tok = await _reg(client)
    r = await client.get("/items/field-values?field=category", headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["values"] == []


@pytest.mark.asyncio
async def test_field_values_tenant_isolation(client):
    """P5 guard: company A's values must NOT appear in company B's results."""
    tok_a = await _reg(client, name="CompanyA-FV")
    tok_b = await _add_company(client, tok_a, "CompanyB-FV")
    # Company A creates item with unique category
    unique_cat = f"TenantA-Only-{uuid.uuid4().hex[:8]}"
    await client.post("/items", json={"sku": "ISO-A", "name": "A Stone", "category": unique_cat, "sell_by": "piece"}, headers=_h(tok_a))
    # Company B must not see it
    r = await client.get("/items/field-values?field=category", headers=_h(tok_b))
    assert r.status_code == 200
    assert unique_cat not in r.json()["values"], "Tenant isolation violated: A's category visible to B"


@pytest.mark.asyncio
async def test_field_values_all_suggestion_fields_accepted(client):
    """Every field in _SUGGESTION_FIELDS must return 200, not 400."""
    from celerp_inventory.routes import _SUGGESTION_FIELDS
    tok = await _reg(client)
    h = _h(tok)
    for field in _SUGGESTION_FIELDS:
        r = await client.get(f"/items/field-values?field={field}", headers=h)
        assert r.status_code == 200, f"Field '{field}' should be accepted but got {r.status_code}"

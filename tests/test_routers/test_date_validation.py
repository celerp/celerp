# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for date-order validation: issue_date / due_date constraints (backend)
and _parse_dates swap behaviour (unit).

Backend checks live in celerp-docs routes._assert_date_order.
The _parse_dates swap lives in ui.routes.reports._parse_dates.
"""

from __future__ import annotations

import uuid
import pytest
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client) -> str:
    r = await client.post("/auth/register", json={
        "company_name": f"DateVal {uuid.uuid4().hex[:6]}",
        "email": f"dateval-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin",
        "password": "pw",
    })
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Backend validation: _assert_date_order via create_doc
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_doc_rejects_inverted_dates(client):
    """POST /docs with due_date < issue_date must return 422."""
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "contact_name": "Test",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100}],
        "issue_date": "2024-06-15",
        "due_date": "2024-06-01",  # earlier than issue_date
    })
    assert r.status_code == 422
    assert "due_date" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_doc_accepts_equal_dates(client):
    """POST /docs with due_date == issue_date must succeed."""
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "contact_name": "Test",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100}],
        "issue_date": "2024-06-15",
        "due_date": "2024-06-15",
    })
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_create_doc_accepts_no_due_date(client):
    """POST /docs with no due_date must succeed regardless of issue_date."""
    token = await _register(client)
    r = await client.post("/docs", headers=_h(token), json={
        "doc_type": "invoice",
        "contact_name": "Test",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100}],
        "issue_date": "2024-06-15",
    })
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_patch_doc_rejects_due_date_before_issue_date(client):
    """PATCH due_date to before existing issue_date must return 422."""
    token = await _register(client)
    h = _h(token)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_name": "Test",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100}],
        "issue_date": "2024-06-15",
    })
    assert r.status_code == 200
    entity_id = r.json()["id"]

    r2 = await client.patch(f"/docs/{entity_id}", headers=h, json={
        "fields_changed": {"due_date": {"old": None, "new": "2024-06-01"}},
    })
    assert r2.status_code == 422
    assert "due_date" in r2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_doc_rejects_issue_date_after_due_date(client):
    """PATCH issue_date to after existing due_date must return 422."""
    token = await _register(client)
    h = _h(token)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_name": "Test",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100}],
        "issue_date": "2024-06-01",
        "due_date": "2024-06-15",
    })
    assert r.status_code == 200
    entity_id = r.json()["id"]

    r2 = await client.patch(f"/docs/{entity_id}", headers=h, json={
        "fields_changed": {"issue_date": {"old": "2024-06-01", "new": "2024-07-01"}},
    })
    assert r2.status_code == 422
    assert "due_date" in r2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_doc_accepts_valid_due_date(client):
    """PATCH due_date to after issue_date must succeed."""
    token = await _register(client)
    h = _h(token)
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice",
        "contact_name": "Test",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100}],
        "issue_date": "2024-06-01",
    })
    assert r.status_code == 200
    entity_id = r.json()["id"]

    r2 = await client.patch(f"/docs/{entity_id}", headers=h, json={
        "fields_changed": {"due_date": {"old": None, "new": "2024-06-30"}},
    })
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Unit: _parse_dates swaps inverted from/to
# ---------------------------------------------------------------------------

def _make_request(from_: str, to_: str):
    """Minimal fake Request with query_params."""
    return SimpleNamespace(query_params={"from": from_, "to": to_})


def test_parse_dates_swaps_inverted_range():
    from ui.routes.reports import _parse_dates
    req = _make_request("2025-05-01", "2024-05-01")
    date_from, date_to, preset = _parse_dates(req)
    assert date_from == "2024-05-01"
    assert date_to == "2025-05-01"
    assert preset == "custom"


def test_parse_dates_leaves_valid_range_unchanged():
    from ui.routes.reports import _parse_dates
    req = _make_request("2024-01-01", "2024-12-31")
    date_from, date_to, preset = _parse_dates(req)
    assert date_from == "2024-01-01"
    assert date_to == "2024-12-31"


def test_parse_dates_equal_dates_accepted():
    from ui.routes.reports import _parse_dates
    req = _make_request("2024-06-15", "2024-06-15")
    date_from, date_to, preset = _parse_dates(req)
    assert date_from == date_to == "2024-06-15"


def test_parse_dates_missing_to_accepted():
    from ui.routes.reports import _parse_dates
    req = _make_request("2024-06-01", "")
    date_from, date_to, preset = _parse_dates(req)
    assert date_from == "2024-06-01"
    assert date_to == ""


def test_parse_dates_missing_from_accepted():
    from ui.routes.reports import _parse_dates
    req = _make_request("", "2024-06-30")
    date_from, date_to, preset = _parse_dates(req)
    assert date_to == "2024-06-30"
    assert date_from == ""

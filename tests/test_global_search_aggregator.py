# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Aggregated /search endpoint: authorization, degradation, and input guards.

The aggregator enumerates search_provider slots at request time, gates each on
its declared permission, and runs the survivors sequentially on one session.
These tests register controllable providers so each failure branch (resolution
failure, invocation failure with rollback, rollback-itself-fails, permission
denial, mid-run disconnect, short and over-long queries, unknown permission
key) is exercised in isolation from the real modules.
"""
from __future__ import annotations

import pytest

from celerp.modules import slots
from celerp.services.auth import get_token_claims
from test_helpers import register_admin, grant_permission

pytestmark = pytest.mark.asyncio


async def _set_enabled_modules(session, company_id, value):
    """Set (or, with value is _ABSENT, remove) the company's enabled_modules key.

    Written through the request-shared session so the aggregator's fresh
    get_current_company_settings read sees exactly this within the test's
    savepoint-joined transaction.
    """
    from celerp.models.company import Company
    company = await session.get(Company, company_id)
    settings = dict(company.settings or {})
    if value is _ABSENT:
        settings.pop("enabled_modules", None)
    else:
        settings["enabled_modules"] = value
    company.settings = settings
    await session.flush()


_ABSENT = object()


# ── Controllable provider handlers ────────────────────────────────────────────
# Registered under search_provider slots by the fixtures below. Each returns the
# provider result shape {result_key: [...]} or fails in a specific way.

_invocation_calls: list[str] = []


async def ok_items_provider(session, company_id, role, q, limit):
    return {"items": [{"id": "prov-a-1", "name": f"hit-{q}"}]}


async def ok_entries_provider(session, company_id, role, q, limit):
    return {"entries": [{"id": "prov-b-1"}]}


async def raising_provider(session, company_id, role, q, limit):
    _invocation_calls.append("raising")
    raise RuntimeError("provider blew up")


async def over_limit_provider(session, company_id, role, q, limit):
    # Returns more than the per-provider cap; the aggregator must truncate.
    return {"items": [{"id": f"x-{i}"} for i in range(20)]}


async def non_list_provider(session, company_id, role, q, limit):
    return {"items": "not-a-list"}


async def never_called_provider(session, company_id, role, q, limit):
    _invocation_calls.append("never")
    return {"items": [{"id": "should-not-run"}]}


async def slow_provider(session, company_id, role, q, limit):
    # Hangs past the (test-shortened) provider timeout without ever raising, so
    # the aggregator's own timeout is what must contain it.
    import asyncio
    _invocation_calls.append("slow")
    await asyncio.sleep(1.0)
    return {"items": [{"id": "slow-1"}]}


@pytest.fixture(autouse=True)
def _clear_invocation_log():
    _invocation_calls.clear()
    yield
    _invocation_calls.clear()


@pytest.fixture
def register_provider():
    """Register controllable search_provider contributions in isolation.

    Module load appends providers to the process-global slot registry without
    clearing it, so real first-party providers may already be present when this
    test runs. Each aggregator test asserts over exactly the providers it adds,
    so the fixture starts from an empty search_provider list and restores the
    prior contents at teardown, leaving every other slot untouched.
    """
    saved = list(slots.get("search_provider"))
    slots._slots["search_provider"] = []

    def _add(module: str, handler: str, result_key: str, permission: str):
        contrib = {
            "handler": handler,
            "result_key": result_key,
            "permission": permission,
            "_module": module,
        }
        slots.register("search_provider", contrib)
        return contrib

    yield _add

    slots._slots["search_provider"] = saved


_THIS = __name__


async def _owner_headers(client):
    tok = await register_admin(client)
    return {"Authorization": f"Bearer {tok}"}


# ── Happy path and truncation ─────────────────────────────────────────────────

async def test_aggregates_permitted_providers(client, register_provider):
    register_provider("test-alpha", f"{_THIS}:ok_items_provider", "items", "view_inventory")
    register_provider("test-beta", f"{_THIS}:ok_entries_provider", "entries", "view_financial_reports")
    headers = await _owner_headers(client)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded_modules"] == []
    assert body["results"]["test-alpha"] == {"items": [{"id": "prov-a-1", "name": "hit-widget"}]}
    assert body["results"]["test-beta"] == {"entries": [{"id": "prov-b-1"}]}


async def test_truncates_to_five_per_provider(client, register_provider):
    register_provider("test-alpha", f"{_THIS}:over_limit_provider", "items", "view_inventory")
    headers = await _owner_headers(client)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]["test-alpha"]["items"]) == 5


# ── Permission gating ─────────────────────────────────────────────────────────

async def test_permission_denied_module_omitted_not_degraded(client, session, register_provider):
    register_provider("test-alpha", f"{_THIS}:never_called_provider", "items", "view_inventory")
    headers = await _owner_headers(client)
    # Revoke view_inventory from everyone below owner AND owner: set floor so only
    # a role above owner holds it, which no one is, denying the owner too.
    await grant_permission(client, headers, "view_inventory", "owner")
    # Now flip the owner cell off directly so the owner is denied.
    await client.patch(
        "/companies/me/role-permissions",
        json={"perm_key": "view_inventory", "role_key": "owner", "granted": False},
        headers=headers,
    )

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-alpha" not in body["results"]
    assert "test-alpha" not in body["degraded_modules"]
    assert "never" not in _invocation_calls


async def test_unknown_permission_key_fails_closed(client, register_provider):
    # An unknown permission key raises KeyError from the registry lookup; the
    # provider must be omitted (not degraded) and never invoked.
    register_provider("test-alpha", f"{_THIS}:never_called_provider", "items", "not_a_real_permission")
    register_provider("test-beta", f"{_THIS}:ok_entries_provider", "entries", "view_financial_reports")
    headers = await _owner_headers(client)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-alpha" not in body["results"]
    assert "test-alpha" not in body["degraded_modules"]
    assert "never" not in _invocation_calls
    # A sibling with a good key still runs.
    assert body["results"]["test-beta"] == {"entries": [{"id": "prov-b-1"}]}


# ── Failure branches ──────────────────────────────────────────────────────────

async def test_resolution_failure_degrades_without_rollback(client, session, register_provider, monkeypatch):
    register_provider("test-alpha", f"{_THIS}:does_not_exist", "items", "view_inventory")
    register_provider("test-beta", f"{_THIS}:ok_entries_provider", "entries", "view_financial_reports")
    headers = await _owner_headers(client)

    rolled_back = []
    orig_rollback = session.rollback

    async def _spy_rollback():
        rolled_back.append(True)
        return await orig_rollback()

    monkeypatch.setattr(session, "rollback", _spy_rollback)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-alpha" in body["degraded_modules"]
    assert "test-alpha" not in body["results"]
    # Resolution fails before any DB work, so no rollback is issued for it.
    assert rolled_back == []
    # A later provider still runs.
    assert body["results"]["test-beta"] == {"entries": [{"id": "prov-b-1"}]}


async def test_invocation_failure_degrades_and_rolls_back(client, session, register_provider, monkeypatch):
    register_provider("test-alpha", f"{_THIS}:raising_provider", "items", "view_inventory")
    register_provider("test-beta", f"{_THIS}:ok_entries_provider", "entries", "view_financial_reports")
    headers = await _owner_headers(client)

    rolled_back = []
    orig_rollback = session.rollback

    async def _spy_rollback():
        rolled_back.append(True)
        return await orig_rollback()

    monkeypatch.setattr(session, "rollback", _spy_rollback)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-alpha" in body["degraded_modules"]
    assert "test-alpha" not in body["results"]
    assert rolled_back == [True]
    # The session recovered by rollback, so the next provider still runs cleanly.
    assert body["results"]["test-beta"] == {"entries": [{"id": "prov-b-1"}]}


async def test_non_list_result_key_degrades(client, register_provider):
    register_provider("test-alpha", f"{_THIS}:non_list_provider", "items", "view_inventory")
    headers = await _owner_headers(client)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-alpha" in body["degraded_modules"]
    assert "test-alpha" not in body["results"]


async def test_rollback_itself_fails_stops_and_degrades_rest(client, session, register_provider, monkeypatch):
    # test-alpha raises on invocation; the rollback that follows also fails, so
    # the session is unusable and every remaining provider is degraded without
    # being invoked.
    register_provider("test-alpha", f"{_THIS}:raising_provider", "items", "view_inventory")
    register_provider("test-omega", f"{_THIS}:never_called_provider", "items", "view_inventory")
    headers = await _owner_headers(client)

    async def _boom_rollback():
        raise RuntimeError("rollback failed")

    monkeypatch.setattr(session, "rollback", _boom_rollback)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-alpha" in body["degraded_modules"]
    assert "test-omega" in body["degraded_modules"]
    assert body["results"] == {}
    # The provider after the failed rollback is degraded, not run.
    assert "never" not in _invocation_calls


async def test_disconnect_mid_run_stops_remaining(client, register_provider, monkeypatch):
    from starlette.requests import Request

    register_provider("test-alpha", f"{_THIS}:never_called_provider", "items", "view_inventory")
    headers = await _owner_headers(client)

    async def _always_disconnected(self):
        return True

    monkeypatch.setattr(Request, "is_disconnected", _always_disconnected)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # Disconnected before running any provider: none ran, none degraded.
    assert body["results"] == {}
    assert body["degraded_modules"] == []
    assert "never" not in _invocation_calls


# ── Input guards ──────────────────────────────────────────────────────────────

async def test_short_query_returns_empty_without_invoking(client, register_provider):
    register_provider("test-alpha", f"{_THIS}:never_called_provider", "items", "view_inventory")
    headers = await _owner_headers(client)

    r = await client.get("/search", params={"q": "a"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"results": {}, "degraded_modules": []}
    assert "never" not in _invocation_calls


async def test_blank_query_returns_empty(client, register_provider):
    register_provider("test-alpha", f"{_THIS}:never_called_provider", "items", "view_inventory")
    headers = await _owner_headers(client)

    r = await client.get("/search", params={"q": "   "}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"results": {}, "degraded_modules": []}
    assert "never" not in _invocation_calls


async def test_over_long_query_rejected_without_invoking(client, register_provider):
    # An over-long stripped query is invalid input, not an empty search: the
    # aggregator answers 422 before waking any provider, so the UI can render a
    # real error instead of the no-results state.
    register_provider("test-alpha", f"{_THIS}:never_called_provider", "items", "view_inventory")
    headers = await _owner_headers(client)

    r = await client.get("/search", params={"q": "x" * 201}, headers=headers)
    assert r.status_code == 422, r.text
    assert "never" not in _invocation_calls


# ── Provider timeout ──────────────────────────────────────────────────────────

import celerp.routers.search as _search_router


async def test_slow_provider_times_out_and_later_provider_runs(client, register_provider, monkeypatch):
    # A provider that hangs past the provider timeout is degraded; the aggregate
    # request still completes and a normal provider after it still runs.
    monkeypatch.setattr(_search_router, "_PROVIDER_TIMEOUT_SECONDS", 0.1)
    register_provider("test-slow", f"{_THIS}:slow_provider", "items", "view_inventory")
    register_provider("test-beta", f"{_THIS}:ok_entries_provider", "entries", "view_financial_reports")
    headers = await _owner_headers(client)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-slow" in body["degraded_modules"]
    assert "test-slow" not in body["results"]
    assert body["results"]["test-beta"] == {"entries": [{"id": "prov-b-1"}]}
    assert "slow" in _invocation_calls


async def test_timeout_rolls_session_back(client, session, register_provider, monkeypatch):
    # A provider timeout follows the same failure path as any provider failure:
    # the session is rolled back before the next provider runs.
    monkeypatch.setattr(_search_router, "_PROVIDER_TIMEOUT_SECONDS", 0.1)
    register_provider("test-slow", f"{_THIS}:slow_provider", "items", "view_inventory")
    register_provider("test-beta", f"{_THIS}:ok_entries_provider", "entries", "view_financial_reports")
    headers = await _owner_headers(client)

    rolled_back = []
    orig_rollback = session.rollback

    async def _spy_rollback():
        rolled_back.append(True)
        return await orig_rollback()

    monkeypatch.setattr(session, "rollback", _spy_rollback)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-slow" in body["degraded_modules"]
    assert rolled_back == [True]
    assert body["results"]["test-beta"] == {"entries": [{"id": "prov-b-1"}]}


async def test_timeout_then_rollback_failure_degrades_rest(client, session, register_provider, monkeypatch):
    # The slow provider times out; the rollback that follows also fails, so the
    # session is unusable and every remaining provider is degraded without running.
    monkeypatch.setattr(_search_router, "_PROVIDER_TIMEOUT_SECONDS", 0.1)
    register_provider("test-slow", f"{_THIS}:slow_provider", "items", "view_inventory")
    register_provider("test-omega", f"{_THIS}:never_called_provider", "items", "view_inventory")
    headers = await _owner_headers(client)

    async def _boom_rollback():
        raise RuntimeError("rollback failed")

    monkeypatch.setattr(session, "rollback", _boom_rollback)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-slow" in body["degraded_modules"]
    assert "test-omega" in body["degraded_modules"]
    assert body["results"] == {}
    assert "never" not in _invocation_calls


# ── Per-company module enablement ─────────────────────────────────────────────

async def test_company_disabled_module_omitted_while_slot_registered(client, session, register_provider):
    # The slot stays registered process-wide, but the company's enabled_modules
    # list excludes it: the provider must not run, and must be absent from BOTH
    # results and degraded_modules (it is disabled, not broken).
    tok = await register_admin(client)
    headers = {"Authorization": f"Bearer {tok}"}
    company_id = get_token_claims(tok)["company_id"]
    register_provider("test-alpha", f"{_THIS}:never_called_provider", "items", "view_inventory")
    register_provider("test-beta", f"{_THIS}:ok_entries_provider", "entries", "view_financial_reports")
    await _set_enabled_modules(session, company_id, ["test-beta"])

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "test-alpha" not in body["results"]
    assert "test-alpha" not in body["degraded_modules"]
    assert "never" not in _invocation_calls
    # The enabled sibling still runs.
    assert body["results"]["test-beta"] == {"entries": [{"id": "prov-b-1"}]}


async def test_company_enabled_module_runs(client, session, register_provider):
    # Adding the module to the company's enabled list lets its provider run.
    tok = await register_admin(client)
    headers = {"Authorization": f"Bearer {tok}"}
    company_id = get_token_claims(tok)["company_id"]
    register_provider("test-alpha", f"{_THIS}:ok_items_provider", "items", "view_inventory")
    await _set_enabled_modules(session, company_id, ["test-alpha"])

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["results"]["test-alpha"] == {"items": [{"id": "prov-a-1", "name": "hit-widget"}]}


async def test_malformed_enabled_modules_fails_closed(client, session, register_provider):
    # enabled_modules present but not a list: get_enabled() returns empty and the
    # key IS present, so search shows nothing rather than everything.
    tok = await register_admin(client)
    headers = {"Authorization": f"Bearer {tok}"}
    company_id = get_token_claims(tok)["company_id"]
    register_provider("test-alpha", f"{_THIS}:never_called_provider", "items", "view_inventory")
    await _set_enabled_modules(session, company_id, "not-a-list")

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"] == {}
    assert body["degraded_modules"] == []
    assert "never" not in _invocation_calls


async def test_absent_enabled_key_runs_all(client, session, register_provider):
    # Legacy fallback: with no enabled_modules key at all, every permitted
    # provider runs (a company that predates per-module enablement).
    tok = await register_admin(client)
    headers = {"Authorization": f"Bearer {tok}"}
    company_id = get_token_claims(tok)["company_id"]
    register_provider("test-alpha", f"{_THIS}:ok_items_provider", "items", "view_inventory")
    await _set_enabled_modules(session, company_id, _ABSENT)

    r = await client.get("/search", params={"q": "widget"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["results"]["test-alpha"] == {"items": [{"id": "prov-a-1", "name": "hit-widget"}]}


async def test_requires_authentication(client, register_provider):
    register_provider("test-alpha", f"{_THIS}:ok_items_provider", "items", "view_inventory")
    r = await client.get("/search", params={"q": "widget"})
    assert r.status_code in (401, 403), r.text

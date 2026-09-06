# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""First-party search providers, exercised through the real aggregator.

Each module's global_search is registered under its real dotted handler and
reached through GET /search, so the provider contract (result_key, read-only
call signature, per-module permission gating) is verified against the running
system with seeded data rather than in isolation.
"""
from __future__ import annotations

import pytest

from celerp.modules import slots
from test_helpers import register_admin

pytestmark = pytest.mark.asyncio


# The six first-party providers, each (module, dotted handler, result_key, permission).
_PROVIDERS = [
    ("celerp-inventory", "celerp_inventory.search:global_search", "items", "view_inventory"),
    ("celerp-contacts", "celerp_contacts.search:global_search", "items", "view_contacts"),
    ("celerp-docs", "celerp_docs.search:global_search", "items", "view_documents"),
    ("celerp-manufacturing", "celerp_manufacturing.search:global_search", "items", "manage_manufacturing"),
    ("celerp-subscriptions", "celerp_subscriptions.search:global_search", "items", "view_subscriptions"),
    ("celerp-accounting", "celerp_accounting.search:global_search", "entries", "view_financial_reports"),
]


@pytest.fixture
def real_providers():
    """Install exactly the six real first-party providers, then restore.

    Module load appends providers without clearing, so the registry may already
    hold these or a subset when the test runs. The fixture sets the slot to
    exactly the six declared providers (no duplicates, no leftovers) and restores
    the prior contents at teardown."""
    saved = list(slots.get("search_provider"))
    slots._slots["search_provider"] = [
        {"handler": handler, "result_key": result_key, "permission": permission, "_module": module}
        for module, handler, result_key, permission in _PROVIDERS
    ]
    yield
    slots._slots["search_provider"] = saved


def _company_id_from_token(token: str) -> str:
    from celerp.services.auth import get_token_claims
    return get_token_claims(token)["company_id"]


async def _owner_headers(client):
    tok = await register_admin(client)
    return {"Authorization": f"Bearer {tok}"}


async def test_all_providers_reachable_and_shaped(client, real_providers):
    """Owner holds every permission, so all six providers run and each returns its
    declared result_key as a list."""
    headers = await _owner_headers(client)
    r = await client.get("/search", params={"q": "zz"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded_modules"] == [], body
    for module, _handler, result_key, _perm in _PROVIDERS:
        assert module in body["results"], (module, body)
        assert result_key in body["results"][module]
        assert isinstance(body["results"][module][result_key], list)


async def test_contact_provider_returns_seeded_match(client, session, real_providers):
    # The contacts module API route is not mounted in the unit app (modules are
    # not loaded), so seed the contact projection the provider reads directly, in
    # the same company the owner token carries.
    import datetime as _dt
    from celerp.models.projections import Projection
    tok = await register_admin(client)
    headers = {"Authorization": f"Bearer {tok}"}
    company_id = _company_id_from_token(tok)
    now = _dt.datetime.now(_dt.timezone.utc)
    session.add(Projection(
        company_id=company_id,
        entity_id="contact:zephyr-1",
        entity_type="contact",
        state={"name": "Zephyr Trading Co", "email": "z@example.test"},
        version=1,
        updated_at=now,
    ))
    await session.flush()

    r = await client.get("/search", params={"q": "Zephyr"}, headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["results"]["celerp-contacts"]["items"]
    assert any("Zephyr" in (it.get("name") or "") for it in items), items


async def test_inventory_provider_returns_seeded_match(client, session, real_providers):
    from test_helpers import default_location_id, create_item
    headers = await _owner_headers(client)
    loc = await default_location_id(client, headers)
    await create_item(client, headers, loc, sku="ZED-SEARCH-1")

    r = await client.get("/search", params={"q": "ZED-SEARCH-1"}, headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["results"]["celerp-inventory"]["items"]
    assert any(it.get("sku") == "ZED-SEARCH-1" for it in items), items
    # The inventory provider attaches q_match reasons, like the list route.
    hit = next(it for it in items if it.get("sku") == "ZED-SEARCH-1")
    assert "q_match" in hit


async def test_provider_permission_gates_per_module(client, session, real_providers):
    """A viewer denied a module's permission does not see that module in results,
    while a module the viewer may see still appears."""
    from test_helpers import invite_user
    admin_headers = await _owner_headers(client)

    # Grant view_contacts to viewer; leave manage_manufacturing manager-floored
    # (a viewer cannot hold it), so contacts appear for a viewer and manufacturing
    # does not.
    await client.patch(
        "/companies/me/role-permissions",
        json={"perm_key": "view_contacts", "role_key": "viewer", "granted": True},
        headers=admin_headers,
    )

    viewer_tok = await invite_user(client, session, admin_headers, "viewer@ex.test", "viewer")
    viewer_headers = {"Authorization": f"Bearer {viewer_tok}"}

    r = await client.get("/search", params={"q": "zz"}, headers=viewer_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "celerp-contacts" in body["results"], body
    assert "celerp-manufacturing" not in body["results"], body
    assert body["degraded_modules"] == [], body


async def test_disabled_module_omitted_not_degraded(client, real_providers):
    """A provider whose slot is absent (module disabled) simply does not appear;
    it is never listed as degraded. Removing one provider models the disabled case."""
    headers = await _owner_headers(client)
    slots._slots["search_provider"] = [
        c for c in slots.get("search_provider") if c.get("_module") != "celerp-manufacturing"
    ]
    r = await client.get("/search", params={"q": "zz"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "celerp-manufacturing" not in body["results"]
    assert "celerp-manufacturing" not in body["degraded_modules"]

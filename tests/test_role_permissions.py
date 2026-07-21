# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Dynamic role-permission registry and resolver tests.

Covers:
- ROLES derived from auth.ROLE_LEVELS with i18n label keys
- PERMISSIONS catalogue: keys, defaults, floors, fixed rows
- Resolver: defaults, overrides, stale overrides, fail-closed roles
- assert_role_permission 403 shape
- Threshold monotonicity
"""
from __future__ import annotations

import pytest
import pytest_asyncio


# ── Registry shape ────────────────────────────────────────────────────────────

def test_roles_derived_from_role_levels():
    """ROLES keys and order match auth.ROLE_LEVELS; each carries a label_key."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import ROLES

    assert [r.key for r in ROLES] == sorted(ROLE_LEVELS, key=ROLE_LEVELS.get)
    for r in ROLES:
        assert r.level == ROLE_LEVELS[r.key]
        assert r.label_key == f"settings.{r.key}"


# The full 2.1 catalogue: key -> (default_min_role, grantable, floor_role).
# Defaults reproduce the thresholds the enforcement sites carry today.
EXPECTED_CATALOGUE = {
    # viewer defaults (view keys floor at viewer)
    "view_dashboards": ("viewer", True, "viewer"),
    "view_documents": ("viewer", True, "viewer"),
    "view_contacts": ("viewer", True, "viewer"),
    "view_inventory": ("viewer", True, "viewer"),
    # operator defaults (write-capable keys floor at operator)
    "edit_documents": ("operator", True, "operator"),
    "edit_contacts": ("operator", True, "operator"),
    "edit_inventory": ("operator", True, "operator"),
    "finalize_documents": ("operator", True, "operator"),
    "fulfill_documents": ("operator", True, "operator"),
    "record_payments": ("operator", True, "operator"),
    "set_sales_doc_prices": ("operator", True, "operator"),
    "manage_labels": ("operator", True, "operator"),
    "manage_manufacturing": ("operator", True, "operator"),
    "use_ai_assistant": ("operator", True, "operator"),
    "run_backups": ("operator", True, "operator"),
    "view_subscriptions": ("operator", True, "operator"),
    # manager defaults
    "set_inventory_prices": ("manager", True, "operator"),
    "delete_documents": ("manager", True, "operator"),
    "adjust_inventory": ("manager", True, "operator"),
    "import_export_data": ("manager", True, "operator"),
    "view_payments": ("manager", True, "operator"),
    "view_financial_reports": ("manager", True, "operator"),
    "manage_accounting": ("manager", True, "operator"),
    "manage_module_settings": ("manager", True, "operator"),
    # admin defaults
    "manage_users": ("admin", True, "operator"),
    "manage_company_settings": ("admin", True, "operator"),
    "manage_integrations": ("admin", True, "operator"),
    # fixed rows: owner-only, never grantable
    "manage_permissions": ("owner", False, "owner"),
    "manage_company_lifecycle": ("owner", False, "owner"),
    "manage_billing": ("owner", False, "owner"),
}


def test_permissions_registry_shape():
    """The registry holds exactly the catalogue keys with their defaults and floors."""
    from celerp.services.permissions import PERMISSIONS

    by_key = {p.key: p for p in PERMISSIONS}
    assert set(by_key) == set(EXPECTED_CATALOGUE)
    for key, (default, grantable, floor) in EXPECTED_CATALOGUE.items():
        p = by_key[key]
        assert p.default_min_role == default, key
        assert p.grantable is grantable, key
        assert p.floor_role == floor, key
        assert p.label, key


def test_fixed_rows_not_grantable():
    """manage_permissions, manage_company_lifecycle, manage_billing are fixed."""
    from celerp.services.permissions import PERMISSIONS

    fixed = {p.key for p in PERMISSIONS if not p.grantable}
    assert fixed == {"manage_permissions", "manage_company_lifecycle", "manage_billing"}


# ── Resolver ──────────────────────────────────────────────────────────────────

def test_permission_min_level_default():
    """Empty overrides resolve to the registry default level."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import permission_min_level

    assert permission_min_level({}, "set_inventory_prices") == ROLE_LEVELS["manager"]
    assert permission_min_level({}, "set_sales_doc_prices") == ROLE_LEVELS["operator"]
    assert permission_min_level({}, "view_inventory") == ROLE_LEVELS["viewer"]


def test_permission_min_level_override():
    """An override {perm_key: role_key} changes the resolved level."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import permission_min_level

    settings = {"role_permissions": {"set_inventory_prices": "operator"}}
    assert permission_min_level(settings, "set_inventory_prices") == ROLE_LEVELS["operator"]
    settings = {"role_permissions": {"set_sales_doc_prices": "manager"}}
    assert permission_min_level(settings, "set_sales_doc_prices") == ROLE_LEVELS["manager"]


def test_permission_min_level_stale_override_ignored():
    """Overrides naming unknown roles or permissions fall back to the default."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import permission_min_level

    settings = {"role_permissions": {"set_inventory_prices": "archduke"}}
    assert permission_min_level(settings, "set_inventory_prices") == ROLE_LEVELS["manager"]
    settings = {"role_permissions": {"no_such_permission": "operator"}}
    assert permission_min_level(settings, "set_inventory_prices") == ROLE_LEVELS["manager"]


def test_permission_min_level_fixed_rows_ignore_overrides():
    """Fixed rows resolve at owner even when the blob carries an override."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import permission_min_level

    settings = {"role_permissions": {"manage_permissions": "viewer"}}
    assert permission_min_level(settings, "manage_permissions") == ROLE_LEVELS["owner"]


def test_role_has_permission_unknown_role_fails_closed():
    """An unmapped role resolves to level 0 and fails closed.

    The resolver receives already-migrated roles from get_current_role;
    legacy-alias migration itself stays covered by TestLegacyRoleMigration
    in tests/test_permissions.py.
    """
    from celerp.services.permissions import role_has_permission

    assert role_has_permission({}, "salesperson", "set_sales_doc_prices") is False
    assert role_has_permission({}, "operator", "set_sales_doc_prices") is True


def test_assert_role_permission_raises_403():
    """Denied caller gets a 403 whose message names the permission."""
    from fastapi import HTTPException
    from celerp.services.permissions import assert_role_permission

    with pytest.raises(HTTPException) as exc:
        assert_role_permission({}, "operator", "set_inventory_prices")
    assert exc.value.status_code == 403
    assert "set_inventory_prices" in exc.value.detail
    # A granted caller passes silently.
    assert_role_permission({}, "manager", "set_inventory_prices")


def test_threshold_monotonic():
    """Granting a lower role implies every higher role passes."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import role_has_permission

    settings = {"role_permissions": {"set_inventory_prices": "operator"}}
    for role, level in ROLE_LEVELS.items():
        expected = level >= ROLE_LEVELS["operator"]
        assert role_has_permission(settings, role, "set_inventory_prices") is expected, role


def test_defaults_match_current_behavior():
    """With empty overrides every registry entry resolves to the minimum role
    its enforcement sites carry today; the old static table's rows are a subset."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import PERMISSIONS, permission_min_level

    for p in PERMISSIONS:
        expected_role, _, _ = EXPECTED_CATALOGUE[p.key]
        assert permission_min_level({}, p.key) == ROLE_LEVELS[expected_role], p.key


# ── Gate 1: set_inventory_prices (cost visibility and price writes) ───────────

from test_helpers import grant_permission, perm_setup  # noqa: E402

_GRANTED = {"role_permissions": {"set_inventory_prices": "operator"}}


async def test_operator_granted_sees_cost_fields(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.get("/items", headers=ctx["operator_h"])
    assert r.status_code == 200
    items = r.json()["items"]
    target = next(i for i in items if i["id"] == ctx["item_id"])
    assert "cost_price" in target or "cost_total" in target


async def test_operator_granted_item_schema_costs(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.get("/companies/me/item-schema", headers=ctx["operator_h"])
    assert r.status_code == 200
    keys = {f.get("key") for f in r.json()}
    assert "cost_price" in keys


async def test_operator_granted_sets_price(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.post(
        f"/items/{ctx['item_id']}/price",
        json={"price_type": "retail_price", "new_price": 150.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def test_operator_ungranted_price_403(client, session):
    ctx = await perm_setup(client, session)
    r = await client.post(
        f"/items/{ctx['item_id']}/price",
        json={"price_type": "retail_price", "new_price": 150.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 403
    assert "set_inventory_prices" in r.json()["detail"]


async def test_operator_granted_create_with_costs(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.post(
        "/items",
        json={"sku": "OP-COST", "name": "Op Cost Item", "quantity": 1,
              "location_id": ctx["location_id"], "cost_price": 42.0, "sell_by": "piece"},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def test_operator_granted_patch_with_costs(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.patch(
        f"/items/{ctx['item_id']}",
        json={"fields_changed": {"cost_price": {"old": 100.0, "new": 80.0}}},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def test_manager_default_unchanged(client, session):
    """Confirmatory: with no overrides, manager keeps cost visibility and price
    writes, operator keeps neither."""
    ctx = await perm_setup(client, session)

    r = await client.get("/items", headers=ctx["manager_h"])
    target = next(i for i in r.json()["items"] if i["id"] == ctx["item_id"])
    assert "cost_price" in target or "cost_total" in target

    r = await client.post(
        f"/items/{ctx['item_id']}/price",
        json={"price_type": "retail_price", "new_price": 175.0},
        headers=ctx["manager_h"],
    )
    assert r.status_code == 200, r.text

    r = await client.get("/items", headers=ctx["operator_h"])
    target = next(i for i in r.json()["items"] if i["id"] == ctx["item_id"])
    assert "cost_price" not in target and "cost_total" not in target


# ── Activity redaction follows the permission ─────────────────────────────────

def test_can_see_costs_uses_permission():
    from celerp.services.activity_redaction import can_see_costs

    assert can_see_costs(_GRANTED, "operator") is True
    assert can_see_costs({}, "operator") is False
    assert can_see_costs({}, "manager") is True
    assert can_see_costs({}, None) is False


def test_redact_entries_granted_operator():
    from celerp.services.activity_redaction import redact_entries_for_role

    entries = [{"event_type": "item.created", "data": {"cost_total": 100.0, "name": "X"}}]
    out = redact_entries_for_role(entries, _GRANTED, "operator")
    assert out[0]["data"]["cost_total"] == 100.0
    out2 = redact_entries_for_role(entries, {}, "operator")
    assert "cost_total" not in out2[0]["data"]


async def test_ledger_route_passes_overrides(client, session):
    """The ledger routes resolve cost visibility from company settings."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")

    def _cost_pricing(entries):
        return [e for e in entries
                if e.get("event_type") == "item.pricing.set"
                and (e.get("data") or {}).get("price_type") == "cost_price"]

    r = await client.get("/ledger", params={"entity_id": ctx["item_id"]}, headers=ctx["operator_h"])
    assert r.status_code == 200
    items = r.json()["items"]
    cost_events = _cost_pricing(items)
    assert cost_events, "item was created with a cost price"
    # Granted: the cost amount is present and not redacted.
    assert all("new_price" in (e.get("data") or {}) for e in cost_events)
    assert not any((e.get("data") or {}).get("cost_redacted") for e in cost_events)

    entry_id = cost_events[0]["id"]
    r2 = await client.get(f"/ledger/{entry_id}", headers=ctx["operator_h"])
    assert r2.status_code == 200
    assert "new_price" in (r2.json().get("data") or {})


async def test_dashboard_activity_granted_operator(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")

    r = await client.get("/dashboard/activity", headers=ctx["operator_h"])
    assert r.status_code == 200
    acts = r.json()["activities"]
    assert any(any(k in (a.get("data") or {}) for k in ("cost_price", "cost_total"))
               for a in acts), "granted operator should see unredacted activity costs"


# ── Dashboard UI follows the permission ───────────────────────────────────────

@pytest_asyncio.fixture
async def ui():
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app),
                           base_url="http://ui", follow_redirects=False) as c:
        yield c


def _dashboard_patches(settings: dict, vertical: str):
    from unittest.mock import AsyncMock, patch

    company = {"name": "Perm Co", "vertical": vertical,
               "currency": "THB", "settings": settings}
    valuation = {"retail_total": 1000.0, "cost_total": 400.0, "active_item_count": 3}
    return [
        patch("ui.api_client.get_company", AsyncMock(return_value=company)),
        patch("ui.api_client.get_valuation", AsyncMock(return_value=valuation)),
        patch("ui.api_client.get_doc_summary", AsyncMock(return_value={})),
        patch("ui.api_client.get_dashboard_kpis", AsyncMock(return_value={})),
        patch("ui.api_client.my_companies", AsyncMock(return_value={"items": [company], "total": 1})),
        patch("ui.api_client.get_ar_aging", AsyncMock(return_value={"buckets": {}})),
        patch("ui.api_client.get_activity", AsyncMock(return_value=[])),
    ]


async def _render_dashboard(ui, role: str, settings: dict, vertical: str) -> bytes:
    from contextlib import ExitStack

    from test_helpers import authed_cookies

    with ExitStack() as stack:
        for p in _dashboard_patches(settings, vertical):
            stack.enter_context(p)
        r = await ui.get("/dashboard", cookies=authed_cookies(role=role))
    assert r.status_code == 200
    return r.content


async def test_margin_redaction_follows_permission(ui):
    """The margin sub-text renders exactly when set_inventory_prices is held.

    Uses the coins vertical, whose operator-visible Stock Value (Retail) card
    carries the margin sub-label, so the assertion isolates the value strip
    from KPI-card filtering."""
    # The mocked valuation (retail 1000, cost 400) yields a 60.0% margin sub-label;
    # match that exact text so the CSS "margin:" declarations never register a hit.
    margin_label = b"margin: 60.0%"
    vertical = "coins_precious_metals"
    content = await _render_dashboard(ui, "operator", {}, vertical)
    assert margin_label not in content

    content = await _render_dashboard(ui, "operator", _GRANTED, vertical)
    assert margin_label in content

    content = await _render_dashboard(ui, "manager", {}, vertical)
    assert margin_label in content


async def test_cost_basis_kpi_follows_permission(ui):
    """The cost KPI card shows exactly for roles holding set_inventory_prices."""
    vertical = "gemstones"
    content = await _render_dashboard(ui, "operator", {}, vertical)
    assert b"Cost Basis" not in content

    content = await _render_dashboard(ui, "operator", _GRANTED, vertical)
    assert b"Cost Basis" in content

    content = await _render_dashboard(ui, "manager", {}, vertical)
    assert b"Cost Basis" in content

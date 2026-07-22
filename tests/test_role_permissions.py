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


# The full 2.1 catalogue: key -> (default_role, grantable, floor_role).
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
        assert p.default_role == default, key
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


# ── Sales document price gate (J5) ────────────────────────────────────────────

async def _make_draft_invoice(client, headers, ctx, unit_price, ref_id):
    """Create a one-line draft invoice referencing the perm item; return its id."""
    r = await client.post(
        "/docs",
        json={
            "doc_type": "invoice",
            "ref_id": ref_id,
            "line_items": [{
                "sku": "SKU-PERM", "item_id": ctx["item_id"],
                "quantity": 2, "unit_price": unit_price,
            }],
            "total": 2 * unit_price,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _line_patch(item_id, quantity, unit_price, old_quantity=2, old_unit_price=50.0):
    """A DocPatch body changing the single line to the given quantity/price."""
    line = {"sku": "SKU-PERM", "item_id": item_id}
    return {"fields_changed": {"line_items": {
        "old": [{**line, "quantity": old_quantity, "unit_price": old_unit_price}],
        "new": [{**line, "quantity": quantity, "unit_price": unit_price}],
    }}}


async def test_operator_revoked_patch_price_403(client, session):
    """With set_sales_doc_prices raised to manager, an operator changing unit_price
    on a draft is rejected naming the permission; a quantity-only edit still saves."""
    ctx = await perm_setup(client, session)
    doc_id = await _make_draft_invoice(client, ctx["admin_h"], ctx, 50.0, "INV-P40")
    await grant_permission(client, ctx["admin_h"], "set_sales_doc_prices", "manager")

    r = await client.patch(f"/docs/{doc_id}", json=_line_patch(ctx["item_id"], 2, 75.0),
                           headers=ctx["operator_h"])
    assert r.status_code == 403, r.text
    assert "set_sales_doc_prices" in r.json()["detail"]

    r2 = await client.patch(f"/docs/{doc_id}", json=_line_patch(ctx["item_id"], 3, 50.0),
                            headers=ctx["operator_h"])
    assert r2.status_code == 200, r2.text


async def test_operator_revoked_create_price_403(client, session):
    """Doc create with a line whose unit_price deviates from the catalog price is
    rejected; a catalog-priced line succeeds."""
    ctx = await perm_setup(client, session)
    r = await client.post(f"/items/{ctx['item_id']}/price",
                          json={"price_type": "Retail", "new_price": 100.0},
                          headers=ctx["admin_h"])
    assert r.status_code == 200, r.text
    await grant_permission(client, ctx["admin_h"], "set_sales_doc_prices", "manager")

    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "INV-P41A",
              "line_items": [{"sku": "SKU-PERM", "item_id": ctx["item_id"],
                              "quantity": 1, "unit_price": 150.0}],
              "total": 150.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 403, r.text
    assert "set_sales_doc_prices" in r.json()["detail"]

    r2 = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "INV-P41B",
              "line_items": [{"sku": "SKU-PERM", "item_id": ctx["item_id"],
                              "quantity": 1, "unit_price": 100.0}],
              "total": 100.0},
        headers=ctx["operator_h"],
    )
    assert r2.status_code == 200, r2.text


async def test_operator_default_sets_doc_price(client, session):
    """Confirmatory: with no override, an operator sets and edits document prices
    exactly as today."""
    ctx = await perm_setup(client, session)
    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "INV-P42",
              "line_items": [{"sku": "SKU-PERM", "item_id": ctx["item_id"],
                              "quantity": 1, "unit_price": 999.0}],
              "total": 999.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]

    r2 = await client.patch(
        f"/docs/{doc_id}",
        json={"fields_changed": {"line_items": {
            "old": [{"sku": "SKU-PERM", "item_id": ctx["item_id"], "quantity": 1, "unit_price": 999.0}],
            "new": [{"sku": "SKU-PERM", "item_id": ctx["item_id"], "quantity": 1, "unit_price": 50.0}],
        }}},
        headers=ctx["operator_h"],
    )
    assert r2.status_code == 200, r2.text


async def test_save_doc_lines_surfaces_permission_error(client, session):
    """The UI save endpoint returns {"error": ...} naming the permission when the
    underlying PATCH is rejected, driven end to end through the real API."""
    from unittest.mock import patch
    from httpx import ASGITransport, AsyncClient
    from celerp.main import app as api_app

    ctx = await perm_setup(client, session)
    doc_id = await _make_draft_invoice(client, ctx["admin_h"], ctx, 50.0, "INV-P43")
    await grant_permission(client, ctx["admin_h"], "set_sales_doc_prices", "manager")
    operator_token = ctx["operator_h"]["Authorization"].split()[1]

    def _bridged_client(token, timeout=10.0):
        return AsyncClient(
            transport=ASGITransport(app=api_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )

    from ui.app import app as ui_app
    with patch("ui.api_client._client", _bridged_client):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as ui_c:
            r = await ui_c.post(
                f"/docs/{doc_id}/lines",
                cookies={"celerp_token": operator_token},
                json={"line_items": [{"sku": "SKU-PERM", "item_id": ctx["item_id"],
                                      "quantity": 2, "unit_price": 75.0}],
                      "subtotal": 150.0, "tax": 0, "total": 150.0},
            )
    assert r.status_code == 400, r.text
    assert "set_sales_doc_prices" in r.json()["error"]


# ── Matrix save API: PATCH /companies/me/role-permissions (J3) ─────────────────

from test_helpers import invite_user  # noqa: E402

_ROLE_PERM_URL = "/companies/me/role-permissions"


async def test_patch_role_permissions_owner_only(client, session):
    """Editing permissions is owner-only: an admin is refused, the owner accepts."""
    ctx = await perm_setup(client, session)
    admin_tok = await invite_user(client, session, ctx["admin_h"], "adm@perm.com", "admin")
    admin_h = {"Authorization": f"Bearer {admin_tok}"}
    body = {"perm_key": "set_inventory_prices", "role_key": "operator", "granted": True}

    r = await client.patch(_ROLE_PERM_URL, json=body, headers=admin_h)
    assert r.status_code == 403, r.text

    r2 = await client.patch(_ROLE_PERM_URL, json=body, headers=ctx["admin_h"])
    assert r2.status_code == 200, r2.text


async def test_patch_role_permissions_unknown_perm_422(client, session):
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "not_a_permission", "role_key": "operator", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 422, r.text


async def test_patch_role_permissions_non_grantable_403(client, session):
    """A fixed row (manage_billing) cannot be reassigned even by the owner."""
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "manage_billing", "role_key": "admin", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 403, r.text


async def test_patch_role_permissions_unknown_role_422(client, session):
    """An unknown role, including a retired legacy alias, is refused."""
    ctx = await perm_setup(client, session)
    for bad in ("wizard", "salesperson"):  # salesperson is a migrated legacy alias, not a role key
        r = await client.patch(
            _ROLE_PERM_URL,
            json={"perm_key": "set_inventory_prices", "role_key": bad, "granted": True},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 422, (bad, r.text)


async def test_patch_role_permissions_persists(client, session):
    """A granted override survives a fresh settings read."""
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "set_inventory_prices", "role_key": "operator", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 200, r.text

    r2 = await client.get("/companies/me", headers=ctx["admin_h"])
    assert r2.status_code == 200, r2.text
    overrides = (r2.json()["settings"] or {}).get("role_permissions") or {}
    assert overrides.get("set_inventory_prices") == "operator"


async def test_patch_role_permissions_malformed_boolean_422(client, session):
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "set_inventory_prices", "role_key": "operator", "granted": "banana"},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 422, r.text


async def test_patch_role_permissions_below_floor_422(client, session):
    """Granting set_sales_doc_prices to viewer sits below its operator floor: 422
    naming the floor, never an accidental sub-floor grant."""
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "set_sales_doc_prices", "role_key": "viewer", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 422, r.text
    assert "operator" in r.json()["detail"]


# ── Matrix render + per-toggle UI route (J3) ──────────────────────────────────

def _settings_patches(settings: dict, users: list | None = None):
    from unittest.mock import AsyncMock, patch

    company = {"name": "Perm Co", "settings": settings}
    return [
        patch("ui.api_client.get_company", AsyncMock(return_value=company)),
        patch("ui.api_client.get_users", AsyncMock(return_value={"items": users or []})),
        patch("ui.api_client.get_modules", AsyncMock(return_value=[])),
    ]


async def _render_users_tab(ui, role: str, settings: dict) -> str:
    from contextlib import ExitStack

    from test_helpers import authed_cookies

    with ExitStack() as stack:
        for p in _settings_patches(settings):
            stack.enter_context(p)
        r = await ui.get("/settings/general", params={"tab": "users"},
                         cookies=authed_cookies(role=role))
    assert r.status_code == 200, r.text
    return r.text


def _cell(html: str, perm: str, role: str) -> str:
    import re

    m = re.search(rf'<input[^>]*id="perm-{perm}-{role}"[^>]*>', html)
    assert m, f"no matrix cell rendered for {perm}/{role}"
    return m.group(0)


async def test_matrix_renders_checkboxes_for_owner(ui):
    """The owner sees interactive checkbox cells wired to the per-toggle route."""
    html = await _render_users_tab(ui, "owner", {})
    assert 'type="checkbox"' in html
    # A default-granted, grantable cell is interactive for the owner.
    assert 'hx-patch="/settings/roles/edit_documents/operator"' in html
    assert "checked" in _cell(html, "edit_documents", "operator")


async def test_matrix_disabled_for_admin(ui):
    """An admin sees the same matrix, every cell disabled and none wired to save."""
    html = await _render_users_tab(ui, "admin", {})
    assert 'type="checkbox"' in html
    assert 'hx-patch="/settings/roles/' not in html
    assert "disabled" in _cell(html, "edit_documents", "operator")


async def test_matrix_reflects_overrides(ui):
    """A stored override checks the lower role's column that the default leaves clear."""
    assert "checked" not in _cell(await _render_users_tab(ui, "owner", {}),
                                  "set_inventory_prices", "operator")
    html = await _render_users_tab(ui, "owner", {"role_permissions": {"set_inventory_prices": "operator"}})
    assert "checked" in _cell(html, "set_inventory_prices", "operator")


async def test_matrix_role_columns_from_registry(ui):
    """Columns come from the ROLES registry, not hardcoded role literals."""
    from ui.i18n import t
    from celerp.services.permissions import ROLES

    html = await _render_users_tab(ui, "owner", {})
    for role in ROLES:
        assert t(role.label_key) in html, role.key
        # every registry role is a real column: it has a cell in a grantable row
        _cell(html, "edit_documents", role.key)


async def test_matrix_row_swap_shows_threshold_fill(client, session):
    """Toggling a cell returns the full re-rendered row with the higher roles that
    inherit the permission also checked (the unmissable threshold feedback)."""
    from unittest.mock import patch

    from httpx import ASGITransport, AsyncClient

    from celerp.main import app as api_app

    ctx = await perm_setup(client, session)
    owner_token = ctx["admin_h"]["Authorization"].split()[1]

    def _bridged_client(token, timeout=10.0):
        return AsyncClient(
            transport=ASGITransport(app=api_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )

    from ui.app import app as ui_app
    with patch("ui.api_client._client", _bridged_client):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as ui_c:
            r = await ui_c.patch(
                "/settings/roles/set_inventory_prices/operator",
                cookies={"celerp_token": owner_token},
                data={"granted": "true"},
            )
    assert r.status_code == 200, r.text
    # operator now granted, so manager and admin (higher) inherit and show checked too.
    for role in ("operator", "manager", "admin"):
        assert "checked" in _cell(r.text, "set_inventory_prices", role), role


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


# ── Default parity, override flips, and write floors (2.8) ─────────────────────

async def test_write_floor_rejects_viewer_grant(client, session):
    """Granting set_inventory_prices to viewer sits below its operator floor: the
    matrix save returns 422 naming the floor, the replacement for the deleted
    router-level read-only baseline."""
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "set_inventory_prices", "role_key": "viewer", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 422, r.text
    assert "operator" in r.json()["detail"]


async def test_default_parity_matrix(client, session):
    """With empty overrides, each default tier's representative endpoint allows the
    role at its default and denies the role one level below - the exact thresholds
    the enforcement sites carried before the registry. The per-key resolver parity
    is proven exhaustively by test_defaults_match_current_behavior; this pins the
    HTTP behavior at every tier boundary."""
    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.com', 'admin')}"}
    viewer_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'vwr@perm.com', 'viewer')}"}

    def _item(sku):
        return {"sku": sku, "name": "Par", "quantity": 1,
                "location_id": ctx["location_id"], "sell_by": "piece"}

    # operator tier: edit_inventory (POST /items). viewer denied, operator allowed.
    assert (await client.post("/items", json=_item("PAR-V"), headers=viewer_h)).status_code == 403
    assert (await client.post("/items", json=_item("PAR-O"), headers=ctx["operator_h"])).status_code == 200

    # manager tier: set_inventory_prices (POST /items/{id}/price). operator denied, manager allowed.
    price = {"price_type": "retail_price", "new_price": 12.0}
    assert (await client.post(f"/items/{ctx['item_id']}/price", json=price, headers=ctx["operator_h"])).status_code == 403
    assert (await client.post(f"/items/{ctx['item_id']}/price", json=price, headers=ctx["manager_h"])).status_code == 200

    # admin tier: manage_company_settings (PATCH /companies/me). manager denied, admin allowed.
    assert (await client.patch("/companies/me", json={"name": "Par X"}, headers=ctx["manager_h"])).status_code == 403
    assert (await client.patch("/companies/me", json={"name": "Par Y"}, headers=admin_h)).status_code == 200

    # owner tier: manage_permissions (PATCH matrix). admin denied, owner allowed.
    grant = {"perm_key": "set_inventory_prices", "role_key": "operator", "granted": True}
    assert (await client.patch(_ROLE_PERM_URL, json=grant, headers=admin_h)).status_code == 403
    assert (await client.patch(_ROLE_PERM_URL, json=grant, headers=ctx["admin_h"])).status_code == 200


async def test_override_grant_flips_gate(client, session):
    """Granting a key to a role below its default flips that role's representative
    endpoint from 403 to success."""
    ctx = await perm_setup(client, session)
    price = {"price_type": "retail_price", "new_price": 20.0}
    # operator lacks set_inventory_prices (manager default) by default.
    assert (await client.post(f"/items/{ctx['item_id']}/price", json=price, headers=ctx["operator_h"])).status_code == 403
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    assert (await client.post(f"/items/{ctx['item_id']}/price", json=price, headers=ctx["operator_h"])).status_code == 200


async def test_override_revoke_flips_gate(client, session):
    """Raising a key's threshold above a role flips that role's representative
    endpoint from success to 403."""
    ctx = await perm_setup(client, session)

    def _item(sku):
        return {"sku": sku, "name": "Rev", "quantity": 1,
                "location_id": ctx["location_id"], "sell_by": "piece"}

    # operator holds edit_inventory (operator default) today.
    assert (await client.post("/items", json=_item("REV-1"), headers=ctx["operator_h"])).status_code == 200
    await grant_permission(client, ctx["admin_h"], "edit_inventory", "manager")
    assert (await client.post("/items", json=_item("REV-2"), headers=ctx["operator_h"])).status_code == 403


async def test_fixed_rows_reject_patch(client, session):
    """PATCH targeting any fixed row is refused for everyone, owner included."""
    ctx = await perm_setup(client, session)
    for key in ("manage_permissions", "manage_company_lifecycle", "manage_billing"):
        r = await client.patch(
            _ROLE_PERM_URL,
            json={"perm_key": key, "role_key": "admin", "granted": True},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 403, (key, r.text)


def test_nav_visibility_follows_permissions():
    """Sidebar entries render exactly when the caller's resolved role holds the
    entry's permission: a granted lower role gains an entry, a revoked higher role
    loses one."""
    import os
    from celerp.modules import slots
    from celerp.modules.loader import load_all
    from fasthtml.common import to_xml

    from ui.components.shell import _sidebar

    # Register the docs nav slots deterministically: the global slot registry's
    # contents depend on which tests ran earlier in this worker, so rebuild it
    # from the real manifests. load_all refuses a module whose depends_on chain
    # is not enabled, so celerp-docs needs its dependencies alongside it.
    saved = slots.all_slots()
    slots.clear()
    load_all(
        os.environ.get("MODULE_DIR") or "default_modules",
        {"celerp-inventory", "celerp-contacts", "celerp-docs"},
    )
    try:
        # Payments gates on view_payments (manager default): operator cannot see it...
        assert "/payments" not in to_xml(_sidebar("dashboard", role="operator", settings={}))
        # ...until it is granted down to operator.
        granted = {"role_permissions": {"view_payments": "operator"}}
        assert "/payments" in to_xml(_sidebar("dashboard", role="operator", settings=granted))

        # Sales Documents gate on view_documents (viewer default): a manager sees them,
        # but not once the key is raised to admin.
        assert "/docs?type=invoice" in to_xml(_sidebar("dashboard", role="manager", settings={}))
        revoked = {"role_permissions": {"view_documents": "admin"}}
        assert "/docs?type=invoice" not in to_xml(_sidebar("dashboard", role="manager", settings=revoked))
    finally:
        slots.clear()
        for slot_name, contributions in saved.items():
            for contribution in contributions:
                slots.register(slot_name, contribution)


def test_kpi_specs_follow_permissions():
    """Dashboard KPI cards filter on permission membership: a permissioned card
    shows only for roles holding the key; ungated cards always show."""
    from fasthtml.common import to_xml

    from ui.routes.dashboard import _kpi_grid

    cfg = {"kpis": [
        {"label": "Cost Basis", "value_fn": "cost_total", "permission": "set_inventory_prices"},
        {"label": "Item Count", "value_fn": "item_count"},
    ]}
    values = {"cost_total": "400", "item_count": "3"}

    op = to_xml(_kpi_grid(cfg, values, role="operator", settings={}))
    assert "Cost Basis" not in op and "Item Count" in op

    granted = {"role_permissions": {"set_inventory_prices": "operator"}}
    assert "Cost Basis" in to_xml(_kpi_grid(cfg, values, role="operator", settings=granted))
    assert "Cost Basis" in to_xml(_kpi_grid(cfg, values, role="manager", settings={}))


async def test_owner_column_fixed(ui):
    """The owner column renders checked and disabled in every grantable row: the
    owner holds everything and cannot be unset."""
    html = await _render_users_tab(ui, "owner", {})
    for perm in ("edit_documents", "set_inventory_prices", "manage_users"):
        cell = _cell(html, perm, "owner")
        assert "checked" in cell, perm
        assert "disabled" in cell, perm


async def test_deactivate_owner_only(client, session):
    """Company deactivation is owner-only: an admin is refused, the owner succeeds."""
    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.com', 'admin')}"}
    assert (await client.delete("/companies/me", headers=admin_h)).status_code == 403
    assert (await client.delete("/companies/me", headers=ctx["admin_h"])).status_code == 200


async def test_reactivate_and_reseed_owner_only(client, session):
    """Reactivation and demo reseed are owner-only like deactivation."""
    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.com', 'admin')}"}

    # Reseed on the active company: the gate refuses the admin, the owner succeeds.
    assert (await client.post("/companies/me/demo/reseed", headers=admin_h)).status_code == 403
    assert (await client.post("/companies/me/demo/reseed", headers=ctx["admin_h"])).status_code == 200

    # Reactivate gate refuses the admin while the company is still active (the gate
    # runs before any state check); the owner then deactivates and reactivates.
    assert (await client.post("/companies/me/reactivate", headers=admin_h)).status_code == 403
    assert (await client.delete("/companies/me", headers=ctx["admin_h"])).status_code == 200
    assert (await client.post("/companies/me/reactivate", headers=ctx["admin_h"])).status_code == 200


def test_web_access_link_requires_integrations():
    """The footer Web Access link renders only for a role holding manage_integrations
    (admin default): absent for a manager, present for an admin."""
    from fasthtml.common import to_xml

    from ui.components.shell import _sidebar

    assert "/settings/cloud" not in to_xml(_sidebar("dashboard", role="manager", settings={}))
    assert "/settings/cloud" in to_xml(_sidebar("dashboard", role="admin", settings={}))


async def test_ai_routes_require_permission(client, session):
    """An AI endpoint returns 403 for a viewer under default permissions; an
    operator (holding use_ai_assistant by default) is admitted. The AI router also
    sits behind the Cloud+AI subscription gate (require_session_token); this test
    isolates the permission gate by satisfying that subscription gate, so a plain
    subscription pass cannot be mistaken for a permission pass."""
    from celerp.main import app
    from celerp.session_gate import require_session_token

    ctx = await perm_setup(client, session)
    viewer_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'vwr@perm.com', 'viewer')}"}
    app.dependency_overrides[require_session_token] = lambda: None
    try:
        assert (await client.get("/ai/memory", headers=viewer_h)).status_code == 403
        assert (await client.get("/ai/memory", headers=ctx["operator_h"])).status_code == 200
    finally:
        app.dependency_overrides.pop(require_session_token, None)


async def test_accounting_reads_require_permission(client, session):
    """The chart-of-accounts read returns 403 for an operator; a manager (holding
    manage_accounting by default) is admitted."""
    ctx = await perm_setup(client, session)
    assert (await client.get("/accounting/chart", headers=ctx["operator_h"])).status_code == 403
    assert (await client.get("/accounting/chart", headers=ctx["manager_h"])).status_code == 200


def test_bulk_action_filter_enforced():
    """A permission-gated bulk action is dropped from the inventory bulk toolbar for
    a role lacking the key and kept for a role holding it, while an ungated action
    always shows. The gated shape mirrors the connectors module's adjust_inventory
    contribution; injecting it keeps the test independent of which optional modules
    a given deployment enables."""
    from unittest.mock import patch

    from fasthtml.common import to_xml

    from ui.routes.inventory import _bulk_toolbar

    actions = [
        {"label": "Enable Shopify sync", "form_action": "/api/items/bulk/shopify-sync/enable",
         "icon": "🛍", "action_type": "htmx", "permission": "adjust_inventory",
         "_module": "celerp-connectors"},
        {"label": "Print Labels", "form_action": "/labels/print-bulk",
         "icon": "🖨", "action_type": "navigate", "_module": "celerp-labels"},
    ]

    def _fake_get(slot):
        return list(actions) if slot == "bulk_action" else []

    with patch("celerp.modules.slots.get", side_effect=_fake_get):
        operator_html = to_xml(_bulk_toolbar([], settings={}, role="operator"))
        manager_html = to_xml(_bulk_toolbar([], settings={}, role="manager"))

    # adjust_inventory defaults to manager: the operator loses the gated action,
    # keeps the ungated one; the manager sees both.
    assert "Enable Shopify sync" not in operator_html
    assert "Print Labels" in operator_html
    assert "Enable Shopify sync" in manager_html


async def test_payments_page_requires_permission(ui):
    """/payments redirects an operator (lacking view_payments, a manager default) to
    the dashboard rather than rendering the payments list."""
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token

    company = {"currency": "THB", "settings": {}}
    with patch("ui.api_client.get_company", AsyncMock(return_value=company)):
        r = await ui.get("/payments", cookies={"celerp_token": make_test_token(role="operator")})
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


async def test_raised_view_inventory_redirects_viewer(ui):
    """With view_inventory raised to operator, a viewer requesting /inventory is
    redirected to the dashboard. The page reads settings from the company, so the
    raised override is enough to close the page to the viewer."""
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token

    company = {"currency": "THB",
               "settings": {"role_permissions": {"view_inventory": "operator"}}}
    patches = [
        patch("ui.api_client.get_company", AsyncMock(return_value=company)),
        patch("ui.api_client.get_item_schema", AsyncMock(return_value={})),
        patch("ui.api_client.get_all_category_schemas", AsyncMock(return_value={})),
        patch("ui.api_client.get_column_prefs", AsyncMock(return_value={})),
        patch("ui.api_client.get_locations", AsyncMock(return_value={"items": []})),
    ]
    for p in patches:
        p.start()
    try:
        r = await ui.get("/inventory", cookies={"celerp_token": make_test_token(role="viewer")})
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


async def _assert_page_redirects_when_revoked(ui, path: str, perm_key: str):
    """A viewer requesting *path* with *perm_key* raised to operator is redirected
    to the dashboard by the page-level gate, before the handler loads any data."""
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token

    company = {"currency": "THB",
               "settings": {"role_permissions": {perm_key: "operator"}}}
    with patch("ui.api_client.get_company", AsyncMock(return_value=company)):
        r = await ui.get(path, cookies={"celerp_token": make_test_token(role="viewer")})
    assert r.status_code == 302, (path, r.status_code)
    assert r.headers["location"] == "/dashboard", path


async def test_dashboard_page_requires_view_dashboards(ui):
    """A viewer whose view_dashboards is revoked gets the not-authorized shell on
    /dashboard, not the KPI grid. The page has nowhere to redirect (it is the
    redirect target), so it degrades in place with a clear message."""
    no_access = b"You do not have access to this page."
    revoked = {"role_permissions": {"view_dashboards": "operator"}}
    content = await _render_dashboard(ui, "viewer", revoked, "coins_precious_metals")
    assert no_access in content

    allowed = await _render_dashboard(ui, "viewer", {}, "coins_precious_metals")
    assert no_access not in allowed


async def test_history_page_requires_view_dashboards(ui):
    await _assert_page_redirects_when_revoked(ui, "/history", "view_dashboards")


async def test_docs_page_requires_view_documents(ui):
    await _assert_page_redirects_when_revoked(ui, "/docs", "view_documents")


async def test_lists_page_requires_view_documents(ui):
    await _assert_page_redirects_when_revoked(ui, "/lists", "view_documents")


async def test_doc_detail_requires_view_documents(ui):
    await _assert_page_redirects_when_revoked(ui, "/docs/doc_1", "view_documents")


async def test_customers_page_requires_view_contacts(ui):
    await _assert_page_redirects_when_revoked(ui, "/contacts/customers", "view_contacts")


async def test_vendors_page_requires_view_contacts(ui):
    await _assert_page_redirects_when_revoked(ui, "/contacts/vendors", "view_contacts")


async def test_item_detail_requires_view_inventory(ui):
    await _assert_page_redirects_when_revoked(ui, "/inventory/item_1", "view_inventory")


def test_guard_family_removed():
    """No source file under celerp/, ui/, or default_modules/ still names the deleted
    guard family or the old min_role nav vocabulary."""
    import pathlib
    import subprocess

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    banned = ["require_admin", "require_operator", "require_manager", "require_min_role",
              "viewer_read_only", "_check_role", "min_role"]
    pattern = r"\b(" + "|".join(banned) + r")\b"
    r = subprocess.run(
        ["grep", "-rnE", pattern, "celerp", "ui", "default_modules", "--include=*.py"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert r.returncode == 1, f"guard-family stragglers found:\n{r.stdout}"

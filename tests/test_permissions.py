# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Permission enforcement tests.

Covers:
- visible_to_roles: cost_price hidden from operator on GET /items and GET /items/{id}
- Write-path guard: operator cannot set cost_price on POST /items or PATCH /items/{id}
- require_manager: operator blocked from destructive/financial operations
- require_admin: operator/manager blocked from admin-only ops
- Admin, owner, and manager can access all guarded routes
- 5-role hierarchy: owner > admin > manager > operator > viewer
- Legacy JWT migration: salesperson → operator
"""
from __future__ import annotations

import pytest


# ── Shared helpers ────────────────────────────────────────────────────────────

async def _register_admin(client) -> str:
    """Register first-admin (creates owner role) and return access token."""
    r = await client.post(
        "/auth/register",
        json={"company_name": "Perm Co", "email": "admin@perm.com", "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _invite_user(client, admin_headers: dict, email: str, role: str) -> str:
    """Create a user with the given role and return their access token."""
    r = await client.post(
        "/companies/me/users",
        json={"email": email, "name": role.title(), "role": role, "password": "pw123"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    r2 = await client.post("/auth/login", json={"email": email, "password": "pw123"})
    assert r2.status_code == 200, r2.text
    return r2.json()["access_token"]


async def _create_item(client, headers: dict, location_id: str, sku: str = "SKU-PERM") -> str:
    """Create an item as admin and return its entity_id."""
    r = await client.post(
        "/items",
        json={"sku": sku, "name": "Perm Item", "quantity": 5, "location_id": location_id, "cost_price": 100.0, "sell_by": "piece"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _setup(client):
    """Bootstrap: admin token, manager token, operator token, location_id, item_id."""
    admin_tok = await _register_admin(client)
    admin_h = {"Authorization": f"Bearer {admin_tok}"}

    loc_r = await client.post(
        "/companies/me/locations",
        json={"name": "Main", "type": "warehouse", "address": None, "is_default": True},
        headers=admin_h,
    )
    location_id = loc_r.json()["id"]
    item_id = await _create_item(client, admin_h, location_id)

    manager_tok = await _invite_user(client, admin_h, "mgr@perm.com", "manager")
    operator_tok = await _invite_user(client, admin_h, "operator@perm.com", "operator")

    return {
        "admin_h":    {"Authorization": f"Bearer {admin_tok}"},
        "manager_h":  {"Authorization": f"Bearer {manager_tok}"},
        "staff_h":    {"Authorization": f"Bearer {operator_tok}"},   # alias for legacy tests
        "operator_h": {"Authorization": f"Bearer {operator_tok}"},
        "location_id": location_id,
        "item_id": item_id,
    }


# ── visible_to_roles: cost_price field visibility ─────────────────────────────

class TestFieldVisibility:

    async def test_admin_sees_cost_price_in_list(self, client):
        ctx = await _setup(client)
        r = await client.get("/items", headers=ctx["admin_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert "cost_price" in items[0]

    async def test_manager_sees_cost_price_in_list(self, client):
        ctx = await _setup(client)
        r = await client.get("/items", headers=ctx["manager_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert "cost_price" in items[0]

    async def test_staff_cannot_see_cost_price_in_list(self, client):
        ctx = await _setup(client)
        r = await client.get("/items", headers=ctx["staff_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert "cost_price" not in items[0]

    async def test_admin_sees_cost_price_in_detail(self, client):
        ctx = await _setup(client)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["admin_h"])
        assert r.status_code == 200
        assert "cost_price" in r.json()

    async def test_staff_cannot_see_cost_price_in_detail(self, client):
        ctx = await _setup(client)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["staff_h"])
        assert r.status_code == 200
        assert "cost_price" not in r.json()

    async def test_manager_sees_cost_price_in_detail(self, client):
        ctx = await _setup(client)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["manager_h"])
        assert r.status_code == 200
        assert "cost_price" in r.json()


# ── Write-path: cost_price field write guard ──────────────────────────────────

class TestCostPriceWriteGuard:

    async def test_staff_cannot_set_cost_price_on_create(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items",
            json={"sku": "STAFF-SKU", "name": "Staff Item", "quantity": 1,
                  "location_id": ctx["location_id"], "cost_price": 50.0, "sell_by": "piece"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_can_create_item_without_cost_price(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items",
            json={"sku": "STAFF-SKU2", "name": "Staff Item", "quantity": 1,
                  "location_id": ctx["location_id"], "sell_by": "piece"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_patch_cost_price(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"cost_price": {"old": 100.0, "new": 50.0}}},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_can_patch_non_restricted_field(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"name": {"old": "Perm Item", "new": "Updated"}}},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 200

    async def test_manager_can_patch_cost_price(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"cost_price": {"old": 100.0, "new": 80.0}}},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_admin_can_set_cost_price_on_create(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items",
            json={"sku": "ADMIN-COST", "name": "Admin Item", "quantity": 1,
                  "location_id": ctx["location_id"], "cost_price": 200.0, "sell_by": "piece"},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200


# ── require_manager: destructive item operations ──────────────────────────────

class TestManagerRequiredItemOps:

    async def test_staff_cannot_bulk_delete(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [ctx["item_id"]]},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_bulk_delete(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [ctx["item_id"]]},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_adjust_quantity(self, client):
        ctx = await _setup(client)
        r = await client.post(
            f"/items/{ctx['item_id']}/adjust",
            json={"new_qty": 99},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_adjust_quantity(self, client):
        ctx = await _setup(client)
        r = await client.post(
            f"/items/{ctx['item_id']}/adjust",
            json={"new_qty": 99},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_set_price(self, client):
        ctx = await _setup(client)
        r = await client.post(
            f"/items/{ctx['item_id']}/price",
            json={"price_type": "cost_price", "new_price": 50.0},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_set_price(self, client):
        ctx = await _setup(client)
        r = await client.post(
            f"/items/{ctx['item_id']}/price",
            json={"price_type": "retail_price", "new_price": 150.0},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_dispose_item(self, client):
        ctx = await _setup(client)
        r = await client.post(f"/items/{ctx['item_id']}/dispose", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_dispose_item(self, client):
        ctx = await _setup(client)
        r = await client.post(f"/items/{ctx['item_id']}/dispose", headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_expire_item(self, client):
        ctx = await _setup(client)
        r = await client.post(f"/items/{ctx['item_id']}/expire", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_staff_cannot_access_valuation(self, client):
        ctx = await _setup(client)
        r = await client.get("/items/valuation", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_access_valuation(self, client):
        ctx = await _setup(client)
        r = await client.get("/items/valuation", headers=ctx["manager_h"])
        assert r.status_code == 200


# ── require_manager: financial document operations ────────────────────────────

class TestManagerRequiredDocOps:

    async def _create_draft_doc(self, client, headers: dict) -> str:
        r = await client.post(
            "/docs",
            json={"doc_type": "invoice", "contact_name": "Client", "line_items": []},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        return r.json()["id"]

    async def test_staff_cannot_finalize_doc(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/finalize", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_finalize_doc(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/finalize", headers=ctx["manager_h"])
        assert r.status_code in (200, 409)  # 409 if already finalized

    async def test_staff_cannot_void_doc(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/void",
            json={"reason": "test"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_cannot_record_payment(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/payment",
            json={"amount": 100.0, "method": "cash", "reference": "REF1"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_cannot_refund_payment(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/refund",
            json={"amount": 10.0, "method": "cash", "reference": "REF2"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403


# ── require_manager: accounting operations ────────────────────────────────────

class TestManagerRequiredAccountingOps:

    async def test_staff_cannot_create_account(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/accounting/accounts",
            json={"code": "9999", "name": "Test Account", "account_type": "expense"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_create_account(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/accounting/accounts",
            json={"code": "9998", "name": "Mgr Account", "account_type": "expense"},
            headers=ctx["manager_h"],
        )
        assert r.status_code in (200, 409)  # 409 if code already exists

    async def test_staff_cannot_see_profit_and_loss(self, client):
        ctx = await _setup(client)
        r = await client.get("/accounting/pnl", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_see_profit_and_loss(self, client):
        ctx = await _setup(client)
        r = await client.get("/accounting/pnl", headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_see_balance_sheet(self, client):
        ctx = await _setup(client)
        r = await client.get("/accounting/balance-sheet", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_see_balance_sheet(self, client):
        ctx = await _setup(client)
        r = await client.get("/accounting/balance-sheet", headers=ctx["manager_h"])
        assert r.status_code == 200


# ── require_manager: reports with cost data ───────────────────────────────────

class TestManagerRequiredReports:

    async def test_staff_cannot_access_sales_report(self, client):
        ctx = await _setup(client)
        r = await client.get("/reports/sales", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_access_sales_report(self, client):
        ctx = await _setup(client)
        r = await client.get("/reports/sales", headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_access_purchases_report(self, client):
        ctx = await _setup(client)
        r = await client.get("/reports/purchases", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_admin_can_access_purchases_report(self, client):
        ctx = await _setup(client)
        r = await client.get("/reports/purchases", headers=ctx["admin_h"])
        assert r.status_code == 200

    async def test_staff_can_access_ar_aging(self, client):
        """AR aging is operational data - staff-accessible."""
        ctx = await _setup(client)
        r = await client.get("/reports/ar-aging", headers=ctx["staff_h"])
        assert r.status_code == 200

    async def test_staff_can_access_expiring_items(self, client):
        """Expiring items report - operational, staff-accessible."""
        ctx = await _setup(client)
        r = await client.get("/reports/expiring", headers=ctx["staff_h"])
        assert r.status_code == 200


# ── require_admin: admin-only operations ─────────────────────────────────────

class TestAdminOnlyOps:

    async def test_staff_cannot_patch_company(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me",
            json={"name": "Hacked Co"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_cannot_patch_company(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me",
            json={"name": "Hacked Co"},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_patch_company(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me",
            json={"name": "Renamed Co"},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200

    async def test_manager_cannot_undo_import(self, client):
        ctx = await _setup(client)
        # Non-existent batch — 403 should fire before 404
        r = await client.post("/items/import/batches/fake-id/undo", headers=ctx["manager_h"])
        assert r.status_code == 403

    async def test_manager_cannot_patch_item_schema(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me/item-schema",
            json={"fields": []},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_patch_item_schema(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me/item-schema",
            json={"fields": [
                {"key": "sku", "label": "SKU", "type": "text", "editable": True, "required": True,
                 "options": [], "visible_to_roles": [], "position": 0, "show_in_table": True, "sell_by": "piece"},
            ]},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200


# ── 5-role hierarchy: owner / admin / manager / operator / viewer ─────────────

class TestRoleHierarchy:

    async def test_owner_can_patch_company(self, client):
        """Owner (level 5) passes require_admin (level 4)."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        # The registered user has role=owner; token carries owner
        r = await client.patch(
            "/companies/me",
            json={"name": "Owner Renamed"},
            headers=admin_h,
        )
        assert r.status_code == 200

    async def test_owner_can_create_user(self, client):
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        r = await client.post(
            "/companies/me/users",
            json={"email": "newuser@x.com", "name": "New", "role": "operator", "password": "pw123"},
            headers=admin_h,
        )
        assert r.status_code == 200

    async def test_admin_user_can_patch_company(self, client):
        """Explicitly created admin (level 4) also passes require_admin."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        admin2_tok = await _invite_user(client, admin_h, "admin2@x.com", "admin")
        admin2_h = {"Authorization": f"Bearer {admin2_tok}"}
        r = await client.patch(
            "/companies/me",
            json={"name": "Admin2 Renamed"},
            headers=admin2_h,
        )
        assert r.status_code == 200

    async def test_viewer_is_blocked_from_manager_ops(self, client):
        """Viewer (level 1) cannot perform manager-level operations."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        loc_r = await client.post(
            "/companies/me/locations",
            json={"name": "VWH", "type": "warehouse", "address": None, "is_default": True},
            headers=admin_h,
        )
        location_id = loc_r.json()["id"]
        item_id = await _create_item(client, admin_h, location_id, sku="VIEWER-ITEM")
        viewer_tok = await _invite_user(client, admin_h, "viewer@x.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [item_id]},
            headers=viewer_h,
        )
        assert r.status_code == 403

    async def test_viewer_cannot_access_valuation(self, client):
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        viewer_tok = await _invite_user(client, admin_h, "viewer2@x.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        r = await client.get("/items/valuation", headers=viewer_h)
        assert r.status_code == 403

    async def test_operator_blocked_from_manager_ops(self, client):
        """Operator (level 2) cannot perform manager-level operations."""
        ctx = await _setup(client)
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [ctx["item_id"]]},
            headers=ctx["operator_h"],
        )
        assert r.status_code == 403

    async def test_operator_blocked_from_finalize_doc(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/docs",
            json={"doc_type": "invoice", "contact_name": "Client", "line_items": []},
            headers=ctx["admin_h"],
        )
        doc_id = r.json()["id"]
        r2 = await client.post(f"/docs/{doc_id}/finalize", headers=ctx["operator_h"])
        assert r2.status_code == 403

    async def test_invalid_role_rejected_on_create_user(self, client):
        """Role not in ROLE_LEVELS is rejected with 400."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        r = await client.post(
            "/companies/me/users",
            json={"email": "bad@x.com", "name": "Bad", "role": "superuser", "password": "pw123"},
            headers=admin_h,
        )
        assert r.status_code == 400

    async def test_invalid_role_rejected_on_patch_user(self, client):
        """Patching a user to an invalid role returns 400."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        op_tok = await _invite_user(client, admin_h, "patchme@x.com", "operator")
        # Get user id by listing
        users_r = await client.get("/companies/me/users", headers=admin_h)
        user_id = next(u["id"] for u in users_r.json()["items"] if u["email"] == "patchme@x.com")
        r = await client.patch(
            f"/companies/me/users/{user_id}",
            json={"role": "wizard"},
            headers=admin_h,
        )
        assert r.status_code == 400


# ── Legacy JWT migration: salesperson → operator ──────────────────────────────

class TestLegacyRoleMigration:

    async def _make_salesperson_token(self, client) -> tuple[dict, str]:
        """Create company, then forge a token with role=salesperson."""
        from celerp.services.auth import create_access_token
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        # Create a user to get a valid user_id + company_id
        r = await client.post(
            "/companies/me/users",
            json={"email": "legacy@x.com", "name": "Legacy", "role": "operator", "password": "pw"},
            headers=admin_h,
        )
        assert r.status_code == 200
        user_id = r.json()["id"]
        # Get company_id from admin token
        import base64, json as _json
        pad = admin_tok.split(".")[1]
        pad += "=" * (-len(pad) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(pad))
        company_id = claims["company_id"]
        # Forge old-style token with role=salesperson
        legacy_tok = create_access_token(user_id, company_id, "salesperson")
        return admin_h, legacy_tok

    async def test_salesperson_token_allowed_as_operator(self, client):
        """Legacy salesperson JWT passes operator-level checks."""
        admin_h, legacy_tok = await self._make_salesperson_token(client)
        legacy_h = {"Authorization": f"Bearer {legacy_tok}"}
        loc_r = await client.post(
            "/companies/me/locations",
            json={"name": "LegWH", "type": "warehouse", "address": None, "is_default": True},
            headers=admin_h,
        )
        location_id = loc_r.json()["id"]
        # Operator can create items without cost_price
        r = await client.post(
            "/items",
            json={"sku": "LEG-SKU", "name": "Legacy Item", "quantity": 1,
                  "location_id": location_id, "sell_by": "piece"},
            headers=legacy_h,
        )
        assert r.status_code == 200

    async def test_salesperson_token_blocked_from_manager_ops(self, client):
        """Legacy salesperson JWT is blocked from manager-level operations."""
        admin_h, legacy_tok = await self._make_salesperson_token(client)
        legacy_h = {"Authorization": f"Bearer {legacy_tok}"}
        loc_r = await client.post(
            "/companies/me/locations",
            json={"name": "LegWH2", "type": "warehouse", "address": None, "is_default": True},
            headers=admin_h,
        )
        location_id = loc_r.json()["id"]
        item_id = await _create_item(client, admin_h, location_id, sku="LEG-ITEM2")
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [item_id]},
            headers=legacy_h,
        )
        assert r.status_code == 403

    async def test_salesperson_token_blocked_from_cost_price(self, client):
        """Legacy salesperson JWT cannot set cost_price (manager field)."""
        admin_h, legacy_tok = await self._make_salesperson_token(client)
        legacy_h = {"Authorization": f"Bearer {legacy_tok}"}
        loc_r = await client.post(
            "/companies/me/locations",
            json={"name": "LegWH3", "type": "warehouse", "address": None, "is_default": True},
            headers=admin_h,
        )
        location_id = loc_r.json()["id"]
        r = await client.post(
            "/items",
            json={"sku": "LEG-COST", "name": "Legacy Cost", "quantity": 1,
                  "location_id": location_id, "cost_price": 50.0, "sell_by": "piece"},
            headers=legacy_h,
        )
        assert r.status_code == 403


# ── require_min_role: importable guard ───────────────────────────────────────

class TestRequireMinRole:

    async def test_require_min_role_importable(self, client):
        """require_min_role and ROLE_LEVELS are importable from auth service."""
        from celerp.services.auth import ROLE_LEVELS, require_min_role
        assert "viewer" in ROLE_LEVELS
        assert "operator" in ROLE_LEVELS
        assert "manager" in ROLE_LEVELS
        assert "admin" in ROLE_LEVELS
        assert "owner" in ROLE_LEVELS
        # Levels are strictly increasing
        assert ROLE_LEVELS["viewer"] < ROLE_LEVELS["operator"] < ROLE_LEVELS["manager"]
        assert ROLE_LEVELS["manager"] < ROLE_LEVELS["admin"] < ROLE_LEVELS["owner"]

    async def test_require_min_role_returns_depends(self, client):
        """require_min_role returns a FastAPI Depends object."""
        from fastapi.params import Depends
        from celerp.services.auth import require_min_role
        dep = require_min_role("operator")
        assert isinstance(dep, Depends)

    async def test_legacy_migration_dict(self, client):
        """_ROLE_MIGRATION maps salesperson → operator."""
        from celerp.services.auth import _ROLE_MIGRATION
        assert _ROLE_MIGRATION.get("salesperson") == "operator"



# ── visible_to_roles: cost_price field visibility ─────────────────────────────

class TestFieldVisibility:

    async def test_admin_sees_cost_price_in_list(self, client):
        ctx = await _setup(client)
        r = await client.get("/items", headers=ctx["admin_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert "cost_price" in items[0]

    async def test_manager_sees_cost_price_in_list(self, client):
        ctx = await _setup(client)
        r = await client.get("/items", headers=ctx["manager_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert "cost_price" in items[0]

    async def test_staff_cannot_see_cost_price_in_list(self, client):
        ctx = await _setup(client)
        r = await client.get("/items", headers=ctx["staff_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert "cost_price" not in items[0]

    async def test_admin_sees_cost_price_in_detail(self, client):
        ctx = await _setup(client)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["admin_h"])
        assert r.status_code == 200
        assert "cost_price" in r.json()

    async def test_staff_cannot_see_cost_price_in_detail(self, client):
        ctx = await _setup(client)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["staff_h"])
        assert r.status_code == 200
        assert "cost_price" not in r.json()

    async def test_manager_sees_cost_price_in_detail(self, client):
        ctx = await _setup(client)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["manager_h"])
        assert r.status_code == 200
        assert "cost_price" in r.json()


# ── Write-path: cost_price field write guard ──────────────────────────────────

class TestCostPriceWriteGuard:

    async def test_staff_cannot_set_cost_price_on_create(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items",
            json={"sku": "STAFF-SKU", "name": "Staff Item", "quantity": 1,
                  "location_id": ctx["location_id"], "cost_price": 50.0, "sell_by": "piece"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_can_create_item_without_cost_price(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items",
            json={"sku": "STAFF-SKU2", "name": "Staff Item", "quantity": 1,
                  "location_id": ctx["location_id"], "sell_by": "piece"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_patch_cost_price(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"cost_price": {"old": 100.0, "new": 50.0}}},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_can_patch_non_restricted_field(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"name": {"old": "Perm Item", "new": "Updated"}}},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 200

    async def test_manager_can_patch_cost_price(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"cost_price": {"old": 100.0, "new": 80.0}}},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_admin_can_set_cost_price_on_create(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items",
            json={"sku": "ADMIN-COST", "name": "Admin Item", "quantity": 1,
                  "location_id": ctx["location_id"], "cost_price": 200.0, "sell_by": "piece"},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200


# ── require_manager: destructive item operations ──────────────────────────────

class TestManagerRequiredItemOps:

    async def test_staff_cannot_bulk_delete(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [ctx["item_id"]]},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_bulk_delete(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [ctx["item_id"]]},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_adjust_quantity(self, client):
        ctx = await _setup(client)
        r = await client.post(
            f"/items/{ctx['item_id']}/adjust",
            json={"new_qty": 99},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_adjust_quantity(self, client):
        ctx = await _setup(client)
        r = await client.post(
            f"/items/{ctx['item_id']}/adjust",
            json={"new_qty": 99},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_set_price(self, client):
        ctx = await _setup(client)
        r = await client.post(
            f"/items/{ctx['item_id']}/price",
            json={"price_type": "cost_price", "new_price": 50.0},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_set_price(self, client):
        ctx = await _setup(client)
        r = await client.post(
            f"/items/{ctx['item_id']}/price",
            json={"price_type": "retail_price", "new_price": 150.0},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_dispose_item(self, client):
        ctx = await _setup(client)
        r = await client.post(f"/items/{ctx['item_id']}/dispose", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_dispose_item(self, client):
        ctx = await _setup(client)
        r = await client.post(f"/items/{ctx['item_id']}/dispose", headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_expire_item(self, client):
        ctx = await _setup(client)
        r = await client.post(f"/items/{ctx['item_id']}/expire", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_staff_cannot_access_valuation(self, client):
        ctx = await _setup(client)
        r = await client.get("/items/valuation", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_access_valuation(self, client):
        ctx = await _setup(client)
        r = await client.get("/items/valuation", headers=ctx["manager_h"])
        assert r.status_code == 200


# ── require_manager: financial document operations ────────────────────────────

class TestManagerRequiredDocOps:

    async def _create_draft_doc(self, client, headers: dict) -> str:
        r = await client.post(
            "/docs",
            json={"doc_type": "invoice", "contact_name": "Client", "line_items": []},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        return r.json()["id"]

    async def test_staff_cannot_finalize_doc(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/finalize", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_finalize_doc(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/finalize", headers=ctx["manager_h"])
        assert r.status_code in (200, 409)  # 409 if already finalized

    async def test_staff_cannot_void_doc(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/void",
            json={"reason": "test"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_cannot_record_payment(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/payment",
            json={"amount": 100.0, "method": "cash", "reference": "REF1"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_cannot_refund_payment(self, client):
        ctx = await _setup(client)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/refund",
            json={"amount": 10.0, "method": "cash", "reference": "REF2"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403


# ── require_manager: accounting operations ────────────────────────────────────

class TestManagerRequiredAccountingOps:

    async def test_staff_cannot_create_account(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/accounting/accounts",
            json={"code": "9999", "name": "Test Account", "account_type": "expense"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_create_account(self, client):
        ctx = await _setup(client)
        r = await client.post(
            "/accounting/accounts",
            json={"code": "9998", "name": "Mgr Account", "account_type": "expense"},
            headers=ctx["manager_h"],
        )
        assert r.status_code in (200, 409)  # 409 if code already exists

    async def test_staff_cannot_see_profit_and_loss(self, client):
        ctx = await _setup(client)
        r = await client.get("/accounting/pnl", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_see_profit_and_loss(self, client):
        ctx = await _setup(client)
        r = await client.get("/accounting/pnl", headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_see_balance_sheet(self, client):
        ctx = await _setup(client)
        r = await client.get("/accounting/balance-sheet", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_see_balance_sheet(self, client):
        ctx = await _setup(client)
        r = await client.get("/accounting/balance-sheet", headers=ctx["manager_h"])
        assert r.status_code == 200


# ── require_manager: reports with cost data ───────────────────────────────────

class TestManagerRequiredReports:

    async def test_staff_cannot_access_sales_report(self, client):
        ctx = await _setup(client)
        r = await client.get("/reports/sales", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_access_sales_report(self, client):
        ctx = await _setup(client)
        r = await client.get("/reports/sales", headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_access_purchases_report(self, client):
        ctx = await _setup(client)
        r = await client.get("/reports/purchases", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_admin_can_access_purchases_report(self, client):
        ctx = await _setup(client)
        r = await client.get("/reports/purchases", headers=ctx["admin_h"])
        assert r.status_code == 200

    async def test_staff_can_access_ar_aging(self, client):
        """AR aging is operational data - staff-accessible."""
        ctx = await _setup(client)
        r = await client.get("/reports/ar-aging", headers=ctx["staff_h"])
        assert r.status_code == 200

    async def test_staff_can_access_expiring_items(self, client):
        """Expiring items report - operational, staff-accessible."""
        ctx = await _setup(client)
        r = await client.get("/reports/expiring", headers=ctx["staff_h"])
        assert r.status_code == 200


# ── require_admin: admin-only operations ─────────────────────────────────────

class TestAdminOnlyOps:

    async def test_staff_cannot_patch_company(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me",
            json={"name": "Hacked Co"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_cannot_patch_company(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me",
            json={"name": "Hacked Co"},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_patch_company(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me",
            json={"name": "Renamed Co"},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200

    async def test_manager_cannot_undo_import(self, client):
        ctx = await _setup(client)
        # Non-existent batch — 403 should fire before 404
        r = await client.post("/items/import/batches/fake-id/undo", headers=ctx["manager_h"])
        assert r.status_code == 403

    async def test_manager_cannot_patch_item_schema(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me/item-schema",
            json={"fields": []},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_patch_item_schema(self, client):
        ctx = await _setup(client)
        r = await client.patch(
            "/companies/me/item-schema",
            json={"fields": [
                {"key": "sku", "label": "SKU", "type": "text", "editable": True, "required": True,
                 "options": [], "visible_to_roles": [], "position": 0, "show_in_table": True, "sell_by": "piece"},
            ]},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200


# ── Nav item min_role tests ───────────────────────────────────────────────────

class TestNavMinRole:
    """Module nav items must declare correct min_role for sidebar filtering."""

    def _load_nav_items(self) -> list[dict]:
        """Collect nav items from all default modules."""
        import importlib.util
        import glob
        import os
        items = []
        for init in sorted(glob.glob("default_modules/celerp-*/__init__.py")):
            mod_dir = os.path.dirname(init)
            mod_name = os.path.basename(mod_dir).replace("-", "_")
            spec = importlib.util.spec_from_file_location(mod_name, init)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            manifest = getattr(mod, "PLUGIN_MANIFEST", {})
            nav = manifest.get("slots", {}).get("nav")
            if nav is None:
                continue
            if isinstance(nav, dict):
                nav = [nav]
            items.extend(nav)
        return items

    def test_finance_nav_requires_manager(self):
        for item in self._load_nav_items():
            if item.get("group") == "Finance":
                assert item.get("min_role") == "manager", (
                    f"Finance nav item {item.get('key')} should require manager, got {item.get('min_role')}"
                )

    def test_operational_nav_requires_operator(self):
        operator_groups = {"Sales Documents", "Purchasing Documents", "Inventory", "Contacts"}
        for item in self._load_nav_items():
            group = item.get("group")
            if group in operator_groups:
                assert item.get("min_role") == "operator", (
                    f"Nav item {item.get('key')} in {group} should require operator, got {item.get('min_role')}"
                )

    def test_dashboard_allows_viewer(self):
        for item in self._load_nav_items():
            if item.get("key") == "dashboard":
                min_role = item.get("min_role", "viewer")
                assert min_role == "viewer", f"Dashboard should allow viewer, got {min_role}"


# ── Sidebar role filtering tests ─────────────────────────────────────────────

def _make_jwt(role: str) -> str:
    """Create a minimal JWT with the given role (unsigned, for UI cookie parsing)."""
    import base64
    import json
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"role": role, "sub": "test"}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.nosig"


class TestSidebarRoleFiltering:
    """Sidebar correctly filters nav items by user role."""

    def test_settings_link_visible_for_operator(self):
        """Operators see settings/general (password change lives there)."""
        from ui.components.shell import _sidebar
        sidebar_html = str(_sidebar("dashboard", role="operator"))
        assert 'href="/settings/general"' in sidebar_html

    def test_settings_link_visible_for_manager(self):
        from ui.components.shell import _sidebar
        sidebar_html = str(_sidebar("dashboard", role="manager"))
        assert "/settings/general" in sidebar_html

    def test_settings_link_visible_for_admin(self):
        from ui.components.shell import _sidebar
        sidebar_html = str(_sidebar("dashboard", role="admin"))
        assert "/settings/general" in sidebar_html


# ── UI settings role guard tests ─────────────────────────────────────────────

class TestSettingsRoleGuard:
    """Settings page handlers redirect low-role users to dashboard."""

    def test_check_role_blocks_operator(self):
        from ui.routes.settings import _check_role
        from unittest.mock import MagicMock
        request = MagicMock()
        request.cookies = {"celerp_token": _make_jwt("operator")}
        result = _check_role(request, "admin")
        assert result is not None
        assert result.status_code == 302

    def test_check_role_allows_admin(self):
        from ui.routes.settings import _check_role
        from unittest.mock import MagicMock
        request = MagicMock()
        request.cookies = {"celerp_token": _make_jwt("admin")}
        result = _check_role(request, "admin")
        assert result is None

    def test_check_role_allows_owner(self):
        from ui.routes.settings import _check_role
        from unittest.mock import MagicMock
        request = MagicMock()
        request.cookies = {"celerp_token": _make_jwt("owner")}
        result = _check_role(request, "admin")
        assert result is None

    def test_check_role_manager_for_operational_settings(self):
        from ui.routes.settings import _check_role
        from unittest.mock import MagicMock
        request = MagicMock()
        request.cookies = {"celerp_token": _make_jwt("manager")}
        assert _check_role(request, "manager") is None
        assert _check_role(request, "admin") is not None

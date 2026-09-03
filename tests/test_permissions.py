# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Permission enforcement tests.

Covers:
- visible_to_roles: cost_price hidden from operator on GET /items and GET /items/{id}
- Write-path guard: operator cannot set cost_price on POST /items or PATCH /items/{id}
- manager-default gates: operator blocked from destructive/financial operations
- admin-default gates: operator/manager blocked from admin-only ops
- Admin, owner, and manager can access all guarded routes
- 5-role hierarchy: owner > admin > manager > operator > viewer
- Legacy JWT migration: salesperson → operator
"""
from __future__ import annotations

import pytest


# ── Shared helpers (canonical source: test_helpers.py at repo root) ───────────

from test_helpers import (  # noqa: E402
    create_item as _create_item,
    invite_user as _invite_user,
    perm_setup as _setup,
    register_admin as _register_admin,
)


async def _restrict_item_fields(client, admin_h, keys: set[str]) -> None:
    """Set visible_to_roles on the named item-schema fields so only admin and manager
    see them, through the same company-settings endpoint an admin uses. Everything
    else in the schema is written back unchanged."""
    r = await client.get("/companies/me/item-schema", headers=admin_h)
    assert r.status_code == 200, r.text
    fields = r.json()
    for f in fields:
        if f["key"] in keys:
            f["visible_to_roles"] = ["admin", "manager"]
    pr = await client.patch("/companies/me/item-schema", json={"fields": fields}, headers=admin_h)
    assert pr.status_code == 200, pr.text

class TestManagerRequiredDocOps:

    async def _create_draft_doc(self, client, headers: dict) -> str:
        r = await client.post(
            "/docs",
            json={"doc_type": "invoice", "contact_name": "Client",
                  "line_items": [{"description": "Item", "quantity": 1, "unit_price": 100, "line_total": 100}]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        return r.json()["id"]

    # --- viewer blocked from everything ---

    async def test_viewer_cannot_finalize_doc(self, client, session):
        ctx = await _setup(client, session)
        viewer_tok = await _invite_user(client, session, ctx["admin_h"], "viewer@perm.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/finalize", headers=viewer_h)
        assert r.status_code == 403

    async def test_viewer_cannot_record_payment(self, client, session):
        ctx = await _setup(client, session)
        viewer_tok = await _invite_user(client, session, ctx["admin_h"], "viewer2@perm.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/payment",
            json={"payment_date": "2026-01-15", "amount": 100.0, "method": "cash", "reference": "REF1", "bank_account": "1111"},
            headers=viewer_h,
        )
        assert r.status_code == 403

    # --- operator CAN finalize/void/payments ---

    async def test_operator_can_finalize_doc(self, client, session):
        ctx = await _setup(client, session)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/finalize", headers=ctx["operator_h"])
        assert r.status_code in (200, 409)

    async def test_operator_can_void_doc(self, client, session):
        ctx = await _setup(client, session)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        await client.post(f"/docs/{doc_id}/finalize", headers=ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/void", json={"reason": "operator test"}, headers=ctx["operator_h"])
        assert r.status_code in (200, 409)

    async def test_operator_can_record_payment(self, client, session):
        ctx = await _setup(client, session)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        await client.post(f"/docs/{doc_id}/finalize", headers=ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/payment",
            json={"payment_date": "2026-01-15", "amount": 100.0, "method": "cash", "reference": "REF1", "bank_account": "1111"},
            headers=ctx["operator_h"],
        )
        assert r.status_code in (200, 409)

    # --- operator CANNOT delete ---

    async def test_operator_cannot_delete_doc(self, client, session):
        ctx = await _setup(client, session)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.delete(f"/docs/{doc_id}", headers=ctx["operator_h"])
        assert r.status_code == 403

    # --- manager CAN finalize and delete ---

    async def test_manager_can_finalize_doc(self, client, session):
        ctx = await _setup(client, session)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/finalize", headers=ctx["manager_h"])
        assert r.status_code in (200, 409)

    async def test_manager_can_delete_doc(self, client, session):
        ctx = await _setup(client, session)
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.delete(f"/docs/{doc_id}", headers=ctx["manager_h"])
        assert r.status_code in (200, 404)

    # --- viewer blocked from void/refund ---

    async def test_viewer_cannot_void_doc(self, client, session):
        ctx = await _setup(client, session)
        viewer_tok = await _invite_user(client, session, ctx["admin_h"], "viewer3@perm.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        await client.post(f"/docs/{doc_id}/finalize", headers=ctx["admin_h"])
        r = await client.post(f"/docs/{doc_id}/void", json={"reason": "test"}, headers=viewer_h)
        assert r.status_code == 403

    async def test_viewer_cannot_refund_payment(self, client, session):
        ctx = await _setup(client, session)
        viewer_tok = await _invite_user(client, session, ctx["admin_h"], "viewer4@perm.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        doc_id = await self._create_draft_doc(client, ctx["admin_h"])
        r = await client.post(
            f"/docs/{doc_id}/refund",
            json={"amount": 10.0, "method": "cash", "reference": "REF2"},
            headers=viewer_h,
        )
        assert r.status_code == 403


# ── 5-role hierarchy: owner / admin / manager / operator / viewer ─────────────

class TestRoleHierarchy:

    async def test_owner_can_patch_company(self, client, session):
        """Owner (level 5) meets the admin-default threshold (level 4)."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        # The registered user has role=owner; token carries owner
        r = await client.patch(
            "/companies/me",
            json={"name": "Owner Renamed"},
            headers=admin_h,
        )
        assert r.status_code == 200

    async def test_owner_can_create_user(self, client, session):
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        r = await client.post(
            "/companies/me/users",
            json={"email": "newuser@x.com", "name": "New", "role": "operator", "password": "pw123"},
            headers=admin_h,
        )
        assert r.status_code == 200

    async def test_admin_user_can_patch_company(self, client, session):
        """Explicitly created admin (level 4) also meets the admin-default threshold."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        admin2_tok = await _invite_user(client, session, admin_h, "admin2@x.com", "admin")
        admin2_h = {"Authorization": f"Bearer {admin2_tok}"}
        r = await client.patch(
            "/companies/me",
            json={"name": "Admin2 Renamed"},
            headers=admin2_h,
        )
        assert r.status_code == 200

    async def test_viewer_is_blocked_from_manager_ops(self, client, session):
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
        viewer_tok = await _invite_user(client, session, admin_h, "viewer@x.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [item_id]},
            headers=viewer_h,
        )
        assert r.status_code == 403

    async def test_viewer_can_access_valuation_without_cost(self, client, session):
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        viewer_tok = await _invite_user(client, session, admin_h, "viewer2@x.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        r = await client.get("/items/valuation", headers=viewer_h)
        assert r.status_code == 200
        data = r.json()
        assert "cost_total" not in data
        # no cost/landed price lists should leak
        for name in data.get("price_totals", {}).keys():
            assert name.lower() not in {"cost", "cost price", "landed"}

    async def test_operator_blocked_from_manager_ops(self, client, session):
        """Operator (level 2) cannot perform manager-level operations."""
        ctx = await _setup(client, session)
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [ctx["item_id"]]},
            headers=ctx["operator_h"],
        )
        assert r.status_code == 403

    async def test_viewer_blocked_from_finalize_doc(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            "/docs",
            json={"doc_type": "invoice", "contact_name": "Client", "line_items": []},
            headers=ctx["admin_h"],
        )
        doc_id = r.json()["id"]
        viewer_tok = await _invite_user(client, session, ctx["admin_h"], "vhier@perm.com", "viewer")
        viewer_h = {"Authorization": f"Bearer {viewer_tok}"}
        r2 = await client.post(f"/docs/{doc_id}/finalize", headers=viewer_h)
        assert r2.status_code == 403

    async def test_invalid_role_rejected_on_create_user(self, client, session):
        """Role not in ROLE_LEVELS is rejected with 400."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        r = await client.post(
            "/companies/me/users",
            json={"email": "bad@x.com", "name": "Bad", "role": "superuser", "password": "pw123"},
            headers=admin_h,
        )
        assert r.status_code == 400

    async def test_invalid_role_rejected_on_patch_user(self, client, session):
        """Patching a user to an invalid role returns 400."""
        admin_tok = await _register_admin(client)
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        op_tok = await _invite_user(client, session, admin_h, "patchme@x.com", "operator")
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
        legacy_tok, _ = create_access_token(user_id, company_id, "salesperson")
        return admin_h, legacy_tok

    async def test_salesperson_token_allowed_as_operator(self, client, session):
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

    async def test_salesperson_token_blocked_from_manager_ops(self, client, session):
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

    async def test_salesperson_token_blocked_from_cost_price(self, client, session):
        """Legacy salesperson JWT (migrates to operator) cannot set cost_price on a
        committed item (manager field). A draft is exempt by design, so the guard is
        asserted on an item created as available."""
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
                  "location_id": location_id, "cost_price": 50.0, "sell_by": "piece",
                  "status": "available"},
            headers=legacy_h,
        )
        assert r.status_code == 403


# ── ROLE_LEVELS: the numeric role hierarchy ──────────────────────────────────

class TestRoleLevels:

    async def test_role_levels_strictly_increasing(self, client, session):
        """ROLE_LEVELS carries every role and is strictly increasing."""
        from celerp.services.auth import ROLE_LEVELS
        assert "viewer" in ROLE_LEVELS
        assert "operator" in ROLE_LEVELS
        assert "manager" in ROLE_LEVELS
        assert "admin" in ROLE_LEVELS
        assert "owner" in ROLE_LEVELS
        # Levels are strictly increasing
        assert ROLE_LEVELS["viewer"] < ROLE_LEVELS["operator"] < ROLE_LEVELS["manager"]
        assert ROLE_LEVELS["manager"] < ROLE_LEVELS["admin"] < ROLE_LEVELS["owner"]

    async def test_legacy_migration_dict(self, client, session):
        """_ROLE_MIGRATION maps salesperson → operator."""
        from celerp.services.auth import _ROLE_MIGRATION
        assert _ROLE_MIGRATION.get("salesperson") == "operator"


# ── visible_to_roles: cost_price field visibility ─────────────────────────────

class TestFieldVisibility:

    async def test_admin_sees_cost_price_in_list(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/items", headers=ctx["admin_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert "cost_price" in items[0]

    async def test_manager_sees_cost_price_in_list(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/items", headers=ctx["manager_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert "cost_price" in items[0]

    async def test_staff_cannot_see_cost_price_in_list(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/items", headers=ctx["staff_h"])
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert "cost_price" not in items[0]

    async def test_admin_sees_cost_price_in_detail(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["admin_h"])
        assert r.status_code == 200
        assert "cost_price" in r.json()

    async def test_staff_cannot_see_cost_price_in_detail(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["staff_h"])
        assert r.status_code == 200
        assert "cost_price" not in r.json()

    async def test_manager_sees_cost_price_in_detail(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["manager_h"])
        assert r.status_code == 200
        assert "cost_price" in r.json()

    # ── Derived field: qty_each follows its sources (quantity, pieces) ────────

    async def test_qty_each_hidden_when_quantity_restricted_list(self, client, session):
        """qty_each is derived from quantity, so a role that cannot see quantity must
        not see qty_each either - otherwise the hidden quantity is recoverable from the
        per-piece figure. GET /items strips both for the denied role."""
        ctx = await _setup(client, session)
        await _restrict_item_fields(client, ctx["admin_h"], {"quantity"})
        r = await client.get("/items", headers=ctx["operator_h"])
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert "quantity" not in item
        assert "qty_each" not in item
        # The manager, who may see quantity, still sees the derived figure.
        mr = await client.get("/items", headers=ctx["manager_h"])
        assert "qty_each" in mr.json()["items"][0]

    async def test_qty_each_hidden_when_quantity_restricted_detail(self, client, session):
        """The same rule holds on GET /items/{id}: no qty_each for the denied role."""
        ctx = await _setup(client, session)
        await _restrict_item_fields(client, ctx["admin_h"], {"quantity"})
        r = await client.get(f"/items/{ctx['item_id']}", headers=ctx["operator_h"])
        assert r.status_code == 200
        body = r.json()
        assert "quantity" not in body
        assert "qty_each" not in body

    async def test_qty_each_hidden_when_pieces_restricted(self, client, session):
        """pieces is the other source. Restricting only pieces still hides qty_each,
        because the per-piece figure would otherwise leak the hidden piece count;
        quantity, unrestricted here, stays visible."""
        ctx = await _setup(client, session)
        await _restrict_item_fields(client, ctx["admin_h"], {"pieces"})
        r = await client.get("/items", headers=ctx["operator_h"])
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert "pieces" not in item
        assert "qty_each" not in item
        assert "quantity" in item

    async def test_qty_each_visible_when_both_sources_visible(self, client, session):
        """Regression guard against over-stripping: when neither source is restricted,
        the operator sees qty_each. Green at merge-base; it fails only if the derived
        drop is applied unconditionally."""
        ctx = await _setup(client, session)
        r = await client.get("/items", headers=ctx["operator_h"])
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert "quantity" in item
        assert "qty_each" in item

    async def test_hidden_field_not_searchable_no_oracle(self, client, session):
        """A hidden field must not be searchable: search runs only over fields the role
        may see, so result membership cannot disclose a hidden value. The item has
        quantity 5; a denied-quantity operator gets it back for NO quantity query -
        the true value `qty: 5`, a bare `5`, and a range `qty: 1-10` all return nothing,
        exactly as a wrong value would, so the operator learns nothing (no oracle). A
        role that may see quantity still finds and tags it."""
        ctx = await _setup(client, session)
        await _restrict_item_fields(client, ctx["admin_h"], {"quantity", "pieces"})
        for query in ("qty: 5", "5", "qty: 1-10"):
            r = await client.get("/items", params={"q": query}, headers=ctx["operator_h"])
            assert r.status_code == 200
            assert r.json()["items"] == [], f"hidden quantity leaked via {query!r}"
        # A wrong value returns nothing too - the true value is indistinguishable.
        wrong = await client.get("/items", params={"q": "qty: 6"}, headers=ctx["operator_h"])
        assert wrong.json()["items"] == []
        # The operator can still list the item (without quantity) and search a visible field.
        by_sku = await client.get("/items", params={"q": "sku-perm"}, headers=ctx["operator_h"])
        assert len(by_sku.json()["items"]) == 1
        assert "quantity" not in by_sku.json()["items"][0]
        # The manager may see quantity, so the search matches and tags it.
        mr = await client.get("/items", params={"q": "qty: 5"}, headers=ctx["manager_h"])
        mitems = mr.json()["items"]
        assert len(mitems) == 1
        assert "quantity" in {m["field"] for m in mitems[0].get("q_match", [])}

    async def test_hidden_category_attribute_no_oracle(self, client, session):
        """A category attribute a role may not see must leak through no side channel:
        not the attribute funnel facets, not an attr.* filter, not a scoped q-search.
        An operator denied the `origin` attribute (visible_to_roles set on the category
        schema) gets it absent from attribute_facets, `attr.origin=Kashmir` returns [],
        and `q=origin: Kashmir` returns []; a manager sees all three."""
        ctx = await _setup(client, session)
        # An item in category Gem carrying an origin attribute the operator will be denied.
        cr = await client.post(
            "/items",
            json={"sku": "GEM-1", "name": "Sapphire", "quantity": 2,
                  "location_id": ctx["location_id"], "sell_by": "piece",
                  "status": "available", "category": "Gem", "origin": "Kashmir"},
            headers=ctx["admin_h"],
        )
        assert cr.status_code == 200, cr.text
        # Hide origin from operator via the category schema (admin+manager only).
        pr = await client.patch(
            "/companies/me/category-schema/Gem",
            json={"fields": [{"key": "origin", "label": "Origin", "type": "text",
                              "visible_to_roles": ["admin", "manager"]}]},
            headers=ctx["admin_h"],
        )
        assert pr.status_code == 200, pr.text

        # Operator: no facet, no attr.* membership, no q-search membership.
        of = await client.get("/items", headers=ctx["operator_h"])
        assert of.status_code == 200
        assert "origin" not in of.json()["attribute_facets"]
        oa = await client.get("/items", params={"attr.origin": "Kashmir"}, headers=ctx["operator_h"])
        assert oa.json()["items"] == [], "hidden attribute leaked via attr.* filter"
        oq = await client.get("/items", params={"q": "origin: Kashmir"}, headers=ctx["operator_h"])
        assert oq.json()["items"] == [], "hidden attribute leaked via q-search"

        # Manager: sees the facet, the attr.* match, and the q-search match.
        mf = await client.get("/items", headers=ctx["manager_h"])
        assert "Kashmir" in mf.json()["attribute_facets"].get("origin", [])
        ma = await client.get("/items", params={"attr.origin": "Kashmir"}, headers=ctx["manager_h"])
        assert len(ma.json()["items"]) == 1
        mq = await client.get("/items", params={"q": "origin: Kashmir"}, headers=ctx["manager_h"])
        assert len(mq.json()["items"]) == 1

    async def test_low_stock_filter_no_reorder_oracle(self, client, session):
        """The low_stock filter must not become an oracle over a hidden quantity. An
        operator denied `quantity` requesting ?filter=low_stock gets hidden-quantity
        items excluded, identical whatever the true quantity is (no membership oracle);
        a manager sees the true low-stock set."""
        ctx = await _setup(client, session)
        # An item below its reorder point: quantity 1 <= reorder_point 5.
        cr = await client.post(
            "/items",
            json={"sku": "LOW-1", "name": "Low Item", "quantity": 1,
                  "location_id": ctx["location_id"], "sell_by": "piece",
                  "status": "available", "reorder_point": 5},
            headers=ctx["admin_h"],
        )
        assert cr.status_code == 200, cr.text
        await _restrict_item_fields(client, ctx["admin_h"], {"quantity"})

        # Operator cannot see quantity, so a hidden-quantity item is excluded from the
        # low_stock membership - it cannot be probed for being at/below reorder.
        ol = await client.get("/items", params={"filter": "low_stock"}, headers=ctx["operator_h"])
        assert ol.status_code == 200
        assert all(i.get("sku") != "LOW-1" for i in ol.json()["items"]), \
            "low_stock leaked membership of a hidden-quantity item"
        for i in ol.json()["items"]:
            assert "quantity" not in i
        # Manager sees quantity, so the true low-stock item is present.
        ml = await client.get("/items", params={"filter": "low_stock"}, headers=ctx["manager_h"])
        assert any(i.get("sku") == "LOW-1" for i in ml.json()["items"]), \
            "manager should see the true low-stock item"

    async def test_hidden_category_inventory_type_filter_no_oracle(self, client, session):
        """The category and inventory_type column filters must not become oracles over a
        hidden field. Both are schema fields a role may be denied (visible_to_roles), so the
        filters run over the visibility-stripped records: a denied operator sees the field
        absent, the filter never matches its true value, and a query for the true value is
        indistinguishable from a wrong one (no membership oracle) - the same closure applied
        to q, attr.*, and low_stock. A role that may see the field filters normally."""
        ctx = await _setup(client, session)
        cr = await client.post(
            "/items",
            json={"sku": "SECRET-1", "name": "Classified", "quantity": 3,
                  "location_id": ctx["location_id"], "sell_by": "piece",
                  "status": "available", "category": "Gem", "inventory_type": "service"},
            headers=ctx["admin_h"],
        )
        assert cr.status_code == 200, cr.text
        await _restrict_item_fields(client, ctx["admin_h"], {"category", "inventory_type"})

        # Operator: the true value and a wrong value both return nothing - indistinguishable.
        for param in ({"category": "Gem"}, {"category": "Metal"},
                      {"inventory_type": "service"}, {"inventory_type": "consumable"}):
            r = await client.get("/items", params=param, headers=ctx["operator_h"])
            assert r.status_code == 200
            assert all(i.get("sku") != "SECRET-1" for i in r.json()["items"]), \
                f"hidden field leaked membership via {param}"
        # The operator can still list the item, but sees neither hidden field on it.
        ol = await client.get("/items", params={"q": "classified"}, headers=ctx["operator_h"])
        assert len(ol.json()["items"]) == 1
        assert "category" not in ol.json()["items"][0]
        assert "inventory_type" not in ol.json()["items"][0]
        # The manager may see both, so each true-value filter returns the item.
        mc = await client.get("/items", params={"category": "Gem"}, headers=ctx["manager_h"])
        assert any(i.get("sku") == "SECRET-1" for i in mc.json()["items"])
        mi = await client.get("/items", params={"inventory_type": "service"}, headers=ctx["manager_h"])
        assert any(i.get("sku") == "SECRET-1" for i in mi.json()["items"])

    async def test_visible_numeric_attribute_keeps_facet(self, client, session):
        """A visible numeric category attribute must keep its column-filter funnel. `length`
        is a `number`-typed category attribute, so it folds into the per-item numeric set -
        but it is a category attribute, not a continuous per-item measure (quantity, weight,
        pieces, qty_each), so it must still appear in attribute_facets. A role that may see it
        gets `length` faceted with its value; excluding it (as the numeric set once did)
        would strip a real funnel."""
        ctx = await _setup(client, session)
        pr = await client.patch(
            "/companies/me/category-schema/Gem",
            json={"fields": [{"key": "length", "label": "Length", "type": "number",
                              "visible_to_roles": []}]},
            headers=ctx["admin_h"],
        )
        assert pr.status_code == 200, pr.text
        cr = await client.post(
            "/items",
            json={"sku": "LEN-1", "name": "Rod", "quantity": 2,
                  "location_id": ctx["location_id"], "sell_by": "piece",
                  "status": "available", "category": "Gem", "length": 6.5},
            headers=ctx["admin_h"],
        )
        assert cr.status_code == 200, cr.text
        for h in (ctx["operator_h"], ctx["manager_h"]):
            r = await client.get("/items", headers=h)
            assert r.status_code == 200
            facets = r.json()["attribute_facets"]
            assert "length" in facets, "visible numeric category attribute lost its facet"
            assert "6.5" in facets["length"]


# ── Write-path: cost_price field write guard ──────────────────────────────────

class TestCostPriceWriteGuard:

    async def test_staff_cannot_set_cost_price_on_create(self, client, session):
        # Cost is manager-gated on a committed item. A draft is exempt by design (its
        # edit_inventory author sets cost while drafting; the gate re-arms at
        # make-available), so this guard is asserted on an item created as available.
        # The draft-allowed path is covered in test_item_draft_status.py.
        ctx = await _setup(client, session)
        r = await client.post(
            "/items",
            json={"sku": "STAFF-SKU", "name": "Staff Item", "quantity": 1,
                  "location_id": ctx["location_id"], "cost_price": 50.0, "sell_by": "piece",
                  "status": "available"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_can_create_item_without_cost_price(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            "/items",
            json={"sku": "STAFF-SKU2", "name": "Staff Item", "quantity": 1,
                  "location_id": ctx["location_id"], "sell_by": "piece"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_patch_cost_price(self, client, session):
        ctx = await _setup(client, session)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"cost_price": {"old": 100.0, "new": 50.0}}},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_staff_can_patch_non_restricted_field(self, client, session):
        ctx = await _setup(client, session)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"name": {"old": "Perm Item", "new": "Updated"}}},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 200

    async def test_manager_can_patch_cost_price(self, client, session):
        ctx = await _setup(client, session)
        r = await client.patch(
            f"/items/{ctx['item_id']}",
            json={"fields_changed": {"cost_price": {"old": 100.0, "new": 80.0}}},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_admin_can_set_cost_price_on_create(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            "/items",
            json={"sku": "ADMIN-COST", "name": "Admin Item", "quantity": 1,
                  "location_id": ctx["location_id"], "cost_price": 200.0, "sell_by": "piece"},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200


# ── manager-default gates: destructive item operations ──────────────────────────────

class TestManagerRequiredItemOps:

    async def test_staff_cannot_bulk_delete(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [ctx["item_id"]]},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_bulk_delete(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            "/items/bulk/delete",
            json={"entity_ids": [ctx["item_id"]]},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_adjust_quantity(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            f"/items/{ctx['item_id']}/adjust",
            json={"new_qty": 99},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_adjust_quantity(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            f"/items/{ctx['item_id']}/adjust",
            json={"new_qty": 99},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_set_price(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            f"/items/{ctx['item_id']}/price",
            json={"price_type": "cost_price", "new_price": 50.0},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_set_price(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            f"/items/{ctx['item_id']}/price",
            json={"price_type": "retail_price", "new_price": 150.0},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 200

    async def test_staff_cannot_archive_item(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post("/items/bulk/status", json={"entity_ids": [ctx["item_id"]], "status": "archived"}, headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_archive_item(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post("/items/bulk/status", json={"entity_ids": [ctx["item_id"]], "status": "archived"}, headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_expire_item(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(f"/items/{ctx['item_id']}/expire", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_operator_can_access_valuation_without_cost(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/items/valuation", headers=ctx["staff_h"])
        assert r.status_code == 200
        data = r.json()
        assert "cost_total" not in data
        for name in data.get("price_totals", {}):
            assert name.lower() not in ("cost", "cost price", "landed")

    async def test_manager_can_access_valuation(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/items/valuation", headers=ctx["manager_h"])
        assert r.status_code == 200


# ── manager-default gates: accounting operations ────────────────────────────────────

class TestManagerRequiredAccountingOps:

    async def test_staff_cannot_create_account(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            "/accounting/accounts",
            json={"code": "9999", "name": "Test Account", "account_type": "expense"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_can_create_account(self, client, session):
        ctx = await _setup(client, session)
        r = await client.post(
            "/accounting/accounts",
            json={"code": "9998", "name": "Mgr Account", "account_type": "expense"},
            headers=ctx["manager_h"],
        )
        assert r.status_code in (200, 409)  # 409 if code already exists

    async def test_staff_cannot_see_profit_and_loss(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/accounting/pnl", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_see_profit_and_loss(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/accounting/pnl", headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_see_balance_sheet(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/accounting/balance-sheet", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_see_balance_sheet(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/accounting/balance-sheet", headers=ctx["manager_h"])
        assert r.status_code == 200


# ── manager-default gates: reports with cost data ───────────────────────────────────

class TestManagerRequiredReports:

    async def test_staff_cannot_access_sales_report(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/reports/sales", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_manager_can_access_sales_report(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/reports/sales", headers=ctx["manager_h"])
        assert r.status_code == 200

    async def test_staff_cannot_access_purchases_report(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/reports/purchases", headers=ctx["staff_h"])
        assert r.status_code == 403

    async def test_admin_can_access_purchases_report(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/reports/purchases", headers=ctx["admin_h"])
        assert r.status_code == 200

    async def test_staff_can_access_ar_aging(self, client, session):
        """AR aging is operational data - staff-accessible."""
        ctx = await _setup(client, session)
        r = await client.get("/reports/ar-aging", headers=ctx["staff_h"])
        assert r.status_code == 200

    async def test_staff_can_access_expiring_items(self, client, session):
        """Expiring items report - operational, staff-accessible."""
        ctx = await _setup(client, session)
        r = await client.get("/reports/expiring", headers=ctx["staff_h"])
        assert r.status_code == 200


# ── admin-default gates: admin-only operations ─────────────────────────────────────

class TestAdminOnlyOps:

    async def test_staff_cannot_patch_company(self, client, session):
        ctx = await _setup(client, session)
        r = await client.patch(
            "/companies/me",
            json={"name": "Hacked Co"},
            headers=ctx["staff_h"],
        )
        assert r.status_code == 403

    async def test_manager_cannot_patch_company(self, client, session):
        ctx = await _setup(client, session)
        r = await client.patch(
            "/companies/me",
            json={"name": "Hacked Co"},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_patch_company(self, client, session):
        ctx = await _setup(client, session)
        r = await client.patch(
            "/companies/me",
            json={"name": "Renamed Co"},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200

    async def test_manager_cannot_undo_import(self, client, session):
        ctx = await _setup(client, session)
        # Non-existent batch — 403 should fire before 404
        r = await client.post("/items/import/batches/fake-id/undo", headers=ctx["manager_h"])
        assert r.status_code == 403

    async def test_manager_cannot_patch_item_schema(self, client, session):
        ctx = await _setup(client, session)
        r = await client.patch(
            "/companies/me/item-schema",
            json={"fields": []},
            headers=ctx["manager_h"],
        )
        assert r.status_code == 403

    async def test_admin_can_patch_item_schema(self, client, session):
        ctx = await _setup(client, session)
        r = await client.patch(
            "/companies/me/item-schema",
            json={"fields": [
                {"key": "sku", "label": "SKU", "type": "text", "editable": True, "required": True,
                 "options": [], "visible_to_roles": [], "position": 0, "show_in_table": True, "sell_by": "piece"},
            ]},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 200


# ── Nav item permission tests ─────────────────────────────────────────────────

class TestNavPermissions:
    """Module nav items must declare the correct permission key for sidebar filtering."""

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

    def _default_role(self, key: str) -> str:
        from celerp.services.permissions import _PERMISSIONS_BY_KEY
        return _PERMISSIONS_BY_KEY[key].default_role

    def test_finance_nav_requires_manager(self):
        for item in self._load_nav_items():
            if item.get("group") == "Finance":
                perm = item.get("permission")
                assert perm, f"Finance nav item {item.get('key')} must declare a permission"
                assert self._default_role(perm) == "manager", (
                    f"Finance nav item {item.get('key')} should gate on a manager permission, got {perm}"
                )

    def test_document_and_contact_nav_allows_viewer(self):
        """The matrix promises 'View documents & contacts' at viewer level, so the
        document/contact sections must be visible to viewers. Writing is still
        blocked by the operator DEFAULT on the edit permissions unless an owner
        grants it - visibility is not capability."""
        viewer_groups = {"Sales Documents", "Purchasing Documents", "Contacts"}
        inventory_viewer_keys = {"inventory", "inventory_sold", "inventory_archived"}
        for item in self._load_nav_items():
            group = item.get("group")
            key = item.get("key")
            perm = item.get("permission")
            if group in viewer_groups:
                assert perm and self._default_role(perm) == "viewer", (
                    f"Nav item {key} in {group} should allow viewer, got {perm}"
                )
            elif key in inventory_viewer_keys:
                assert perm and self._default_role(perm) == "viewer", (
                    f"Nav item {key} in Inventory should allow viewer, got {perm}"
                )

    def test_dashboard_allows_viewer(self):
        for item in self._load_nav_items():
            if item.get("key") == "dashboard":
                perm = item.get("permission")
                if perm:
                    assert self._default_role(perm) == "viewer", (
                        f"Dashboard should allow viewer, got {perm}"
                    )


# ── Sidebar role filtering tests ─────────────────────────────────────────────

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


# ── Activity/ledger cost redaction (H3) ───────────────────────────────────────

class TestActivityCostRedaction:
    """Cost amounts must never ship to under-manager roles via the ledger/activity feed."""

    @pytest.mark.asyncio
    async def test_manager_sees_cost_in_ledger(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/ledger", params={"entity_id": ctx["item_id"]}, headers=ctx["manager_h"])
        assert r.status_code == 200, r.text
        cost = [e for e in r.json()["items"]
                if e["event_type"] == "item.pricing.set" and e["data"].get("price_type") in ("cost_total", "cost_price")]
        assert cost and any("new_price" in e["data"] for e in cost), "manager must see cost amounts"

    @pytest.mark.asyncio
    async def test_operator_cost_redacted_in_ledger(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/ledger", params={"entity_id": ctx["item_id"]}, headers=ctx["operator_h"])
        assert r.status_code == 200, r.text
        cost = [e for e in r.json()["items"]
                if e["event_type"] == "item.pricing.set" and e["data"].get("price_type") in ("cost_total", "cost_price")]
        assert cost, "cost rows are redacted in place, not removed (pagination integrity)"
        for e in cost:
            assert "new_price" not in e["data"], "operator must not receive the cost amount"
            assert e["data"].get("cost_redacted") is True
        # Nothing anywhere in the operator payload should carry a cost amount.
        import json as _json
        blob = _json.dumps(r.json())
        assert '"cost_total"' not in blob or '"new_price"' not in blob

    @pytest.mark.asyncio
    async def test_operator_sees_noncost_events(self, client, session):
        ctx = await _setup(client, session)
        r = await client.get("/ledger", params={"entity_id": ctx["item_id"]}, headers=ctx["operator_h"])
        # The item.created event is still visible to operators.
        assert any(e["event_type"] == "item.created" for e in r.json()["items"])

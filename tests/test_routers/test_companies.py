# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import pytest


async def _headers(client) -> dict:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Acme", "email": "admin@acme.com", "name": "Admin", "password": "pw"},
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_companies_me_patch_and_locations(client):
    headers = await _headers(client)

    r = await client.get("/companies/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["name"] == "Acme"

    r = await client.patch("/companies/me", json={"settings": {"a": 1}}, headers=headers)
    assert r.status_code == 200

    r = await client.post(
        "/companies/me/locations",
        json={"name": "Main", "type": "warehouse", "address": {"x": 1}, "is_default": True},
        headers=headers,
    )
    assert r.status_code == 200

    r = await client.get("/companies/me/locations", headers=headers)
    assert r.status_code == 200
    # Registration seeds "Head Office" + we added "Main" = 2 locations
    assert len(r.json()["items"]) >= 1
    names = [loc["name"] for loc in r.json()["items"]]
    assert "Main" in names


@pytest.mark.asyncio
async def test_companies_requires_auth(client):
    r = await client.get("/companies/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_patch_user_last_owner_guard(client):
    """Cannot demote the last active owner."""
    headers = await _headers(client)

    # Get own user id
    me = await client.get("/companies/me/users", headers=headers)
    assert me.status_code == 200
    users = me.json()["items"]
    owner = next(u for u in users if u["role"] == "owner")
    owner_id = owner["id"]

    # Demoting the only owner must fail
    r = await client.patch(f"/companies/me/users/{owner_id}", json={"role": "admin"}, headers=headers)
    assert r.status_code == 400
    assert "last owner" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_user_demote_admin_allowed(client):
    """Can demote an admin when they are not the last owner."""
    headers = await _headers(client)

    # Invite a second user and make them admin
    import uuid as _uuid
    email2 = f"admin2-{_uuid.uuid4().hex[:8]}@test.com"
    r = await client.post("/companies/me/users", json={"email": email2, "name": "Admin2", "password": "pw", "role": "admin"}, headers=headers)
    assert r.status_code == 200
    user2_id = r.json()["id"]

    # Now demoting the second admin should succeed
    r = await client.patch(f"/companies/me/users/{user2_id}", json={"role": "operator"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_demo_reseed_vertical(client):
    """POST /companies/me/demo/reseed seeds vertical-aware items."""
    headers = await _headers(client)

    # Set vertical to gemstones
    await client.patch("/companies/me", json={"settings": {"vertical": "gemstones"}}, headers=headers)

    r = await client.post("/companies/me/demo/reseed", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["vertical"] == "gemstones"

    # Items should now include gemstone demo items
    items = (await client.get("/items", headers=headers)).json()["items"]
    skus = {i["sku"] for i in items}
    assert "DEMO-DIA-001" in skus
    assert "DEMO-RUB-001" in skus
    assert "DEMO-JWL-001" in skus


@pytest.mark.asyncio
async def test_demo_reseed_no_vertical(client):
    """POST /companies/me/demo/reseed with no vertical seeds generic items."""
    headers = await _headers(client)

    r = await client.post("/companies/me/demo/reseed", headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_demo_reseed_full_wizard_flow(client):
    """Simulate full wizard flow: register (seeds generic), set vertical, reseed.
    DEMO-001 must be gone and gemstone items must appear."""
    headers = await _headers(client)

    # Registration seeds DEMO-001
    items = (await client.get("/items", headers=headers)).json()["items"]
    skus = {i["sku"] for i in items}
    assert "DEMO-001" in skus, "Registration should seed DEMO-001"

    # Wizard step: save vertical to company settings
    company = (await client.get("/companies/me", headers=headers)).json()
    settings = dict(company.get("settings") or {})
    settings["vertical"] = "gemstones"
    await client.patch("/companies/me", json={"name": company.get("name", "Acme"), "settings": settings}, headers=headers)

    # Wizard step: reseed demo items
    r = await client.post("/companies/me/demo/reseed", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["vertical"] == "gemstones"
    assert body["wiped"] >= 1  # DEMO-001 was wiped

    # DEMO-001 must be gone, gemstone items must appear
    items = (await client.get("/items", headers=headers)).json()["items"]
    skus = {i["sku"] for i in items}
    assert "DEMO-001" not in skus, "DEMO-001 should be wiped after reseed"
    assert "DEMO-DIA-001" in skus, "Diamond demo item should appear after gemstones reseed"
    assert "DEMO-JWL-001" in skus, "Jewelry demo item should appear after gemstones reseed"


@pytest.mark.asyncio
async def test_create_company_seeds_self_contacts(client):
    """POST /companies seeds customer + vendor self-contacts for the new company."""
    # Register first (to get a user with name + email)
    r = await client.post(
        "/auth/register",
        json={"company_name": "ContactSeedCo", "email": "owner@seedtest.com", "name": "Seed Owner", "password": "pw"},
    )
    assert r.status_code == 200
    orig_token = r.json()["access_token"]
    orig_headers = {"Authorization": f"Bearer {orig_token}"}

    # Create a second company for the same user
    r2 = await client.post("/companies", json={"name": "ContactSeedCo2"}, headers=orig_headers)
    assert r2.status_code == 200, r2.text
    new_token = r2.json()["access_token"]
    new_headers = {"Authorization": f"Bearer {new_token}"}

    # New company should have customer + vendor contacts seeded
    contacts_r = await client.get("/crm/contacts", headers=new_headers)
    assert contacts_r.status_code == 200
    contacts = contacts_r.json()["items"]
    types = {c["contact_type"] for c in contacts}
    assert "customer" in types, f"Expected customer contact seeded, got types={types}"
    assert "vendor" in types, f"Expected vendor contact seeded, got types={types}"

    # Contacts should carry the owner's email and name
    emails = {c.get("email") for c in contacts}
    assert "owner@seedtest.com" in emails, f"Owner email missing from seeded contacts: {emails}"


@pytest.mark.asyncio
async def test_create_company_seeds_head_office_location(client):
    """POST /companies seeds a default Head Office location for the new company."""
    r = await client.post(
        "/auth/register",
        json={"company_name": "LocSeedCo", "email": "owner@locseed.com", "name": "Loc Owner", "password": "pw"},
    )
    orig_token = r.json()["access_token"]
    orig_headers = {"Authorization": f"Bearer {orig_token}"}

    r2 = await client.post("/companies", json={"name": "LocSeedCo2"}, headers=orig_headers)
    assert r2.status_code == 200, r2.text
    new_token = r2.json()["access_token"]
    new_headers = {"Authorization": f"Bearer {new_token}"}

    locs_r = await client.get("/companies/me/locations", headers=new_headers)
    assert locs_r.status_code == 200
    locs = locs_r.json()["items"]
    names = [loc["name"] for loc in locs]
    assert "Head Office" in names, f"Expected Head Office location seeded, got: {names}"
    default_locs = [loc for loc in locs if loc.get("is_default")]
    assert default_locs, "Expected a default location to exist"


@pytest.mark.asyncio
async def test_deactivate_frees_slug_for_recreate(client):
    """Deactivating a company must free its slug so a new company with the same name can be created."""
    r = await client.post(
        "/auth/register",
        json={"company_name": "Gems Co", "email": "owner@gemsco.com", "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Deactivate the company
    r2 = await client.delete("/companies/me", headers=headers)
    assert r2.status_code == 200, r2.text

    # Create a new company with the same name - must succeed (slug freed)
    r3 = await client.post("/companies", json={"name": "Gems Co"}, headers=headers)
    assert r3.status_code == 200, f"Expected 200 after deactivation freed slug, got {r3.status_code}: {r3.text}"

    # Verify the new company is active
    new_token = r3.json()["access_token"]
    r4 = await client.get("/companies/me", headers={"Authorization": f"Bearer {new_token}"})
    assert r4.json()["name"] == "Gems Co"


@pytest.mark.asyncio
async def test_reactivate_restores_slug(client):
    """Reactivating a company must strip the -deactivated-{ts} suffix from the slug."""
    r = await client.post(
        "/auth/register",
        json={"company_name": "SlugTest Co", "email": "owner@slugtest.com", "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r2 = await client.get("/companies/me", headers=headers)
    original_slug = r2.json()["slug"]

    # Deactivate - slug gains suffix
    await client.delete("/companies/me", headers=headers)

    # Reactivate - slug should be restored
    r3 = await client.post("/companies/me/reactivate", headers=headers)
    assert r3.status_code == 200, r3.text

    r4 = await client.get("/companies/me", headers=headers)
    assert r4.json()["slug"] == original_slug, (
        f"Expected slug restored to '{original_slug}', got '{r4.json()['slug']}'"
    )


@pytest.mark.asyncio
async def test_create_company_token_has_owner_role_and_email(client):
    """POST /companies must return a JWT with role=owner and email set.

    Bug: create_access_token was called with role='admin' and no email,
    causing the logout button and danger zone to disappear after creating
    a company via the deactivation flow.
    """
    import base64
    import json as _json

    r = await client.post(
        "/auth/register",
        json={"company_name": "TokenTest", "email": "token@test.com", "name": "Tester", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    orig_token = r.json()["access_token"]

    r2 = await client.post(
        "/companies",
        json={"name": "Second Co"},
        headers={"Authorization": f"Bearer {orig_token}"},
    )
    assert r2.status_code == 200, r2.text
    new_token = r2.json()["access_token"]

    payload_b64 = new_token.split(".")[1]
    claims = _json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))

    assert claims.get("role") == "owner", f"Expected role=owner, got {claims.get('role')!r}"
    assert claims.get("email") == "token@test.com", f"Expected email in token, got {claims.get('email')!r}"


# ── Category CRUD ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_category_success(client):
    headers = await _headers(client)
    r = await client.post("/companies/me/categories", json={"name": "Gold Rings"}, headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["key"] == "gold_rings"
    # Verify it's in settings
    r2 = await client.get("/companies/me/company-category-schemas", headers=headers)
    assert "gold_rings" in r2.json()


@pytest.mark.asyncio
async def test_create_category_duplicate_409(client):
    headers = await _headers(client)
    await client.post("/companies/me/categories", json={"name": "Gold Rings"}, headers=headers)
    r = await client.post("/companies/me/categories", json={"name": "Gold Rings"}, headers=headers)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_rename_category_updates_items(client):
    """Rename must update category key in category_schemas AND in all item projections."""
    headers = await _headers(client)
    # Create category
    await client.post("/companies/me/categories", json={"name": "Old Name"}, headers=headers)
    # Create item referencing that category
    r = await client.post(
        "/items",
        json={"name": "Test Item", "quantity": 1, "sell_by": "piece", "category": "old_name", "sku": "RENAME-TEST-001"},
        headers=headers,
    )
    assert r.status_code in (200, 201), r.text
    item_id = r.json()["id"]
    # Rename
    r2 = await client.patch(
        "/companies/me/categories/old_name",
        json={"name": "New Name"},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    assert r2.json()["items_updated"] >= 1
    # Verify schema key updated
    schemas = (await client.get("/companies/me/company-category-schemas", headers=headers)).json()
    assert "new_name" in schemas
    assert "old_name" not in schemas
    # Verify item projection updated
    r3 = await client.get(f"/items/{item_id}", headers=headers)
    assert r3.json().get("category") == "new_name"


@pytest.mark.asyncio
async def test_delete_category_with_items_409(client):
    headers = await _headers(client)
    await client.post("/companies/me/categories", json={"name": "Gems"}, headers=headers)
    await client.post(
        "/items",
        json={"name": "Ruby", "quantity": 1, "sell_by": "piece", "category": "gems", "sku": "DELETE-TEST-001"},
        headers=headers,
    )
    r = await client.delete("/companies/me/categories/gems", headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"]["item_count"] >= 1


@pytest.mark.asyncio
async def test_delete_category_empty_ok(client):
    headers = await _headers(client)
    await client.post("/companies/me/categories", json={"name": "Empty Cat"}, headers=headers)
    r = await client.delete("/companies/me/categories/empty_cat", headers=headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    schemas = (await client.get("/companies/me/company-category-schemas", headers=headers)).json()
    assert "empty_cat" not in schemas

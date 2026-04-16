# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client, email="admin@test.com", company="Test Co"):
    r = await client.post(
        "/auth/register",
        json={"company_name": company, "email": email, "name": "Admin", "password": "pw"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Multi-company: my-companies + switch-company
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_my_companies_lists_registered_company(client):
    token = await _register(client)
    r = await client.get("/auth/my-companies", headers=_auth(token))
    assert r.status_code == 200
    companies = r.json()["items"]
    assert len(companies) == 1
    assert companies[0]["role"] == "owner"
    assert companies[0]["company_name"] == "Test Co"


@pytest.mark.asyncio
async def test_switch_company_own(client):
    """User can switch back to their own company (trivial but validates the flow)."""
    token = await _register(client)
    companies = (await client.get("/auth/my-companies", headers=_auth(token))).json()["items"]
    company_id = companies[0]["company_id"]

    r = await client.post(f"/auth/switch-company/{company_id}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["access_token"]


@pytest.mark.asyncio
async def test_switch_company_denied_for_other(client):
    """User cannot switch to a company they don't belong to (random UUID)."""
    import uuid
    token = await _register(client)
    fake_id = str(uuid.uuid4())
    r = await client.post(f"/auth/switch-company/{fake_id}", headers=_auth(token))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Item schema
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_item_schema_default(client):
    token = await _register(client)
    r = await client.get("/companies/me/item-schema", headers=_auth(token))
    assert r.status_code == 200
    schema = r.json()
    assert isinstance(schema, list)
    assert len(schema) > 0
    keys = {f["key"] for f in schema}
    assert "sku" in keys
    assert "name" in keys
    assert "status" in keys


@pytest.mark.asyncio
async def test_item_schema_patch_and_retrieve(client):
    token = await _register(client)
    new_schema = [
        {"key": "sku", "label": "SKU", "type": "text", "editable": True, "required": True, "options": [], "visible_to_roles": [], "position": 0},
        {"key": "name", "label": "Name", "type": "text", "editable": True, "required": True, "options": [], "visible_to_roles": [], "position": 1},
        {"key": "stone_type", "label": "Type", "type": "select", "editable": True, "required": False, "options": ["emerald", "ruby", "sapphire"], "visible_to_roles": [], "position": 2},
        {"key": "weight_ct", "label": "Weight", "type": "weight", "editable": True, "required": False, "options": [], "visible_to_roles": [], "position": 3},
        {"key": "cost_price", "label": "Cost", "type": "money", "editable": True, "required": False, "options": [], "visible_to_roles": ["admin", "manager"], "position": 4},
        {"key": "status", "label": "Status", "type": "status", "editable": True, "required": False, "options": ["available", "sold"], "visible_to_roles": [], "position": 5},
    ]
    r = await client.patch("/companies/me/item-schema", headers=_auth(token), json={"fields": new_schema})
    assert r.status_code == 200
    assert r.json()["field_count"] == 6

    r2 = await client.get("/companies/me/item-schema", headers=_auth(token))
    fields = r2.json()
    # merge adds any missing defaults, so total >= the 6 we stored
    assert len(fields) >= 6
    assert any(f["key"] == "stone_type" for f in fields)
    assert next(f for f in fields if f["key"] == "stone_type")["options"] == ["emerald", "ruby", "sapphire"]


@pytest.mark.asyncio
async def test_item_schema_merge_adds_missing_defaults(client):
    """Stored schema missing location_name gets it appended on GET without losing custom fields."""
    token = await _register(client)
    # Store a schema that deliberately omits location_name
    partial = [
        {"key": "sku", "label": "SKU", "type": "text", "editable": True, "required": True, "options": [], "visible_to_roles": [], "position": 0},
        {"key": "name", "label": "Name", "type": "text", "editable": True, "required": True, "options": [], "visible_to_roles": [], "position": 1},
    ]
    r = await client.patch("/companies/me/item-schema", headers=_auth(token), json={"fields": partial})
    assert r.status_code == 200

    r2 = await client.get("/companies/me/item-schema", headers=_auth(token))
    fields = r2.json()
    keys = [f["key"] for f in fields]
    # stored fields preserved
    assert "sku" in keys
    assert "name" in keys
    # missing defaults merged in
    assert "location_name" in keys
    assert "status" in keys
    # total > 2 (defaults were appended)
    assert len(fields) > 2


@pytest.mark.asyncio
async def test_item_schema_merge_no_duplicates(client):
    """If stored schema already contains all defaults, merge must not add duplicates."""
    token = await _register(client)
    # Fresh company — GET returns full defaults, then PATCH it back verbatim
    r1 = await client.get("/companies/me/item-schema", headers=_auth(token))
    defaults = r1.json()
    r2 = await client.patch("/companies/me/item-schema", headers=_auth(token), json={"fields": defaults})
    assert r2.status_code == 200

    r3 = await client.get("/companies/me/item-schema", headers=_auth(token))
    fields = r3.json()
    keys = [f["key"] for f in fields]
    # no duplicate keys
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Tax rates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tax_rates_default(client):
    token = await _register(client)
    r = await client.get("/companies/me/taxes", headers=_auth(token))
    assert r.status_code == 200
    taxes = r.json()
    assert len(taxes) >= 1
    defaults = [t for t in taxes if t["is_default"]]
    assert len(defaults) >= 1


@pytest.mark.asyncio
async def test_tax_rates_patch(client):
    token = await _register(client)
    new_taxes = [
        {"name": "VAT 10%", "rate": 10.0, "tax_type": "both", "is_default": True, "description": "Custom VAT"},
        {"name": "Zero Rated", "rate": 0.0, "tax_type": "sales", "is_default": False, "description": ""},
    ]
    r = await client.patch("/companies/me/taxes", headers=_auth(token), json={"taxes": new_taxes})
    assert r.status_code == 200

    r2 = await client.get("/companies/me/taxes", headers=_auth(token))
    taxes = r2.json()
    assert len(taxes) == 2
    assert taxes[0]["rate"] == 10.0
    assert taxes[1]["name"] == "Zero Rated"


# ---------------------------------------------------------------------------
# Payment terms
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payment_terms_default(client):
    token = await _register(client)
    r = await client.get("/companies/me/payment-terms", headers=_auth(token))
    assert r.status_code == 200
    terms = r.json()
    assert len(terms) >= 4
    names = {t["name"] for t in terms}
    assert "Pay in Advance" in names
    assert "Net 30" in names


@pytest.mark.asyncio
async def test_payment_terms_patch(client):
    token = await _register(client)
    new_terms = [
        {"name": "COD", "days": 0, "description": "Cash on delivery"},
        {"name": "Net 45", "days": 45, "description": ""},
    ]
    r = await client.patch("/companies/me/payment-terms", headers=_auth(token), json={"terms": new_terms})
    assert r.status_code == 200

    r2 = await client.get("/companies/me/payment-terms", headers=_auth(token))
    terms = r2.json()
    assert len(terms) == 2
    assert terms[0]["name"] == "COD"


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_users(client):
    token = await _register(client)
    r = await client.get("/companies/me/users", headers=_auth(token))
    assert r.status_code == 200
    users = r.json()["items"]
    assert len(users) >= 1
    assert any(u["role"] in ("admin", "owner") for u in users)


@pytest.mark.asyncio
async def test_create_and_patch_user(client):
    token = await _register(client)
    # Create a salesperson
    r = await client.post(
        "/companies/me/users",
        headers=_auth(token),
        json={"email": "sales@test.com", "name": "Sales Person", "role": "operator", "password": "pass"},
    )
    assert r.status_code == 200
    user_id = r.json()["id"]

    # Patch: deactivate
    r2 = await client.patch(
        f"/companies/me/users/{user_id}",
        headers=_auth(token),
        json={"is_active": False},
    )
    assert r2.status_code == 200

    # Verify deactivated
    users = (await client.get("/companies/me/users", headers=_auth(token))).json()["items"]
    sales_user = next(u for u in users if u["email"] == "sales@test.com")
    assert sales_user["is_active"] is False


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chart_seeded_on_register(client):
    token = await _register(client)
    r = await client.get("/accounting/chart", headers=_auth(token))
    assert r.status_code == 200
    accounts = r.json()["items"]
    assert len(accounts) >= 40
    codes = {a["code"] for a in accounts}
    assert "1110" in codes  # Cash
    assert "1120" in codes  # Accounts Receivable
    assert "4100" in codes  # Sales Revenue
    assert "2110" in codes  # Accounts Payable


@pytest.mark.asyncio
async def test_create_account(client):
    token = await _register(client)
    r = await client.post(
        "/accounting/accounts",
        headers=_auth(token),
        json={"code": "7001", "name": "R&D Expenses", "account_type": "expense", "parent_code": "6000"},
    )
    assert r.status_code == 200
    acc = r.json()
    assert acc["code"] == "7001"
    assert acc["account_type"] == "expense"


@pytest.mark.asyncio
async def test_create_account_duplicate_code_rejected(client):
    token = await _register(client)
    r = await client.post(
        "/accounting/accounts",
        headers=_auth(token),
        json={"code": "1110", "name": "Duplicate Cash", "account_type": "asset"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_patch_account(client):
    token = await _register(client)
    r = await client.patch(
        "/accounting/accounts/1110",
        headers=_auth(token),
        json={"name": "Cash and Bank"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Cash and Bank"


@pytest.mark.asyncio
async def test_trial_balance_empty(client):
    """Trial balance with no journal entries should return empty lines with balanced=True."""
    token = await _register(client)
    r = await client.get("/accounting/trial-balance", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["balanced"] is True
    assert data["lines"] == []
    assert data["total_debit"] == 0.0
    assert data["total_credit"] == 0.0


@pytest.mark.asyncio
async def test_pnl_empty(client):
    token = await _register(client)
    r = await client.get("/accounting/pnl", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["net_profit"] == 0.0
    assert data["revenue"]["total"] == 0.0
    assert data["gross_profit"] == 0.0


@pytest.mark.asyncio
async def test_balance_sheet_empty(client):
    token = await _register(client)
    r = await client.get("/accounting/balance-sheet", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["balanced"] is True
    assert data["assets"]["total"] == 0.0


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_subscription(client):
    token = await _register(client)
    r = await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={
            "name": "Monthly Rent Invoice",
            "contact_id": "contact:abc123",
            "doc_type": "invoice",
            "frequency": "monthly",
            "start_date": "2026-02-01",
            "line_items": [{"description": "Office Rent", "quantity": 1, "unit_price": 50000}],
            "payment_terms": "Net 30",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert "id" in data
    assert data["id"].startswith("sub:")
    assert data["next_run"]  # computed from start_date + monthly


@pytest.mark.asyncio
async def test_list_subscriptions(client):
    token = await _register(client)
    await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={"name": "Sub A", "doc_type": "invoice", "frequency": "weekly", "start_date": "2026-02-01"},
    )
    await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={"name": "Sub B", "doc_type": "purchase_order", "frequency": "monthly", "start_date": "2026-02-01"},
    )
    r = await client.get("/subscriptions", headers=_auth(token))
    assert r.status_code == 200
    subs = r.json()["items"]
    assert len(subs) == 2
    names = {s["name"] for s in subs}
    assert {"Sub A", "Sub B"} == names


@pytest.mark.asyncio
async def test_get_subscription(client):
    token = await _register(client)
    created = (await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={"name": "Weekly PO", "doc_type": "purchase_order", "frequency": "weekly", "start_date": "2026-02-01"},
    )).json()
    entity_id = created["id"]

    r = await client.get(f"/subscriptions/{entity_id}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["name"] == "Weekly PO"
    assert r.json()["status"] == "active"


@pytest.mark.asyncio
async def test_pause_and_resume_subscription(client):
    token = await _register(client)
    entity_id = (await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={"name": "To Pause", "doc_type": "invoice", "frequency": "monthly", "start_date": "2026-02-01"},
    )).json()["id"]

    # Pause
    r = await client.post(f"/subscriptions/{entity_id}/pause", headers=_auth(token))
    assert r.status_code == 200
    assert (await client.get(f"/subscriptions/{entity_id}", headers=_auth(token))).json()["status"] == "paused"

    # Resume
    r2 = await client.post(f"/subscriptions/{entity_id}/resume", headers=_auth(token))
    assert r2.status_code == 200
    data = (await client.get(f"/subscriptions/{entity_id}", headers=_auth(token))).json()
    assert data["status"] == "active"
    assert data["next_run"]


@pytest.mark.asyncio
async def test_pause_already_paused_is_conflict(client):
    token = await _register(client)
    entity_id = (await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={"name": "Double Pause", "doc_type": "invoice", "frequency": "monthly", "start_date": "2026-02-01"},
    )).json()["id"]
    await client.post(f"/subscriptions/{entity_id}/pause", headers=_auth(token))
    r = await client.post(f"/subscriptions/{entity_id}/pause", headers=_auth(token))
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_generate_now_creates_document(client):
    token = await _register(client)
    entity_id = (await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={
            "name": "Auto Invoice",
            "contact_id": "contact:cust1",
            "doc_type": "invoice",
            "frequency": "monthly",
            "start_date": "2026-02-01",
            "line_items": [{"description": "Service Fee", "quantity": 1, "unit_price": 1000}],
        },
    )).json()["id"]

    r = await client.post(f"/subscriptions/{entity_id}/generate", headers=_auth(token))
    assert r.status_code == 200
    result = r.json()
    assert result["doc_id"].startswith("doc:")
    assert result["next_run"]

    # The subscription should now reflect last_run and last_generated_doc_id
    sub = (await client.get(f"/subscriptions/{entity_id}", headers=_auth(token))).json()
    assert sub["last_generated_doc_id"] == result["doc_id"]


@pytest.mark.asyncio
async def test_subscription_invalid_doc_type(client):
    token = await _register(client)
    r = await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={"name": "Bad", "doc_type": "memo", "frequency": "monthly", "start_date": "2026-02-01"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_subscription_custom_frequency_requires_interval(client):
    token = await _register(client)
    r = await client.post(
        "/subscriptions",
        headers=_auth(token),
        json={"name": "Custom", "doc_type": "invoice", "frequency": "custom", "start_date": "2026-02-01"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ar_aging_empty(client):
    token = await _register(client)
    r = await client.get("/reports/ar-aging", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert "lines" in data
    assert data["lines"] == []


@pytest.mark.asyncio
async def test_ap_aging_empty(client):
    token = await _register(client)
    r = await client.get("/reports/ap-aging", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["lines"] == []


@pytest.mark.asyncio
async def test_sales_report_group_by_options(client):
    token = await _register(client)
    for group_by in ("customer", "item", "period"):
        r = await client.get(f"/reports/sales?group_by={group_by}", headers=_auth(token))
        assert r.status_code == 200, f"Failed for group_by={group_by}"
        data = r.json()
        assert "lines" in data
        assert data["group_by"] == group_by


@pytest.mark.asyncio
async def test_purchases_report_group_by_options(client):
    token = await _register(client)
    for group_by in ("supplier", "item", "period"):
        r = await client.get(f"/reports/purchases?group_by={group_by}", headers=_auth(token))
        assert r.status_code == 200, f"Failed for group_by={group_by}"
        data = r.json()
        assert "lines" in data
        assert data["group_by"] == group_by


@pytest.mark.asyncio
async def test_expiring_report_empty(client):
    token = await _register(client)
    r = await client.get("/reports/expiring?days=30", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["days_threshold"] == 30


# ---------------------------------------------------------------------------
# Multi-tax schema
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tax_rate_has_new_fields(client):
    """TaxRate model now includes is_compound and default_order."""
    token = await _register(client, email="taxfields@test.com")
    new_taxes = [
        {
            "name": "WHT 3%",
            "rate": 3.0,
            "tax_type": "purchase",
            "is_default": False,
            "description": "Withholding tax",
            "is_compound": False,
            "default_order": 1,
        },
    ]
    r = await client.patch("/companies/me/taxes", headers=_auth(token), json={"taxes": new_taxes})
    assert r.status_code == 200

    r2 = await client.get("/companies/me/taxes", headers=_auth(token))
    taxes = r2.json()
    assert taxes[0]["is_compound"] is False
    assert taxes[0]["default_order"] == 1


@pytest.mark.asyncio
async def test_doc_with_line_taxes(client):
    """LineItem accepts taxes list; doc is created successfully."""
    token = await _register(client, email="linetax@test.com")
    line_taxes = [{"code": "VAT 7%", "rate": 7.0, "amount": 70.0, "order": 0, "is_compound": False}]
    payload = {
        "doc_type": "invoice",
        "line_items": [
            {
                "description": "Service",
                "quantity": 1,
                "unit_price": 1000.0,
                "taxes": line_taxes,
                "line_total": 1000.0,
            }
        ],
        "subtotal": 1000.0,
        "tax": 70.0,
        "total": 1070.0,
    }
    r = await client.post("/docs", headers=_auth(token), json=payload)
    assert r.status_code == 200
    doc_id = r.json()["id"]

    r2 = await client.get(f"/docs/{doc_id}", headers=_auth(token))
    doc = r2.json()
    li = doc["line_items"][0]
    assert li["taxes"][0]["code"] == "VAT 7%"
    assert li["taxes"][0]["amount"] == 70.0


@pytest.mark.asyncio
async def test_doc_with_doc_taxes(client):
    """DocCreatePayload accepts doc_taxes list; total auto-computed from doc_taxes."""
    token = await _register(client, email="doctax@test.com")
    doc_taxes = [{"code": "VAT 7%", "rate": 7.0, "amount": 70.0, "order": 0, "is_compound": False}]
    payload = {
        "doc_type": "invoice",
        "line_items": [
            {
                "description": "Widget",
                "quantity": 1,
                "unit_price": 1000.0,
                "line_total": 1000.0,
            }
        ],
        "doc_taxes": doc_taxes,
    }
    r = await client.post("/docs", headers=_auth(token), json=payload)
    assert r.status_code == 200
    doc_id = r.json()["id"]

    r2 = await client.get(f"/docs/{doc_id}", headers=_auth(token))
    doc = r2.json()
    assert doc["total"] == pytest.approx(1070.0, abs=0.01)
    assert doc["doc_taxes"][0]["code"] == "VAT 7%"


@pytest.mark.asyncio
async def test_item_create_with_tax_codes(client):
    """ItemCreate accepts tax_codes; stored on item state."""
    token = await _register(client, email="itemtax@test.com")
    payload = {
        "sku": "TX-001",
        "name": "Taxable Widget",
        "quantity": 10,
        "sell_by": "piece",
        "tax_codes": ["VAT 7%"],
    }
    r = await client.post("/items", headers=_auth(token), json=payload)
    assert r.status_code == 200
    item_id = r.json()["id"]

    r2 = await client.get(f"/items/{item_id}", headers=_auth(token))
    item = r2.json()
    assert "VAT 7%" in item.get("tax_codes", [])


@pytest.mark.asyncio
async def test_doc_backward_compat_tax_field(client):
    """Legacy tax field still works; no regressions."""
    token = await _register(client, email="bctax@test.com")
    payload = {
        "doc_type": "invoice",
        "line_items": [{"description": "Old-style", "quantity": 1, "unit_price": 500.0, "tax_rate": 7.0, "line_total": 500.0}],
        "subtotal": 500.0,
        "tax": 35.0,
        "total": 535.0,
    }
    r = await client.post("/docs", headers=_auth(token), json=payload)
    assert r.status_code == 200
    doc_id = r.json()["id"]

    r2 = await client.get(f"/docs/{doc_id}", headers=_auth(token))
    doc = r2.json()
    assert doc["total"] == pytest.approx(535.0, abs=0.01)
    assert doc["line_items"][0]["tax_rate"] == 7.0


@pytest.mark.asyncio
async def test_default_tax_rates_have_new_fields(client):
    """Default tax rates returned by GET /companies/me/taxes include new fields."""
    token = await _register(client, email="defaulttax2@test.com")
    r = await client.get("/companies/me/taxes", headers=_auth(token))
    assert r.status_code == 200
    taxes = r.json()
    for t in taxes:
        assert "is_compound" in t
        assert "default_order" in t


@pytest.mark.asyncio
async def test_compound_doc_tax_computed_server_side(client):
    """is_compound=True doc tax applies to subtotal + preceding tax amounts, computed server-side.

    Setup: subtotal = 1000
      Tax 1: GST 5%,  is_compound=False, order=0 → amount = 1000 * 5% = 50
      Tax 2: QST 9.975%, is_compound=True, order=1 → amount = (1000 + 50) * 9.975% = 104.7375 ≈ 104.74

    Amounts submitted as 0 so server must compute them.
    Total = 1000 + 50 + 104.74 = 1154.74
    """
    token = await _register(client, email="compound@test.com")
    payload = {
        "doc_type": "invoice",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 1000.0, "line_total": 1000.0}],
        "doc_taxes": [
            {"code": "GST", "rate": 5.0, "amount": 0.0, "order": 0, "is_compound": False},
            {"code": "QST", "rate": 9.975, "amount": 0.0, "order": 1, "is_compound": True},
        ],
    }
    r = await client.post("/docs", headers=_auth(token), json=payload)
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=_auth(token))).json()

    taxes = {t["code"]: t for t in doc["doc_taxes"]}
    assert taxes["GST"]["amount"] == pytest.approx(50.0, abs=0.01)
    assert taxes["QST"]["amount"] == pytest.approx(104.74, abs=0.01)
    assert doc["total"] == pytest.approx(1154.74, abs=0.01)


@pytest.mark.asyncio
async def test_non_compound_doc_taxes_both_apply_to_base(client):
    """Two non-compound doc taxes both apply to subtotal independently (not stacked).

    Setup: subtotal = 1000
      VAT 7%: is_compound=False → 70.0
      Excise 5%: is_compound=False → 50.0
    Total = 1000 + 70 + 50 = 1120
    """
    token = await _register(client, email="flatstack@test.com")
    payload = {
        "doc_type": "invoice",
        "line_items": [{"description": "Item", "quantity": 1, "unit_price": 1000.0, "line_total": 1000.0}],
        "doc_taxes": [
            {"code": "VAT 7%", "rate": 7.0, "amount": 0.0, "order": 0, "is_compound": False},
            {"code": "Excise 5%", "rate": 5.0, "amount": 0.0, "order": 1, "is_compound": False},
        ],
    }
    r = await client.post("/docs", headers=_auth(token), json=payload)
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=_auth(token))).json()

    taxes = {t["code"]: t for t in doc["doc_taxes"]}
    assert taxes["VAT 7%"]["amount"] == pytest.approx(70.0, abs=0.01)
    assert taxes["Excise 5%"]["amount"] == pytest.approx(50.0, abs=0.01)
    assert doc["total"] == pytest.approx(1120.0, abs=0.01)


@pytest.mark.asyncio
async def test_negative_wht_doc_tax(client):
    """Negative (offset) doc tax - withholding tax reduces net payable.

    Setup: subtotal = 1000
      VAT 7%: +70.0
      WHT -3%: is_compound=False, amount=0 → server computes -30.0
    Total = 1000 + 70 - 30 = 1040
    """
    token = await _register(client, email="wht@test.com")
    payload = {
        "doc_type": "invoice",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 1000.0, "line_total": 1000.0}],
        "doc_taxes": [
            {"code": "VAT 7%", "rate": 7.0, "amount": 0.0, "order": 0, "is_compound": False},
            {"code": "WHT", "rate": -3.0, "amount": 0.0, "order": 1, "is_compound": False},
        ],
    }
    r = await client.post("/docs", headers=_auth(token), json=payload)
    assert r.status_code == 200
    doc = (await client.get(f"/docs/{r.json()['id']}", headers=_auth(token))).json()

    taxes = {t["code"]: t for t in doc["doc_taxes"]}
    assert taxes["WHT"]["amount"] == pytest.approx(-30.0, abs=0.01)
    assert doc["total"] == pytest.approx(1040.0, abs=0.01)


@pytest.mark.asyncio
async def test_compute_tax_amounts_unit(client):
    """Unit test for compute_tax_amounts helper directly."""
    from celerp_docs.taxes import TaxApplication, compute_tax_amounts

    taxes = [
        TaxApplication(code="GST", rate=5.0, amount=0.0, order=0, is_compound=False),
        TaxApplication(code="QST", rate=9.975, amount=0.0, order=1, is_compound=True),
        TaxApplication(code="WHT", rate=-3.0, amount=0.0, order=2, is_compound=False),
    ]
    result = compute_tax_amounts(taxes, base=1000.0)

    assert result[0].amount == pytest.approx(50.0, abs=0.01)       # GST: 1000 * 5%
    assert result[1].amount == pytest.approx(104.74, abs=0.01)     # QST: 1050 * 9.975%
    assert result[2].amount == pytest.approx(-30.0, abs=0.01)      # WHT: 1000 * -3%


@pytest.mark.asyncio
async def test_caller_provided_amount_not_overridden(client):
    """Explicit non-zero amount from caller is kept as-is, not recomputed."""
    from celerp_docs.taxes import TaxApplication, compute_tax_amounts

    taxes = [
        TaxApplication(code="Special", rate=10.0, amount=999.0, order=0, is_compound=False),
    ]
    result = compute_tax_amounts(taxes, base=1000.0)
    # rate would give 100, but caller said 999 — trust the caller
    assert result[0].amount == 999.0


# ---------------------------------------------------------------------------
# Tax regime auto-seeding via default location
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_location_with_country_seeds_taxes(client):
    """Creating a default location with a known country seeds correct tax regime."""
    token = await _register(client, email="regime_sg@test.com")
    r = await client.post("/companies/me/locations", headers=_auth(token), json={
        "name": "HQ", "type": "warehouse", "is_default": True,
        "address": {"country": "SG", "city": "Singapore"},
    })
    assert r.status_code == 200

    taxes = (await client.get("/companies/me/taxes", headers=_auth(token))).json()
    names = {t["name"] for t in taxes}
    assert "GST 9%" in names

    me = (await client.get("/companies/me", headers=_auth(token))).json()
    assert me["settings"]["currency"] == "SGD"


@pytest.mark.asyncio
async def test_patch_location_country_seeds_taxes(client):
    """Patching an existing default location to add a country triggers re-seed."""
    token = await _register(client, email="regime_au@test.com")
    loc_id = (await client.post("/companies/me/locations", headers=_auth(token), json={
        "name": "Warehouse", "type": "warehouse", "is_default": True,
    })).json()["id"]

    await client.patch(f"/companies/me/locations/{loc_id}", headers=_auth(token), json={
        "address": {"country": "AU"},
    })

    taxes = (await client.get("/companies/me/taxes", headers=_auth(token))).json()
    assert any(t["name"] == "GST 10%" for t in taxes)
    me = (await client.get("/companies/me", headers=_auth(token))).json()
    assert me["settings"]["currency"] == "AUD"


@pytest.mark.asyncio
async def test_non_default_location_does_not_seed_taxes(client):
    """A non-default location with a country does NOT trigger re-seed."""
    token = await _register(client, email="regime_nontrigger@test.com")
    await client.post("/companies/me/locations", headers=_auth(token), json={
        "name": "Remote", "type": "warehouse", "is_default": False,
        "address": {"country": "GB"},
    })

    taxes = (await client.get("/companies/me/taxes", headers=_auth(token))).json()
    # Should still be generic default, not GB VAT
    assert not any(t["name"] == "VAT 20%" for t in taxes)


@pytest.mark.asyncio
async def test_customised_taxes_not_overwritten_by_location(client):
    """If user already customised taxes, location country change does NOT overwrite."""
    token = await _register(client, email="regime_custom@test.com")

    # Customise taxes first
    await client.patch("/companies/me/taxes", headers=_auth(token), json={
        "taxes": [{"name": "My Special Tax", "rate": 13.0, "tax_type": "both",
                   "is_default": True, "description": "", "is_compound": False, "default_order": 0}]
    })

    # Now create a default location with a country
    await client.post("/companies/me/locations", headers=_auth(token), json={
        "name": "HQ", "type": "warehouse", "is_default": True,
        "address": {"country": "TH"},
    })

    taxes = (await client.get("/companies/me/taxes", headers=_auth(token))).json()
    names = {t["name"] for t in taxes}
    # Custom tax preserved, TH regime NOT applied
    assert "My Special Tax" in names
    assert "VAT 7%" not in names


@pytest.mark.asyncio
async def test_unknown_country_falls_back_to_default(client):
    """Unknown country code falls back to _default regime (placeholder tax, USD)."""
    token = await _register(client, email="regime_unknown@test.com")
    await client.post("/companies/me/locations", headers=_auth(token), json={
        "name": "HQ", "type": "warehouse", "is_default": True,
        "address": {"country": "ZZ"},
    })

    me = (await client.get("/companies/me", headers=_auth(token))).json()
    assert me["settings"]["currency"] == "USD"
    taxes = (await client.get("/companies/me/taxes", headers=_auth(token))).json()
    assert any(t["name"] == "Standard Tax" for t in taxes)

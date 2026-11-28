# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Extended coverage for celerp/routers/companies.py.

Targets: locations PATCH, BOM CRUD, taxes batch import, payment terms batch import,
         error paths (invalid UUID, 404, 409, invalid rate/days, missing name).
"""
from __future__ import annotations

import os
import uuid as _uuid

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest

from test_helpers import refusal_keys, refused_records
from ui.i18n import t

from connector_test_support import connector_routes, connector_session, record_first_sync  # noqa: F401


# ── helper ────────────────────────────────────────────────────────────────────

async def _register(client, suffix: str) -> dict:
    r = await client.post(
        "/auth/register",
        json={
            "company_name": f"Co {suffix}",
            "email": f"admin_{suffix}@test.example",
            "name": "Admin",
            "password": "pw123val",
        },
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _tax_record(name: str, rate, tax_type: str = "both", idem: str | None = None) -> dict:
    return {
        "entity_id": "company",
        "event_type": "tax.import",
        "source": "import",
        "idempotency_key": idem or f"tax-{name.lower().replace(' ', '-')}",
        "data": {"name": name, "rate": rate, "tax_type": tax_type},
    }


def _pt_record(name: str, days, idem: str | None = None) -> dict:
    return {
        "entity_id": "company",
        "event_type": "pt.import",
        "source": "import",
        "idempotency_key": idem or f"pt-{name.lower().replace(' ', '-')}",
        "data": {"name": name, "days": days},
    }


# ── Locations ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_location_patch(client):
    headers = await _register(client, "loc_patch")

    # Create
    r = await client.post(
        "/companies/me/locations",
        json={"name": "Warehouse A", "type": "warehouse", "is_default": True},
        headers=headers,
    )
    assert r.status_code == 200
    loc_id = r.json()["id"]

    # Patch name
    r = await client.patch(
        f"/companies/me/locations/{loc_id}",
        json={"name": "Warehouse B"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Warehouse B"

    # Verify updated
    r = await client.get("/companies/me/locations", headers=headers)
    names = [loc["name"] for loc in r.json()["items"]]
    assert "Warehouse B" in names


@pytest.mark.asyncio
async def test_location_patch_invalid_uuid(client):
    headers = await _register(client, "loc_bad_uuid")
    r = await client.patch(
        "/companies/me/locations/not-a-uuid",
        json={"name": "X"},
        headers=headers,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_location_patch_not_found(client):
    headers = await _register(client, "loc_404")
    r = await client.patch(
        f"/companies/me/locations/{_uuid.uuid4()}",
        json={"name": "X"},
        headers=headers,
    )
    assert r.status_code == 404


# The /companies/me/boms CRUD API was retired (recipes live on the inventory item); its removal
# is covered by test_bom_removed.py.


# ── Taxes batch import ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_taxes_batch_import_creates(client):
    headers = await _register(client, "tax_import")

    r = await client.post(
        "/companies/me/taxes/import/batch",
        json={"records": [
            _tax_record("Custom GST 10%", 10.0),       # not in defaults
            _tax_record("WHT 3%", 3.0, "sales"),
        ]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 2
    assert body["skipped"] == 0
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_taxes_batch_import_skips_duplicate(client):
    headers = await _register(client, "tax_dup")

    payload = {"records": [_tax_record("VAT 7%", 7.0, idem="vat7-unique")]}
    await client.post("/companies/me/taxes/import/batch", json=payload, headers=headers)
    # Second import with same name (different idem key - duplicate is name-based)
    payload2 = {"records": [_tax_record("VAT 7%", 7.0, idem="vat7-unique-2")]}
    r = await client.post("/companies/me/taxes/import/batch", json=payload2, headers=headers)

    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == 1
    assert body["created"] == 0


@pytest.mark.asyncio
async def test_taxes_batch_import_invalid_rate(client):
    headers = await _register(client, "tax_bad_rate")

    r = await client.post(
        "/companies/me/taxes/import/batch",
        json={"records": [_tax_record("Bad Tax", "notanumber")]},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 0
    assert refusal_keys(body["errors"]) == ["settings.rate_must_be_a_number"], body


@pytest.mark.asyncio
async def test_taxes_batch_import_missing_name(client):
    headers = await _register(client, "tax_no_name")

    r = await client.post(
        "/companies/me/taxes/import/batch",
        json={"records": [{
            "entity_id": "company",
            "event_type": "tax.import",
            "source": "import",
            "idempotency_key": "tax-noname-1",
            "data": {"rate": 5.0, "tax_type": "sales"},
        }]},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert refusal_keys(body["errors"]) == ["settings.name_required"], body


@pytest.mark.asyncio
async def test_taxes_batch_import_invalid_tax_type(client):
    headers = await _register(client, "tax_bad_type")

    r = await client.post(
        "/companies/me/taxes/import/batch",
        json={"records": [_tax_record("Weird Tax", 5.0, "weird")]},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert refusal_keys(body["errors"]) == ["import.record.invalid_tax_type"], body


# ── Payment terms batch import ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_payment_terms_batch_import_creates(client):
    headers = await _register(client, "pt_import")

    r = await client.post(
        "/companies/me/payment-terms/import/batch",
        json={"records": [
            _pt_record("Net 120", 120),   # not in defaults
            _pt_record("Net 180", 180),
        ]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 2
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_payment_terms_batch_import_skips_duplicate(client):
    headers = await _register(client, "pt_dup")

    payload = {"records": [_pt_record("Net 45", 45, idem="pt45-a")]}
    await client.post("/companies/me/payment-terms/import/batch", json=payload, headers=headers)
    payload2 = {"records": [_pt_record("Net 45", 45, idem="pt45-b")]}
    r = await client.post("/companies/me/payment-terms/import/batch", json=payload2, headers=headers)

    assert r.status_code == 200
    assert r.json()["skipped"] == 1


@pytest.mark.asyncio
async def test_payment_terms_batch_import_invalid_days(client):
    headers = await _register(client, "pt_bad_days")

    r = await client.post(
        "/companies/me/payment-terms/import/batch",
        json={"records": [_pt_record("Bad Terms", "not-a-number")]},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert refusal_keys(body["errors"]) == ["error.days_must_be_number"], body


@pytest.mark.asyncio
async def test_payment_terms_batch_import_missing_name(client):
    headers = await _register(client, "pt_no_name")

    r = await client.post(
        "/companies/me/payment-terms/import/batch",
        json={"records": [{
            "entity_id": "company",
            "event_type": "pt.import",
            "source": "import",
            "idempotency_key": "pt-noname-1",
            "data": {"days": 15},
        }]},
        headers=headers,
    )
    assert r.status_code == 200
    assert refusal_keys(r.json()["errors"]) == ["settings.name_required"], r.json()


@pytest.mark.asyncio
async def test_purchasing_payment_terms_batch_import_creates_and_skips(client):
    headers = await _register(client, "ppt_import")
    payload = {"records": [_pt_record("Net 75", 75), _pt_record("Net 75", 75, idem="pt75-b"),
                           _pt_record("Soon", "x")]}
    r = await client.post("/companies/me/purchasing-payment-terms/import/batch", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["skipped"]) == (1, 1), r.json()
    assert refused_records(r.json()["errors"]) == {"Soon": t("error.days_must_be_number")}, r.json()
    terms = (await client.get("/companies/me/purchasing-payment-terms", headers=headers)).json()
    assert {"name": "Net 75", "days": 75, "description": ""} in terms


@pytest.mark.asyncio
async def test_locations_batch_import_creates_and_skips(client):
    headers = await _register(client, "loc_import")
    payload = {"records": [{"name": "Back Room", "type": "store"}, {"name": "Back Room"}, {"name": " "}]}
    r = await client.post("/companies/me/locations/import/batch", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["skipped"], body["failed"]) == (1, 1, 1), body
    assert refused_records(body["errors"]) == {"Row 3": t("settings.name_required")}, body
    names = [loc["name"] for loc in (await client.get("/companies/me/locations", headers=headers)).json()["items"]]
    assert names.count("Back Room") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "body", "read"), [
    ("/companies/me/column-prefs", {"prefs": {"inventory": {"vis": {"sku": True, "name": False}, "order": ["name", "sku"]}}},
     {"inventory": {"vis": {"sku": True, "name": False}, "order": ["name", "sku"]}}),
    ("/companies/me/contact-tags", {"tags": [{"name": "VIP", "color": "gold"}]}, [{"name": "VIP", "color": "gold"}]),
    ("/companies/me/default-price-list", {"name": "Wholesale"}, "Wholesale"),
])
async def test_company_setting_patch_is_read_back(client, path, body, read):
    headers = await _register(client, f"set_{path.rsplit('/', 1)[-1]}")
    r = await client.patch(path, json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert (await client.get(path, headers=headers)).json() == read


@pytest.mark.asyncio
async def test_each_user_keeps_their_own_column_setup(client):
    """Column setup belongs to the signed-in user: a colleague's change never moves theirs, a
    later change to one part keeps the others, and null forgets the table."""
    owner = await _register(client, "colprefs")
    r = await client.post("/companies/me/users", headers=owner, json={
        "name": "Staff", "email": "staff_colprefs@test.example", "password": "pw123val", "role": "operator"})
    assert r.status_code == 200, r.text
    r = await client.post("/auth/login", json={"email": "staff_colprefs@test.example", "password": "pw123val"})
    assert r.status_code == 200, r.text
    staff = {"Authorization": f"Bearer {r.json()['access_token']}"}

    for h, vis in ((owner, {"sku": False}), (staff, {"sku": True})):
        assert (await client.patch("/companies/me/column-prefs", headers=h,
                                   json={"prefs": {"customers": {"vis": vis}}})).status_code == 200
    await client.patch("/companies/me/column-prefs", headers=owner,
                       json={"prefs": {"customers": {"widths": {"sku": "120px"}}}})
    assert (await client.get("/companies/me/column-prefs", headers=owner)).json() == {
        "customers": {"vis": {"sku": False}, "widths": {"sku": "120px"}}}
    assert (await client.get("/companies/me/column-prefs", headers=staff)).json() == {"customers": {"vis": {"sku": True}}}

    await client.patch("/companies/me/column-prefs", headers=owner, json={"prefs": {"customers": None}})
    assert (await client.get("/companies/me/column-prefs", headers=owner)).json() == {}
    assert (await client.patch("/companies/me/column-prefs", headers=owner,
                               json={"prefs": {"customers": {"colour": "red"}}})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("width", ["NaN", "Infinity", "-Infinity", "1e400"])
async def test_a_column_width_that_is_not_a_number_is_refused_unsaved(client, width):
    headers = await _register(client, f"colwidth_{width.strip('-').lower()}{len(width)}")
    r = await client.patch("/companies/me/column-prefs", headers={**headers, "Content-Type": "application/json"},
                           content='{"prefs": {"inventory": {"widths": {"name": %s}}}}' % width)
    assert r.status_code == 422, r.text
    assert (await client.get("/companies/me/column-prefs", headers=headers)).json() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["inventory", "customers", "vendors", "doc_lines_invoice", "doc_lines_list_quotation"])
async def test_every_table_with_a_column_setup_is_saved(client, table):
    headers = await _register(client, f"coltable_{table}")
    r = await client.patch("/companies/me/column-prefs", headers=headers, json={"prefs": {table: {"widths": {"name": 12.5}}}})
    assert r.status_code == 200, r.text
    assert (await client.get("/companies/me/column-prefs", headers=headers)).json() == {table: {"widths": {"name": 12.5}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["items", "contacts", "Inventory", "doc_lines_", "doc_lines_9", "inventory ", "t0_1"])
async def test_a_table_with_no_column_setup_is_refused_by_name(client, table):
    headers = await _register(client, f"colunknown_{abs(hash(table)) % 10**8}")
    r = await client.patch("/companies/me/column-prefs", headers=headers,
                           json={"prefs": {"inventory": {"vis": {"sku": False}}, table: {"vis": {"sku": True}}}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message"] == t("settings.err_column_table_unknown", table=table)
    assert (await client.get("/companies/me/column-prefs", headers=headers)).json() == {}


@pytest.mark.asyncio
async def test_one_user_stores_a_bounded_number_of_tables(client):
    """Each save stays under the per-call limit, yet the tables one user keeps never pass the
    bound: the save that would pass it is refused whole, and forgetting a table makes room."""
    from celerp.routers.companies import MAX_COLUMN_TABLES

    headers = await _register(client, "colbound")
    names = [f"doc_lines_kind_{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(MAX_COLUMN_TABLES + 1)]
    r = await client.patch("/companies/me/column-prefs", headers=headers,
                           json={"prefs": {n: {"order": ["sku"]} for n in names[:MAX_COLUMN_TABLES]}})
    assert r.status_code == 200, r.text
    r = await client.patch("/companies/me/column-prefs", headers=headers, json={"prefs": {names[-1]: {"order": ["sku"]}}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message"] == t("settings.err_column_tables_full", limit=MAX_COLUMN_TABLES)
    assert len((await client.get("/companies/me/column-prefs", headers=headers)).json()) == MAX_COLUMN_TABLES
    # a table already kept still saves, and forgetting one makes room for the next
    assert (await client.patch("/companies/me/column-prefs", headers=headers,
                               json={"prefs": {names[0]: {"order": ["name"]}}})).status_code == 200
    assert (await client.patch("/companies/me/column-prefs", headers=headers,
                               json={"prefs": {names[1]: None, names[-1]: {"order": ["sku"]}}})).status_code == 200


# ── /health routes ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_ready(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"


@pytest.mark.asyncio
async def test_health_system(client):
    # /health/system reports host RAM/CPU/disk and is authenticated.
    r_anon = await client.get("/health/system")
    assert r_anon.status_code == 401
    reg = await client.post(
        "/auth/register",
        json={"company_name": "SysCo", "email": "sys@example.com", "name": "Admin", "password": "pwvalid1"},
    )
    r = await client.get(
        "/health/system", headers={"Authorization": f"Bearer {reg.json()['access_token']}"}
    )
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# ── /connectors sync success path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_sync_success(client, session, connector_routes):
    """POST /connectors/shopify/sync with mocked connector returns 200 SyncResponse."""
    from unittest.mock import AsyncMock, patch
    from celerp.connectors.base import SyncResult, SyncEntity

    base_headers = await _register(client, "conn_sync")
    headers = {**base_headers, "X-Session-Token": connector_routes}
    company_id = (await client.get("/companies/me", headers=base_headers)).json()["id"]
    from celerp.models.connector_config import ConnectorConfig
    config = ConnectorConfig(company_id=str(company_id), connector="shopify")
    session.add(config)
    await session.flush()
    await record_first_sync(session, config, "completed")
    await session.commit()

    mock_result = SyncResult(
        entity=SyncEntity.PRODUCTS,
        created=3,
        updated=0,
        skipped=1,
        errors=[],
    )

    with patch("celerp.connectors.shopify.ShopifyConnector.sync_products", new=AsyncMock(return_value=mock_result)):
        r = await client.post(
            "/connectors/shopify/sync",
            headers=headers,
            json={"entity": "products"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["created"] == 3
    assert body["skipped"] == 1

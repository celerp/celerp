# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Each company setting changes through exactly one owner.

PATCH /companies/me accepts only the general settings. A setting with its own page is
refused there with a keyed message naming the route that changes it: the lock date
(the period lock), the posting accounts, the books (currency, fiscal year start, import
VAT, deposit accounts), role permissions, modules, the business type and every other
key in the owner table. An unknown key is refused too. The books route needs Manage
accounting, records who changed what and when, and refuses a currency change once the
company has posted entries of its own.
"""
from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from celerp.models.company import Company

pytestmark = pytest.mark.asyncio


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _owner(client) -> tuple[dict, uuid.UUID]:
    r = await client.post("/auth/register", json={
        "company_name": "Owners Co", "email": f"own-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = _h(r.json()["access_token"])
    me = (await client.get("/companies/me", headers=h)).json()
    return h, uuid.UUID(me["id"])


async def _settings(session, cid) -> dict:
    session.expire_all()
    return dict((await session.get(Company, cid)).settings or {})


def _keyed(r, status: int, key: str) -> dict:
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail.get("message_key") == key, detail
    return detail


async def _user_with_role(client, session, admin_h: dict, role: str) -> dict:
    addr = f"{role}-{os.urandom(4).hex()}@example.com"
    r = await client.post("/companies/me/users", headers=admin_h, json={
        "name": role.title(), "email": addr, "password": "testpass123", "role": role})
    assert r.status_code == 200, r.text
    from celerp.services.session_tracker import clear
    await clear(session)
    r = await client.post("/auth/login", json={"email": addr, "password": "testpass123"})
    assert r.status_code == 200, r.text
    return _h(r.json()["access_token"])


async def _post_own_entry(client, h) -> None:
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100, "line_total": 100}],
        "subtotal": 100, "tax": 0, "total": 100})
    assert r.status_code == 200, r.text
    r = await client.post(f"/docs/{r.json()['id']}/finalize", headers=h)
    assert r.status_code == 200, r.text


async def test_company_settings_patch_refuses_the_lock_date(client, session):
    """Red statement: patch_me merged any key, so an admin moved the lock date with no
    Manage accounting check and no record of who did it."""
    h, cid = await _owner(client)
    r = await client.patch("/companies/me", headers=h, json={"settings": {"lock_date": "2026-01-31"}})
    detail = _keyed(r, 422, "company.setting_has_own_route")
    assert "/accounting/period-lock" in detail["message"]
    assert "lock_date" not in await _settings(session, cid)


async def test_company_settings_patch_refuses_posting_accounts(client, session):
    h, cid = await _owner(client)
    before = (await _settings(session, cid)).get("posting_roles")
    r = await client.patch("/companies/me", headers=h, json={"settings": {"posting_roles": {"ar": "9999"}}})
    detail = _keyed(r, 422, "company.setting_has_own_route")
    assert "/accounting/posting-accounts" in detail["message"]
    assert (await _settings(session, cid)).get("posting_roles") == before


@pytest.mark.parametrize("key,value", [
    ("currency", "EUR"), ("fiscal_year_start", "04-01"), ("import_vat_recoverable_default", True),
    ("stripe_deposit_account", "1010"), ("woocommerce_deposit_account", "1010"),
    ("opening_balance_date", "2026-03-31"),
])
async def test_company_settings_patch_refuses_the_books(client, session, key, value):
    h, cid = await _owner(client)
    before = (await _settings(session, cid)).get(key)
    r = await client.patch("/companies/me", headers=h, json={"settings": {key: value}})
    detail = _keyed(r, 422, "company.setting_has_own_route")
    assert "/companies/me/books" in detail["message"]
    assert (await _settings(session, cid)).get(key) == before


async def test_every_owned_setting_is_refused_with_its_route(client, session):
    """The whole class: every key in the owner table other than the general ones."""
    from celerp.services.company_settings import GENERAL, OWNERS, SYSTEM

    h, cid = await _owner(client)
    before = await _settings(session, cid)
    owned = [k for k in OWNERS if k not in GENERAL]
    assert len(owned) > 30
    for key in owned:
        r = await client.patch("/companies/me", headers=h, json={"settings": {key: "x"}})
        if OWNERS[key] == SYSTEM:
            _keyed(r, 422, "company.setting_system_owned")
        else:
            detail = _keyed(r, 422, "company.setting_has_own_route")
            assert OWNERS[key] in detail["message"], (key, detail)
    assert await _settings(session, cid) == before


async def test_company_settings_patch_refuses_an_unknown_key(client, session):
    h, cid = await _owner(client)
    r = await client.patch("/companies/me", headers=h, json={"settings": {"made_up_key": 1}})
    detail = _keyed(r, 422, "company.setting_unknown")
    assert "made_up_key" in detail["message"]
    assert "made_up_key" not in await _settings(session, cid)


async def test_a_refused_key_saves_nothing_from_the_same_request(client, session):
    h, cid = await _owner(client)
    r = await client.patch("/companies/me", headers=h, json={
        "settings": {"timezone": "Asia/Bangkok", "lock_date": "2026-01-31"}})
    _keyed(r, 422, "company.setting_has_own_route")
    assert (await _settings(session, cid)).get("timezone") != "Asia/Bangkok"


async def test_general_settings_still_save_through_company_settings(client, session):
    """Neighbour: the general settings keep their door."""
    h, cid = await _owner(client)
    r = await client.patch("/companies/me", headers=h, json={"settings": {
        "timezone": "Asia/Bangkok", "line_item_identifier": "sku", "getting_started_dismissed": True,
        "manufacturing": {"auto_create_work_orders": True}}})
    assert r.status_code == 200, r.text
    s = await _settings(session, cid)
    assert s["timezone"] == "Asia/Bangkok" and s["line_item_identifier"] == "sku"
    assert s["manufacturing"] == {"auto_create_work_orders": True}


async def test_books_route_saves_and_records_who_changed_it(client, session):
    h, cid = await _owner(client)
    r = await client.patch("/companies/me/books", headers=h, json={
        "currency": "EUR", "fiscal_year_start": "04-01", "import_vat_recoverable_default": True})
    assert r.status_code == 200, r.text
    s = await _settings(session, cid)
    assert (s["currency"], s["fiscal_year_start"], s["import_vat_recoverable_default"]) == ("EUR", "04-01", True)
    for key in ("currency", "fiscal_year_start", "import_vat_recoverable_default"):
        assert s[f"{key}_set_by"] and s[f"{key}_set_at"], key


async def test_books_route_saves_the_opening_balance_date_and_records_who_set_it(client, session):
    """Red statement: the books route had no opening balance date, so the date the opening
    balances are stated at could not be recorded and a request carrying it saved nothing."""
    h, cid = await _owner(client)
    r = await client.patch("/companies/me/books", headers=h, json={"opening_balance_date": "2026-03-31"})
    assert r.status_code == 200, r.text
    assert r.json()["opening_balance_date"] == "2026-03-31"
    s = await _settings(session, cid)
    assert s["opening_balance_date"] == "2026-03-31"
    assert s["opening_balance_date_set_by"] and s["opening_balance_date_set_at"]
    # Clearing it is a change too, and is recorded.
    r = await client.patch("/companies/me/books", headers=h, json={"opening_balance_date": ""})
    assert r.status_code == 200, r.text
    s2 = await _settings(session, cid)
    assert s2["opening_balance_date"] is None
    assert s2["opening_balance_date_set_at"] >= s["opening_balance_date_set_at"]


async def test_books_route_needs_manage_accounting(client, session):
    h, cid = await _owner(client)
    operator = await _user_with_role(client, session, h, "operator")
    before = (await _settings(session, cid)).get("currency")
    r = await client.patch("/companies/me/books", headers=operator, json={"currency": "EUR"})
    assert r.status_code == 403, r.text
    assert (await _settings(session, cid)).get("currency") == before


async def test_books_currency_change_is_refused_once_entries_are_posted(client, session):
    h, cid = await _owner(client)
    r = await client.patch("/companies/me/books", headers=h, json={"currency": "USD"})
    assert r.status_code == 200, r.text
    await _post_own_entry(client, h)
    r = await client.patch("/companies/me/books", headers=h, json={"currency": "EUR"})
    detail = _keyed(r, 409, "company.currency_has_postings")
    assert "USD" in detail["message"]
    assert (await _settings(session, cid))["currency"] == "USD"
    # Saving the same currency again is not a change and is accepted.
    r = await client.patch("/companies/me/books", headers=h, json={"currency": "USD"})
    assert r.status_code == 200, r.text


async def test_books_currency_change_is_allowed_with_only_sample_stock(client, session):
    """Neighbour: the sample stock booked by the business type is not the company's own."""
    h, cid = await _owner(client)
    r = await client.post("/companies/me/business-type", json={"vertical": "agricultural"}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.patch("/companies/me/books", headers=h, json={"currency": "EUR"})
    assert r.status_code == 200, r.text
    assert (await _settings(session, cid))["currency"] == "EUR"


@pytest.mark.parametrize("payload,key", [
    ({"currency": "XXQ"}, "company.currency_unknown"),
    ({"fiscal_year_start": "13-01"}, "company.fiscal_year_start_invalid"),
    ({"fiscal_year_start": "04-15"}, "company.fiscal_year_start_invalid"),
    ({"import_vat_recoverable_default": "yes"}, "company.import_vat_default_invalid"),
    ({"opening_balance_date": "2026-02-30"}, "company.opening_balance_date_invalid"),
    ({"opening_balance_date": "31/03/2026"}, "company.opening_balance_date_invalid"),
    ({"opening_balance_date": 20260331}, "company.opening_balance_date_invalid"),
])
async def test_books_route_refuses_invalid_values(client, session, payload, key):
    h, cid = await _owner(client)
    before = await _settings(session, cid)
    r = await client.patch("/companies/me/books", headers=h, json=payload)
    _keyed(r, 422, key)
    after = await _settings(session, cid)
    assert {k: after.get(k) for k in payload} == {k: before.get(k) for k in payload}


async def test_posting_account_change_records_who_changed_it(client, session):
    h, cid = await _owner(client)
    accounts = (await client.get("/accounting/posting-accounts", headers=h)).json()
    role, code = _a_reassignable_role(accounts)
    r = await client.put(f"/accounting/posting-accounts/{role}", headers=h, json={"code": code})
    assert r.status_code == 200, r.text
    s = await _settings(session, cid)
    assert s["posting_roles_set_by"] and s["posting_roles_set_at"]


def _a_reassignable_role(accounts: dict) -> tuple[str, str]:
    """Some role and the account it already points at: re-pointing it is always valid."""
    for row in accounts.get("roles") or []:
        if row.get("account") or row.get("code"):
            return row["role"], row.get("account") or row.get("code")
    raise AssertionError(accounts)


async def test_settings_import_is_not_a_settings_door(client, session):
    """The legacy settings import writes only the company record event; settings stay."""
    h, cid = await _owner(client)
    before = await _settings(session, cid)
    r = await client.post("/companies/import/batch", headers=h, json={"records": [{
        "entity_id": str(cid), "event_type": "sys.company.created", "source": "csv",
        "idempotency_key": f"s01-{uuid.uuid4().hex[:6]}",
        "data": {"name": "Owners Co", "slug": "owners-co", "settings": {"lock_date": "2026-01-31"},
                 "lock_date": "2026-01-31"}}]})
    assert r.status_code == 200, r.text
    after = await _settings(session, cid)
    assert after.get("lock_date") == before.get("lock_date") is None

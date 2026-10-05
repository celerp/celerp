# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Factory reset on real Postgres. It resets the signed-in company and nothing else: the
owner types the company's exact name, every record of that company goes in one
transaction, and a user goes with it only when no other company still has them."""

from __future__ import annotations

import pytest

from migration_support import OWNER_EMAIL, OWNER_PASSWORD, auth, count, real_client, real_engine  # noqa: F401 - fixtures
from test_company_backup_ui import _page, ui  # noqa: F401 - fixture
from test_helpers import in_language

pytestmark = pytest.mark.asyncio


async def _register(client, company: str) -> str:
    r = await client.post("/auth/register", json={
        "company_name": company, "email": OWNER_EMAIL, "name": "Owner", "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _reset(client, token: str, name: str | None):
    return await client.post("/system/factory-reset", headers=auth(token),
                             json=None if name is None else {"confirm_name": name})


async def _id(client, token: str) -> str:
    return (await client.get("/companies/me", headers=auth(token))).json()["id"]


async def _held(engine, company_id: str) -> dict:
    return {table: await count(engine, table, "company_id = :i", i=company_id)
            for table in ("ledger", "projections", "user_companies", "locations")}


async def _two_companies(client) -> tuple[str, str]:
    """Alpha with a contact and a clerk only Alpha has; Beta, owned by the same owner,
    with a contact of its own."""
    ta = await _register(client, "Alpha Co")
    r = await client.post("/crm/contacts", json={"name": "Alpha Buyer"}, headers=auth(ta))
    assert r.status_code in (200, 201), r.text
    r = await client.post("/companies/me/users", headers=auth(ta), json={
        "email": "clerk@example.com", "name": "Clerk", "role": "operator", "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    r = await client.post("/companies", json={"name": "Beta Co"}, headers=auth(ta))
    assert r.status_code == 200, r.text
    tb = r.json()["access_token"]
    r = await client.post("/crm/contacts", json={"name": "Beta Buyer"}, headers=auth(tb))
    assert r.status_code in (200, 201), r.text
    return ta, tb


async def test_factory_reset_wipes_the_company_on_a_real_session(real_client, real_engine):  # noqa: F811
    token = await _register(real_client, "Reset Co")
    cid = await _id(real_client, token)

    r = await _reset(real_client, token, "Reset Co")

    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert await count(real_engine, "companies") == 0
    assert set((await _held(real_engine, cid)).values()) == {0}
    assert await count(real_engine, "users") == 0


async def test_resetting_one_company_leaves_the_other_and_its_people(real_client, real_engine):  # noqa: F811
    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    beta_before = await _held(real_engine, beta)
    assert beta_before["ledger"] > 0
    beta_contacts = (await real_client.get("/crm/contacts", headers=auth(tb))).json()["items"]
    assert "Beta Buyer" in [c["name"] for c in beta_contacts]

    r = await _reset(real_client, ta, "Alpha Co")

    assert r.status_code == 200, r.text
    assert await count(real_engine, "companies", "id = :i", i=alpha) == 0
    assert set((await _held(real_engine, alpha)).values()) == {0}
    assert await count(real_engine, "companies", "id = :i", i=beta) == 1
    assert await _held(real_engine, beta) == beta_before
    # The owner still has Beta; the clerk had only Alpha.
    assert await count(real_engine, "users", "email = :e", e=OWNER_EMAIL) == 1
    assert await count(real_engine, "users", "email = :e", e="clerk@example.com") == 0
    r = await real_client.get("/crm/contacts", headers=auth(tb))
    assert r.json()["items"] == beta_contacts


async def test_a_failure_part_way_leaves_both_companies_as_they_were(real_client, real_engine, monkeypatch):  # noqa: F811
    import celerp.routers.system as system

    ta, tb = await _two_companies(real_client)
    alpha, beta = await _id(real_client, ta), await _id(real_client, tb)
    before = {alpha: await _held(real_engine, alpha), beta: await _held(real_engine, beta)}
    users = await count(real_engine, "users")
    wipe = system._company_tables

    def broken():
        from sqlalchemy import Column, MetaData, Table, Uuid

        return [*wipe(), Table("no_such_table", MetaData(), Column("company_id", Uuid))]

    monkeypatch.setattr(system, "_company_tables", broken)
    with pytest.raises(Exception):  # the in-process transport re-raises the server error
        await _reset(real_client, ta, "Alpha Co")

    assert await count(real_engine, "companies") == 2
    assert {alpha: await _held(real_engine, alpha), beta: await _held(real_engine, beta)} == before
    assert await count(real_engine, "users") == users


@pytest.mark.parametrize("typed", [None, "", "RESET", "alpha co", "Alpha Co "])
async def test_reset_needs_the_exact_company_name(real_client, real_engine, typed):  # noqa: F811
    token = await _register(real_client, "Alpha Co")
    cid = await _id(real_client, token)
    before = await _held(real_engine, cid)

    r = await _reset(real_client, token, typed)

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "system.factory_reset.name_mismatch", detail
    assert detail["message"] == "Type the company name exactly as shown to reset this company."
    assert in_language("de", detail) != detail["message"]
    assert await count(real_engine, "companies", "id = :i", i=cid) == 1
    assert await _held(real_engine, cid) == before


# --- The settings page: the modal names the company and the typed name is what is sent ---


@pytest.mark.parametrize("lang", ["en", "de"])
async def test_the_reset_modal_names_the_company_to_type(ui, real_client, lang):  # noqa: F811
    token = await _register(real_client, "Alpha & Sons")
    ui.cookies.set("celerp_token", token)
    ui.cookies.set("celerp_lang", lang)

    r = await ui.get("/settings/general?tab=company")
    page = _page(r)

    assert "<strong>Alpha & Sons</strong>" in page
    assert 'name="confirm_name" data-expected="Alpha &amp; Sons"' in r.text
    assert 'hx-include="#factory-reset-confirm-input"' in page
    assert "RESET" not in page


@pytest.mark.parametrize("lang", ["en", "de"])
async def test_a_wrong_name_typed_in_the_modal_resets_nothing(ui, real_client, real_engine, lang):  # noqa: F811
    token = await _register(real_client, "Alpha Co")
    cid = await _id(real_client, token)
    before = await _held(real_engine, cid)
    ui.cookies.set("celerp_token", token)
    ui.cookies.set("celerp_lang", lang)

    r = await ui.post("/settings/factory-reset", data={"confirm_name": "RESET"}, headers={"HX-Request": "true"})

    refusal = {"message": "Type the company name exactly as shown to reset this company.",
               "message_key": "system.factory_reset.name_mismatch", "params": {}}
    assert in_language(lang, refusal) in _page(r)
    assert "HX-Redirect" not in r.headers
    assert await _held(real_engine, cid) == before


async def test_the_name_typed_in_the_modal_resets_the_company(ui, real_client, real_engine):  # noqa: F811
    token = await _register(real_client, "Alpha Co")
    ui.cookies.set("celerp_token", token)

    r = await ui.post("/settings/factory-reset", data={"confirm_name": "Alpha Co"}, headers={"HX-Request": "true"})

    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/setup", r.text
    assert await count(real_engine, "companies") == 0

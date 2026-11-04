# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tax and payment-term defaults are neutral, reads never write, and choosing a
country only replaces settings nobody has configured on a company with no books yet."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from celerp.tax_regimes import TAX_REGIMES

_GENERIC = TAX_REGIMES["_default"]["taxes"]
_CUSTOM_STANDARD = [{**_GENERIC[0], "rate": 12.0}, _GENERIC[1]]


async def _register(client, email: str) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Tax Co", "email": email, "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _settings(client, h) -> dict:
    r = await client.get("/companies/me", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["settings"]



async def _set_country(client, h, country: str) -> None:
    r = await client.post("/companies/me/locations", headers=h, json={
        "name": f"HQ {uuid.uuid4().hex[:6]}", "type": "warehouse", "is_default": True,
        "address": {"country": country}})
    assert r.status_code == 200, r.text


async def _post_manual_entry(client, h) -> None:
    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-01-15", "memo": "Sale", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 100}, {"account": "4100", "credit": 100}]})
    assert r.status_code == 200, r.text


# --- neutral defaults ------------------------------------------------------

async def test_new_company_reads_the_neutral_generic_taxes(client):
    h = await _register(client, "neutral@test.example")
    assert (await client.get("/companies/me/taxes", headers=h)).json() == _GENERIC
    assert (await client.get("/companies/me/purchasing-taxes", headers=h)).json() == _GENERIC


async def test_explicit_empty_sales_taxes_stay_empty(client):
    h = await _register(client, "empty-sales@test.example")
    r = await client.patch("/companies/me/taxes", headers=h, json={"taxes": []})
    assert r.status_code == 200, r.text
    assert (await client.get("/companies/me/taxes", headers=h)).json() == []
    assert (await client.get("/companies/me/purchasing-taxes", headers=h)).json() == []


async def test_explicit_empty_payment_terms_inherited_by_purchasing(client):
    h = await _register(client, "empty-terms@test.example")
    r = await client.patch("/companies/me/payment-terms", headers=h, json={"terms": []})
    assert r.status_code == 200, r.text
    assert (await client.get("/companies/me/purchasing-payment-terms", headers=h)).json() == []


async def test_purchasing_reads_make_no_writes(client):
    h = await _register(client, "no-write@test.example")
    before = await _settings(client, h)
    for _ in range(3):
        assert (await client.get("/companies/me/purchasing-taxes", headers=h)).status_code == 200
        assert (await client.get("/companies/me/purchasing-payment-terms", headers=h)).status_code == 200
    after = await _settings(client, h)
    assert "purchasing_taxes" not in after and "purchasing_payment_terms" not in after
    assert after == before


async def test_saved_purchasing_override_is_returned_unchanged(client):
    h = await _register(client, "override@test.example")
    own = [{**_GENERIC[0], "name": "Input VAT", "rate": 5.0}]
    r = await client.patch("/companies/me/purchasing-taxes", headers=h, json={"taxes": own})
    assert r.status_code == 200, r.text
    await client.patch("/companies/me/taxes", headers=h, json={"taxes": _CUSTOM_STANDARD})
    assert (await client.get("/companies/me/purchasing-taxes", headers=h)).json() == own


# --- country seeding -------------------------------------------------------

async def test_country_before_any_activity_seeds_the_regime(client):
    h = await _register(client, "fresh-th@test.example")
    await _set_country(client, h, "TH")
    s = await _settings(client, h)
    assert s["currency"] == TAX_REGIMES["TH"]["currency"]
    assert s["taxes"] == TAX_REGIMES["TH"]["taxes"]


async def test_customized_standard_tax_with_the_same_name_is_not_overwritten(client):
    h = await _register(client, "custom-std@test.example")
    r = await client.patch("/companies/me/taxes", headers=h, json={"taxes": _CUSTOM_STANDARD})
    assert r.status_code == 200, r.text
    await _set_country(client, h, "AU")
    assert (await client.get("/companies/me/taxes", headers=h)).json() == _CUSTOM_STANDARD


async def test_explicit_empty_taxes_are_not_replaced(client):
    h = await _register(client, "empty-country@test.example")
    await client.patch("/companies/me/taxes", headers=h, json={"taxes": []})
    await _set_country(client, h, "AU")
    assert (await client.get("/companies/me/taxes", headers=h)).json() == []


async def test_saved_currency_is_kept_while_untouched_taxes_are_seeded(client):
    h = await _register(client, "saved-ccy@test.example")
    r = await client.patch("/companies/me/books", headers=h, json={"currency": "EUR"})
    assert r.status_code == 200, r.text
    await _set_country(client, h, "AU")
    s = await _settings(client, h)
    assert s["currency"] == "EUR"
    assert s["taxes"] == TAX_REGIMES["AU"]["taxes"]


async def test_financially_active_company_keeps_currency_and_taxes(client):
    h = await _register(client, "active@test.example")
    await _post_manual_entry(client, h)
    before = await _settings(client, h)
    await _set_country(client, h, "AU")
    after = await _settings(client, h)
    assert after.get("currency") == before.get("currency")
    assert after.get("taxes") == before.get("taxes")


# --- Spanish copy ----------------------------------------------------------

def test_spanish_navigation_and_issue_date_strings():
    es = json.loads((Path(__file__).resolve().parents[1] / "ui" / "locales" / "es.json").read_text())
    assert es["nav.lists"] == "Listas"
    assert es["doc.issue_date"] == "Fecha de emisión:"



# --- who set the taxes -----------------------------------------------------

async def test_taxes_saved_at_a_catalog_rate_are_the_company_own(client):
    """Saving the very rates a country's catalog holds still makes them the company's own:
    naming another country afterwards keeps them."""
    h = await _register(client, "same-rates@test.example")
    r = await client.patch("/companies/me/taxes", headers=h, json={"taxes": TAX_REGIMES["TH"]["taxes"]})
    assert r.status_code == 200, r.text
    await _set_country(client, h, "AU")
    s = await _settings(client, h)
    assert s["taxes"] == TAX_REGIMES["TH"]["taxes"] and "taxes_seeded_for" not in s


async def test_imported_taxes_are_the_company_own(client):
    h = await _register(client, "imported-rates@test.example")
    await _set_country(client, h, "TH")
    r = await client.post("/companies/me/taxes/import/batch", headers=h, json={"records": [
        {"entity_id": "tax:levy", "event_type": "sys.tax.created", "source": "csv_import",
         "idempotency_key": uuid.uuid4().hex, "data": {"name": "Local levy", "rate": 3.0, "tax_type": "both"}}]})
    assert r.status_code == 200, r.text
    imported = (await _settings(client, h))["taxes"]
    await _set_country(client, h, "AU")
    assert (await _settings(client, h))["taxes"] == imported


async def test_seeded_taxes_follow_a_country_change_and_stay_on_repeated_edits(client):
    h = await _register(client, "moves@test.example")
    await _set_country(client, h, "TH")
    assert (await _settings(client, h))["taxes_seeded_for"] == "TH"
    await _set_country(client, h, "TH")
    assert (await _settings(client, h))["taxes"] == TAX_REGIMES["TH"]["taxes"]
    await _set_country(client, h, "AU")
    s = await _settings(client, h)
    assert (s["taxes"], s["taxes_seeded_for"]) == (TAX_REGIMES["AU"]["taxes"], "AU")


async def test_taxes_stored_before_their_source_was_recorded_are_kept(client, session):
    """A company whose taxes match a catalog but carry no record of who set them may have
    chosen them: they are kept."""
    from sqlalchemy import select

    from celerp.models.company import Company

    h = await _register(client, "legacy-rates@test.example")
    from celerp.services.company_lock import locked_company

    company_id = (await session.execute(select(Company.id).where(Company.name == "Tax Co")
                                        .order_by(Company.created_at.desc()))).scalars().first()
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "taxes": TAX_REGIMES["TH"]["taxes"]}
    await session.commit()
    await _set_country(client, h, "AU")
    assert (await _settings(client, h))["taxes"] == TAX_REGIMES["TH"]["taxes"]

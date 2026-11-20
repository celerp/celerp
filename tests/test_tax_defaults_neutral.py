# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tax and payment-term defaults are neutral, reads never write, and Update localization
adds a country's taxes without changing the currency or any tax the company already has."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from celerp.services.currencies import CURRENCIES
from celerp.tax_regimes import DEFAULT_REGIME, get_regime, tax_countries
from test_helpers import save_taxes
from ui.i18n import t

_GENERIC = DEFAULT_REGIME["taxes"]
_CUSTOM_STANDARD = [{**_GENERIC[0], "versions": [{"rate": 12.0}]}, _GENERIC[1]]


async def _register(client, email: str) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Tax Co", "email": email, "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _settings(client, h) -> dict:
    r = await client.get("/companies/me", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["settings"]



async def _update_localization(client, h, country: str) -> None:
    """Name the company's country, then preview and confirm Update localization."""
    r = await client.patch("/companies/me", headers=h, json={"settings": {"country": country}})
    assert r.status_code == 200, r.text
    r = await client.post("/settings/taxes/localization/preview", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post("/settings/taxes/localization/confirm", headers=h, json={
        "binding": r.json()["fingerprint"], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


def _names(taxes: list[dict]) -> list[str]:
    return [tax["name"] for tax in taxes]


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
    r = await save_taxes(client, h, [])
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
    r = await save_taxes(client, h, [{**_GENERIC[0], "name": "Input VAT", "versions": [{"rate": 5.0}]}], "purchase")
    assert r.status_code == 200, r.text
    own = (await client.get("/companies/me/purchasing-taxes", headers=h)).json()
    assert (await save_taxes(client, h, _CUSTOM_STANDARD)).status_code == 200
    assert (await client.get("/companies/me/purchasing-taxes", headers=h)).json() == own


# --- country seeding -------------------------------------------------------

async def test_update_localization_replaces_a_never_saved_neutral_list_with_the_pack(client):
    h = await _register(client, "fresh-th@test.example")
    currency = (await _settings(client, h)).get("currency")
    await _update_localization(client, h, "TH")
    s = await _settings(client, h)
    assert s.get("currency") == currency
    assert _names(s["taxes"]) == _names(get_regime("TH")["taxes"])


async def test_customized_standard_tax_with_the_same_name_is_not_overwritten(client):
    h = await _register(client, "custom-std@test.example")
    r = await save_taxes(client, h, _CUSTOM_STANDARD)
    assert r.status_code == 200, r.text
    saved = (await client.get("/companies/me/taxes", headers=h)).json()
    await _update_localization(client, h, "AU")
    assert (await client.get("/companies/me/taxes", headers=h)).json()[:2] == saved


async def test_saved_currency_is_kept_when_a_country_pack_is_applied(client):
    h = await _register(client, "saved-ccy@test.example")
    r = await client.patch("/companies/me/books", headers=h, json={"currency": "EUR"})
    assert r.status_code == 200, r.text
    await _update_localization(client, h, "AU")
    s = await _settings(client, h)
    assert s["currency"] == "EUR"
    assert _names(s["taxes"]) == _names(get_regime("AU")["taxes"])


async def test_financially_active_company_keeps_currency_and_its_taxes(client):
    h = await _register(client, "active@test.example")
    await _post_manual_entry(client, h)
    r = await save_taxes(client, h, _CUSTOM_STANDARD)
    assert r.status_code == 200, r.text
    before = await _settings(client, h)
    await _update_localization(client, h, "AU")
    after = await _settings(client, h)
    assert after.get("currency") == before.get("currency")
    assert after["taxes"][:len(before["taxes"])] == before["taxes"]


# --- Spanish copy ----------------------------------------------------------

def test_spanish_navigation_and_issue_date_strings():
    es = json.loads((Path(__file__).resolve().parents[1] / "ui" / "locales" / "es.json").read_text())
    assert es["nav.lists"] == "Presupuestos"
    assert es["doc.issue_date"] == "Fecha de emisión:"



# --- every country pack ----------------------------------------------------

@pytest.mark.real_packs
@pytest.mark.parametrize("country", tax_countries())
async def test_every_tax_country_starts_with_exactly_its_catalog(client, country):
    regime = get_regime(country)
    assert t(f"country.{country}") != f"country.{country}"
    assert regime["currency"] in CURRENCIES
    assert len([x for x in regime["taxes"] if x.get("is_default")]) == 1
    h = await _register(client, f"catalog-{country.lower()}@test.example")
    await _update_localization(client, h, country)
    assert _names((await _settings(client, h))["taxes"]) == _names(regime["taxes"])

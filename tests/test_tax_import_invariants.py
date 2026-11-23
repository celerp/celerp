"""A tax import keeps the same invariants as every other tax list writer: names are told
apart as tax_name_key compares them, and a rate must be a finite number. Both import
endpoints are held to it, through the TaxRate model the PATCH routes take."""

from __future__ import annotations

import uuid

import pytest

from test_helpers import refusal_keys

_ENDPOINTS = {
    "/companies/me/taxes/import/batch": "/companies/me/taxes",
    "/companies/me/purchasing-taxes/import/batch": "/companies/me/purchasing-taxes",
}


async def _register(client) -> dict:
    suffix = uuid.uuid4().hex[:10]
    r = await client.post("/auth/register", json={
        "company_name": f"Co {suffix}", "email": f"admin_{suffix}@test.example",
        "name": "Admin", "password": "pw123val",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _rows(*rows: tuple[str, object]) -> dict:
    return {"records": [{
        "entity_id": "company", "event_type": "tax.import", "source": "import",
        "idempotency_key": f"tax-{i}-{name}", "data": {"name": name, "rate": rate},
    } for i, (name, rate) in enumerate(rows)]}


async def _import(client, headers, endpoint, *rows) -> dict:
    r = await client.post(endpoint, json=_rows(*rows), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _names(client, headers, endpoint) -> list[str]:
    r = await client.get(_ENDPOINTS[endpoint], headers=headers)
    assert r.status_code == 200, r.text
    return [tax["name"] for tax in r.json()]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", list(_ENDPOINTS))
async def test_a_name_tax_name_key_folds_to_a_held_tax_is_skipped(client, endpoint):
    headers = await _register(client)
    first = await _import(client, headers, endpoint, ("Straße Levy", 2))
    assert first["created"] == 1, first
    # casefold, unlike lower, folds the sharp s, so this is the same tax.
    again = await _import(client, headers, endpoint, ("  STRASSE LEVY ", 3))
    assert (again["created"], again["skipped"], again["errors"]) == (0, 1, []), again
    names = await _names(client, headers, endpoint)
    assert [n for n in names if "levy" in n.casefold()] == ["Straße Levy"], names


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", list(_ENDPOINTS))
async def test_a_name_twice_in_one_batch_is_created_once(client, endpoint):
    headers = await _register(client)
    body = await _import(client, headers, endpoint, ("Eco Tax", 1), ("ECO TAX", 1), ("eco tax ", 4))
    assert (body["created"], body["skipped"], body["errors"]) == (1, 2, []), body
    names = await _names(client, headers, endpoint)
    assert [n for n in names if n.casefold() == "eco tax"] == ["Eco Tax"], names


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", list(_ENDPOINTS))
@pytest.mark.parametrize("rate", ["nan", "inf", "-inf", "1e999", "NaN"])
async def test_a_rate_that_is_not_finite_is_refused_per_row(client, endpoint, rate):
    headers = await _register(client)
    body = await _import(client, headers, endpoint, ("Odd Tax", rate), ("Fine Tax", "5"))
    assert body["created"] == 1, body
    assert refusal_keys(body["errors"]) == ["settings.rate_must_be_a_number"], body
    names = await _names(client, headers, endpoint)
    assert "Odd Tax" not in names and "Fine Tax" in names, names


_CHECKED_BY = {"TaxRatesPatch", "_import_taxes", "get_regime"}


def _tax_list_writers() -> dict[str, set[str]]:
    """Each function of the companies router that writes a tax list (with_taxes, or a
    settings["taxes"] / settings["purchasing_taxes"] assignment), with the names it uses."""
    import ast
    from pathlib import Path

    import celerp.routers.companies as companies

    tree = ast.parse(Path(companies.__file__).read_text(encoding="utf-8"))
    writers = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        names |= {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assigns = any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                      and t.slice.value in ("taxes", "purchasing_taxes")
                      for n in ast.walk(fn) if isinstance(n, ast.Assign) for t in n.targets)
        if assigns or "with_taxes" in names:
            writers[fn.name] = names
    return writers


def test_every_tax_list_writer_takes_its_taxes_through_the_shared_checks():
    """A tax list is written only from a TaxRatesPatch, from _import_taxes, or from a country
    pack (get_regime, whose taxes passed the CountryPack schema): each holds names unique as
    tax_name_key compares them and rates finite. A new writer that skips them fails here."""
    writers = _tax_list_writers()
    assert {"patch_taxes", "patch_purchasing_taxes", "import_taxes_batch",
            "import_purchasing_taxes_batch", "_apply_default_location"} <= set(writers), sorted(writers)
    unchecked = sorted(name for name, used in writers.items() if not used & _CHECKED_BY)
    assert unchecked == [], unchecked

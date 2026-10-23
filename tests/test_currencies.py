# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The currencies Celerp keeps books in: ISO 4217's active circulating currencies, each at
its ISO minor units, offered in every currency picker; and the codes ISO 4217 has withdrawn,
which records made before still carry and still show, but which nothing new may use."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from celerp.services.currencies import CURRENCIES, CURRENCY_CODES, HISTORICAL, KNOWN_CURRENCY_CODES
from celerp.services.money import currency_dp, doc_rate, require_doc_rate, round_money
from test_helpers import make_test_token

ROOT = Path(__file__).resolve().parents[1]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client) -> str:
    r = await client.post("/auth/register", json={"company_name": "Birr Co", "email": f"{uuid.uuid4().hex[:8]}@test.test",
                                                  "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ── The list ──────────────────────────────────────────────────────────────────

def test_the_offered_currencies_are_iso_4217s_active_circulating_currencies():
    assert list(CURRENCIES) == sorted(set(CURRENCIES)) and len(CURRENCIES) == 155
    assert {"ETB", "ZWG", "XCG", "SSP", "SLE", "VED", "VES", "MRU", "STN", "USD", "EUR"} <= CURRENCY_CODES
    # Withdrawn: replaced by XCG and ZWG, Croatia and Bulgaria now on the euro.
    assert set(HISTORICAL) == {"ANG", "BGN", "HRK", "ZWL"} and not CURRENCY_CODES & set(HISTORICAL)
    assert not {"SLL", "CUC", "VEF", "LTL"} & KNOWN_CURRENCY_CODES
    # Funds, metals, bond units and test codes are not currencies books are kept in.
    assert not {"CLF", "XDR", "XAU", "XAG", "XTS", "XXX", "BOV", "USN", "XSU", "XUA", "UYI"} & KNOWN_CURRENCY_CODES


def test_every_known_currency_has_its_name_and_only_those_do():
    en = json.loads((ROOT / "ui" / "locales" / "en.json").read_text(encoding="utf-8"))
    named = {k.removeprefix("currency.name.") for k in en if k.startswith("currency.name.")}
    assert named == KNOWN_CURRENCY_CODES
    assert en["currency.name.ETB"] == "Ethiopian Birr" and en["currency.name.XCG"] == "Caribbean Guilder"


def test_no_other_list_of_currencies_is_kept():
    """One list: no module outside celerp/services/currencies.py spells out the codes."""
    run = re.compile(r'"AED",\s*"AFN",\s*"ALL"|"AED",\s*"AUD",\s*"BDT"')
    found = [p.relative_to(ROOT).as_posix() for top in ("celerp", "ui", "default_modules")
             for p in (ROOT / top).rglob("*.py") if run.search(p.read_text(errors="ignore"))]
    assert found == ["celerp/services/currencies.py"]


@pytest.mark.parametrize("code, places", [
    ("JPY", 0), ("CLP", 0), ("XPF", 0), ("ISK", 0), ("KRW", 0), ("VND", 0), ("UGX", 0),
    ("IDR", 2), ("ETB", 2), ("USD", 2), ("XCG", 2), ("ZWG", 2), ("MGA", 2), ("HUF", 2),
    ("IQD", 3), ("LYD", 3), ("KWD", 3), ("TND", 3), ("BHD", 3),
])
def test_money_is_kept_at_each_currencys_iso_minor_units(code, places):
    assert currency_dp(code) == places
    assert round_money(Decimal("1234.56789"), code) == Decimal("1234.56789").quantize(
        Decimal(1).scaleb(-places), rounding="ROUND_HALF_UP")


def test_the_setup_guess_suggests_only_currencies_celerp_offers():
    script = (ROOT / "ui" / "static" / "currency-guess.js").read_text()
    guessed = json.loads(re.search(r"COUNTRY_CURRENCY = (\{.*?\});", script).group(1))
    assert set(guessed.values()) <= CURRENCY_CODES
    assert (guessed["ET"], guessed["CW"], guessed["HR"], guessed["SL"]) == ("ETB", "XCG", "EUR", "SLE")


def test_a_picker_offers_every_currency_and_keeps_a_records_withdrawn_one():
    from ui.components.currency import currency_label, currency_options

    assert [code for code, _ in currency_options()] == list(CURRENCIES)
    assert [code for code, _ in currency_options("ETB")] == list(CURRENCIES)
    assert [code for code, _ in currency_options("HRK")] == ["HRK", *CURRENCIES]
    assert currency_label("ETB") == "ETB – Ethiopian Birr"
    assert currency_label("HRK") == "HRK – Croatian Kuna"


# ── Books kept in Birr ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_company_keeps_its_books_in_birr_and_posts_an_invoice_in_it(client, session):
    from celerp.models.ledger import LedgerEntry

    h = _auth(await _register(client))
    r = await client.patch("/companies/me/books", headers=h, json={"currency": "ETB"})
    assert r.status_code == 200, r.text
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "contact:1", "currency": "ETB",
        "line_items": [{"name": "Paper", "quantity": 3, "unit_price": 411.525, "line_total": 1234.575}],
        "subtotal": 1234.575, "tax": 0.0, "total": 1234.575})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=h)
    assert r.status_code == 200, r.text
    rows = (await session.execute(select(LedgerEntry).where(
        LedgerEntry.entity_id.like(f"je:auto:{doc_id}:fin%"),
        LedgerEntry.event_type == "acc.journal_entry.created"))).scalars().all()
    receivable = [e for row in rows for e in row.data["entries"] if e.get("debit")]
    # Kept at the birr's two places: 1,234.575 is 1,234.58.
    assert [round(float(e["debit"]), 3) for e in receivable] == [1234.58]


@pytest.mark.asyncio
async def test_a_birr_invoice_with_no_rate_in_dollar_books_has_no_rate_and_is_not_posted(client):
    assert doc_rate({"currency": "ETB"}, "USD") is None
    with pytest.raises(ValueError):
        require_doc_rate({"currency": "ETB"}, "USD")
    h = _auth(await _register(client))
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_id": "contact:1", "currency": "ETB",
        "line_items": [{"name": "Paper", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
        "subtotal": 100.0, "tax": 0.0, "total": 100.0})
    assert r.status_code == 200, r.text
    finalize = await client.post(f"/docs/{r.json()['id']}/finalize", headers=h)
    assert finalize.status_code == 422 and "exchange rate" in finalize.json()["detail"].lower()


# ── Withdrawn currencies ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_books_kept_in_a_withdrawn_currency_keep_it_and_none_move_to_one(client, session):
    from jose import jwt

    from celerp.config import settings
    from celerp.services.company_lock import locked_company

    token = await _register(client)
    r = await client.patch("/companies/me/books", headers=_auth(token), json={"currency": "HRK"})
    assert r.status_code == 422, r.text

    company = await locked_company(session, uuid.UUID(jwt.decode(token, settings.jwt_secret,
                                                                 algorithms=["HS256"])["company_id"]))
    company.settings = {**(company.settings or {}), "currency": "HRK"}
    await session.commit()
    r = await client.patch("/companies/me/books", headers=_auth(token), json={"currency": "HRK"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_contact_kept_in_a_withdrawn_currency_keeps_it_and_none_moves_to_one(client, session):
    from celerp.models.projections import Projection

    h = _auth(await _register(client))
    ids = []
    for name in ("Old Supplier", "New Supplier"):
        r = await client.post("/crm/contacts", headers=h, json={"name": name, "currency": "EUR"})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    old = (await session.execute(select(Projection).where(Projection.entity_id == ids[0]))).scalar_one()
    old.state = {**old.state, "currency": "HRK"}
    await session.commit()

    async def change(cid: str, before: str) -> int:
        return (await client.patch(f"/crm/contacts/{cid}", headers=h, json={
            "fields_changed": {"currency": {"old": before, "new": "HRK"}}})).status_code

    assert await change(ids[0], "HRK") == 200
    assert await change(ids[1], "EUR") == 422


# ── The pickers ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


@pytest.mark.asyncio
async def test_the_bank_account_forms_offer_every_currency_in_a_searchable_picker(ui_client):
    bank = {"id": "b1", "bank_name": "Old Bank", "account_number": "1", "bank_type": "checking",
            "chart_account_code": "1112", "currency": "HRK", "balance": 0.0, "is_active": True}
    with patch("ui.api_client.get_company", new=AsyncMock(return_value={"currency": "ETB"})), \
         patch("ui.api_client.get_bank_account", new=AsyncMock(return_value=bank)):
        new = await ui_client.get("/settings/accounting/bank-accounts/new")
        edit = await ui_client.get("/settings/accounting/bank-accounts/b1/edit")
    for page in (new, edit):
        assert page.status_code == 200, page.text
        assert "combobox-input" in page.text
        assert sum(f'data-value="{code}"' in page.text for code in CURRENCIES) == len(CURRENCIES)
    assert 'value="ETB"' in new.text and "ETB – Ethiopian Birr" in new.text
    # An account kept in a withdrawn currency keeps it on its form.
    assert 'data-value="HRK"' in edit.text and 'data-value="HRK"' not in new.text

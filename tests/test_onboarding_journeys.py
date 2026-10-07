"""End-to-end onboarding and import journeys, driven through the real API and UI apps.

Every journey runs against the real API over ASGI; the UI app talks to that same
API through a bridged transport, so each step is the one a person in a browser
would take, without a browser. Each journey asserts the end state the owner
cares about: company settings, items, units, categories, import history, staged
files and where the user lands.
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ui.routes import csv_import as ci

_PASSWORD = "pwvalid1"


# ---------------------------------------------------------------------------
# Harness: a UI client whose API calls reach the real API app
# ---------------------------------------------------------------------------


@pytest.fixture
def stage_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    return tmp_path / "import_staging"


@contextmanager
def _bridged(**overrides):
    """Route every UI API call to the real API app; the restart call is a no-op.

    ``overrides`` replaces named ``ui.api_client`` functions for one step, e.g. to
    make one API call fail.
    """
    from contextlib import ExitStack

    from celerp.main import app as api_app

    def _transport():
        return ASGITransport(app=api_app)

    with ExitStack() as stack:
        stack.enter_context(patch("ui.api_client._get_transport", side_effect=_transport))
        stack.enter_context(patch("ui.api_client._get_bulk_transport", side_effect=_transport))
        stack.enter_context(patch("ui.api_client.restart_system", new=AsyncMock(return_value={})))
        for name, value in overrides.items():
            stack.enter_context(patch(f"ui.api_client.{name}", new=value))
        yield


async def _ui(method: str, path: str, token: str | None, **kwargs):
    from ui.app import app as ui_app
    cookies = {"celerp_token": token} if token else {}
    cookies.update(kwargs.pop("cookies", None) or {})
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
        return await c.request(method, path, cookies=cookies, **kwargs)


async def _register(client, session, *, name: str = "Journey Co") -> dict:
    from celerp.services.auth import decode_access_token
    from celerp.services.session_tracker import clear as clear_tracker
    email = f"journey-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/auth/register", json={
        "company_name": name, "email": email, "name": "Owner", "password": _PASSWORD,
    })
    assert r.status_code == 200, r.text
    await clear_tracker(session)
    token = r.json()["access_token"]
    claims = decode_access_token(token)
    return {
        "token": token, "h": {"Authorization": f"Bearer {token}"}, "email": email,
        "company_id": claims["company_id"], "user_id": claims["sub"],
    }


async def _settings(session, company_id: str) -> dict:
    from celerp.models.company import Company
    session.expire_all()
    company = await session.get(Company, uuid.UUID(str(company_id)))
    return dict(company.settings or {})


async def _set_settings(session, company_id: str, **values) -> None:
    from celerp.services.company_lock import locked_company
    company = await locked_company(session, uuid.UUID(str(company_id)))
    company.settings = {**(company.settings or {}), **values}
    await session.commit()


async def _items(session, company_id: str) -> list[dict]:
    from sqlalchemy import select

    from celerp.models.projections import Projection
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == uuid.UUID(str(company_id)), Projection.entity_type == "item",
    ))).scalars().all()
    # Sample items seeded at sign-up are not the user's own data.
    return [dict(p.state, _entity_id=p.entity_id) for p in rows if "demo-" not in p.entity_id]


async def _batches(session, company_id: str) -> list:
    from sqlalchemy import select

    from celerp.models.import_batch import ImportBatch
    session.expire_all()
    return list((await session.execute(select(ImportBatch).where(
        ImportBatch.company_id == uuid.UUID(str(company_id)),
    ))).scalars().all())


async def _ledger_count(session, company_id: str) -> int:
    from sqlalchemy import func, select

    from celerp.models.ledger import LedgerEntry
    session.expire_all()
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == uuid.UUID(str(company_id)),
    ))).scalar_one()


async def _unit_names(client, h) -> set[str]:
    from celerp.services.units import DEFAULT_UNITS
    settings = (await client.get("/companies/me", headers=h)).json()["settings"]
    return {u["name"] for u in settings.get("units") or DEFAULT_UNITS}


def _hidden(html: str, name: str) -> str | None:
    """Value of the hidden input called ``name`` in rendered HTML, if any."""
    for tag in re.findall(r"<input[^>]*>", html):
        if re.search(rf'name="{re.escape(name)}"', tag):
            m = re.search(r'value="([^"]*)"', tag)
            return m.group(1) if m else ""
    return None


def _offers_confirm(html: str) -> bool:
    """A clean final review: a confirm form carrying a preview hash."""
    return 'hx-post="/inventory/import/confirm"' in html and bool(_hidden(html, "preview_hash"))


def _csv(header: list[str], rows: list[list]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


async def _ui_setup(token: str, vertical: str, *, currency: str = "USD", **overrides):
    with _bridged(**overrides):
        return await _ui("POST", "/setup/company", token, data={
            "vertical": vertical, "currency": currency, "timezone": "UTC",
        })


async def _lands_on_onboarding(r, token: str) -> None:
    """A successful setup reaches the getting-started hub, directly or after activation."""
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location in ("/onboarding", "/setup/activating"), location
    if location == "/setup/activating":
        with _bridged():
            page = await _ui("GET", "/setup/activating", token)
        assert "window.location.href = '/onboarding'" in page.text
    with _bridged():
        hub = await _ui("GET", "/onboarding", token)
    assert hub.status_code == 200, hub.text


async def _upload(token: str, text: str, *, filename: str = "items.csv", cookies: dict | None = None):
    with _bridged():
        r = await _ui("POST", "/inventory/import/preview", token, cookies=cookies,
                      files={"csv_file": (filename, text.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    ref = _hidden(r.text, "csv_ref")
    assert ref and ref.startswith("imp_"), r.text
    return r, ref


async def _map(token: str, ref: str, mapping: dict[str, str], *, cookies: dict | None = None):
    form = {"csv_ref": ref, **{f"map__{col}": target for col, target in mapping.items()}}
    with _bridged():
        r = await _ui("POST", "/inventory/import/mapped", token, data=form, cookies=cookies)
    assert r.status_code == 200, r.text
    return r


async def _confirm(token: str, ref: str, preview_hash: str, *, cookies: dict | None = None):
    with _bridged():
        r = await _ui("POST", "/inventory/import/confirm", token, cookies=cookies,
                      data={"csv_ref": ref, "preview_hash": preview_hash})
    assert r.status_code == 200, r.text
    return r


def _stage_rows(stage_dir, ref: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO((stage_dir / f"{ref}.csv").read_text(encoding="utf-8"))))


# ---------------------------------------------------------------------------
# Setup transition table
# ---------------------------------------------------------------------------


def _fail_on(real, key: str):
    """patch_company that fails only when the payload carries ``key``."""
    from ui.api_client import APIError

    async def _patch(token, data):
        if key in data:
            raise APIError(503, "Service temporarily unavailable")
        return await real(token, data)
    return _patch


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [
    "invalid_empty_vertical",
    "invalid_unknown_vertical",
    "valid_vertical_preset_succeeds",
    "valid_vertical_preset_fails",
    "valid_vertical_pending_write_fails",
    "explicit_blank",
])
async def test_setup_transition_table(row, client, session):
    import ui.api_client as api
    from ui.api_client import APIError
    from celerp.config import read_config
    from celerp.services.vertical_presets import load_preset

    owner = await _register(client, session)
    before = await _settings(session, owner["company_id"])
    modules_before = (read_config().get("modules") or {}).get("enabled")

    if row.startswith("invalid"):
        vertical = "" if row == "invalid_empty_vertical" else "no_such_business_type"
        r = await _ui_setup(owner["token"], vertical)
        # Remain on setup, with nothing about the company or its modules changed.
        assert r.status_code == 200 and "location" not in r.headers
        assert 'action="/setup/company"' in r.text
        assert await _settings(session, owner["company_id"]) == before
        assert (read_config().get("modules") or {}).get("enabled") == modules_before
        return

    if row == "valid_vertical_preset_succeeds":
        r = await _ui_setup(owner["token"], "gemstones")
        after = await _settings(session, owner["company_id"])
        assert after.get("vertical") == "gemstones"
        assert set(load_preset("gemstones")["categories"]) <= set(after.get("category_schemas") or {})
        assert after.get("onboarding_pending") is True
        await _lands_on_onboarding(r, owner["token"])
        return

    if row == "valid_vertical_preset_fails":
        r = await _ui_setup(owner["token"], "gemstones",
                            set_business_type=AsyncMock(side_effect=APIError(500, "Preset could not be applied")))
        assert r.status_code == 200 and "location" not in r.headers
        assert 'action="/setup/company"' in r.text
        after = await _settings(session, owner["company_id"])
        assert "onboarding_pending" not in after
        assert not after.get("category_schemas")
        # Recoverable: the same submission succeeds once the preset can be applied.
        retry = await _ui_setup(owner["token"], "gemstones")
        assert (await _settings(session, owner["company_id"])).get("onboarding_pending") is True
        await _lands_on_onboarding(retry, owner["token"])
        return

    if row == "valid_vertical_pending_write_fails":
        r = await _ui_setup(owner["token"], "gemstones", patch_company=_fail_on(api.patch_company, "onboarding_pending"))
        # A setup whose finishing write failed is not reported as finished.
        assert r.status_code == 200 and "location" not in r.headers, (
            f"setup redirected to {r.headers.get('location')} although the pending flag was not stored")
        assert 'action="/setup/company"' in r.text
        assert "onboarding_pending" not in await _settings(session, owner["company_id"])
        retry = await _ui_setup(owner["token"], "gemstones")
        assert (await _settings(session, owner["company_id"])).get("onboarding_pending") is True
        await _lands_on_onboarding(retry, owner["token"])
        return

    assert row == "explicit_blank"
    r = await _ui_setup(owner["token"], "blank")
    after = await _settings(session, owner["company_id"])
    assert not after.get("category_schemas")
    assert not after.get("category_display_names")
    assert "inventory_method" not in after
    assert after.get("onboarding_pending") is True
    await _lands_on_onboarding(r, owner["token"])


# ---------------------------------------------------------------------------
# Inventory transition table, as one browserless chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inventory_transition_chain_without_browser(client, session, stage_dir):
    from ui.routes.inventory import _import_operation_key

    owner = await _register(client, session)
    cid, token = owner["company_id"], owner["token"]
    await _set_settings(session, cid, category_schemas={"red_a": [], "red_b": []},
                        category_display_names={"red_a": "Red", "red_b": "Rouge"})
    header = ["Item", "Unit", "Qty", "Group"]
    text = _csv(header, [["Widget", "furlong", "2", "Red"], ["Gadget", "piece", "3", ""]])
    full_map = {"Item": "name", "Unit": "sell_by", "Qty": "quantity", "Group": "category"}

    # Upload stages the file for this company.
    page, ref = await _upload(token, text)
    assert (stage_dir / f"{ref}.csv").exists()
    assert 'action="/inventory/import/mapped"' in page.text

    # Mapping without the required name target stays on mapping.
    r = await _map(token, ref, {**full_map, "Item": ci.MAPPING_SKIP})
    assert 'action="/inventory/import/mapped"' in r.text
    assert not _offers_confirm(r.text)
    assert await _items(session, cid) == []

    # A complete mapping reaches cell fixes for the bad unit.
    r = await _map(token, ref, full_map)
    assert 'hx-post="/inventory/import/revalidate"' in r.text
    assert not _offers_confirm(r.text)
    fix_ref = _hidden(r.text, "csv_ref")
    with _bridged():
        r = await _ui("POST", "/inventory/import/revalidate", token,
                      data={"csv_ref": fix_ref, "fixes_json": json.dumps({"0__sell_by": "piece"})})
    # Once every cell is valid, the server's semantic preview is the final review.
    assert _offers_confirm(r.text), r.text
    ref, good_hash = _hidden(r.text, "csv_ref"), _hidden(r.text, "preview_hash")
    rows = _stage_rows(stage_dir, ref)

    # Stale hash: nothing is written and the review is shown again.
    r = await _confirm(token, ref, "0" * 64)
    assert await _items(session, cid) == [] and await _batches(session, cid) == []
    assert _offers_confirm(r.text)

    # Semantic drift after the preview: the label now names two categories.
    await _set_settings(session, cid, category_display_names={"red_a": "Red", "red_b": "Red"})
    r = await _confirm(token, ref, good_hash)
    assert await _items(session, cid) == [] and await _batches(session, cid) == []
    assert not _offers_confirm(r.text)

    # A semantic error in the preview never enables confirm.
    with _bridged():
        r = await _ui("POST", "/inventory/import/review", token, data={"csv_ref": ref})
    assert not _offers_confirm(r.text)
    assert "Red" in r.text

    # Success goes through the canonical importer.
    await _set_settings(session, cid, category_display_names={"red_a": "Red", "red_b": "Rouge"})
    with _bridged():
        r = await _ui("POST", "/inventory/import/review", token, data={"csv_ref": ref})
    assert _offers_confirm(r.text), r.text
    good_hash = _hidden(r.text, "preview_hash")
    r = await _confirm(token, ref, good_hash)
    items = await _items(session, cid)
    assert sorted((i["name"], i["sell_by"], float(i["quantity"]), i.get("category") or "") for i in items) == [
        ("Gadget", "piece", 3.0, ""), ("Widget", "piece", 2.0, "red_a"),
    ]
    batches = await _batches(session, cid)
    assert len(batches) == 1 and batches[0].row_count == 2
    assert sorted(batches[0].entity_ids) == sorted(i["_entity_id"] for i in items)
    assert not (stage_dir / f"{ref}.csv").exists()
    ledger = await _ledger_count(session, cid)

    # Exact retry, from the browser and on the wire: no second business effect.
    await _confirm(token, ref, good_hash)
    replay = await client.post("/items/import/rows", headers=owner["h"], json={
        "rows": rows, "upsert": False, "idempotency_key": _import_operation_key(rows, False),
        "preview_hash": good_hash,
    })
    assert replay.status_code == 200, replay.text
    assert len(await _items(session, cid)) == 2
    assert len(await _batches(session, cid)) == 1
    assert await _ledger_count(session, cid) == ledger


# ---------------------------------------------------------------------------
# J1 - no-data service business
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("vertical", ["consulting", "blank"])
async def test_journey_j1_no_data_service_business(vertical, client, session, stage_dir):
    owner = await _register(client, session)
    cid, token, h = owner["company_id"], owner["token"], owner["h"]

    r = await _ui_setup(token, vertical)
    await _lands_on_onboarding(r, token)
    with _bridged():
        assert (await _ui("GET", "/", token)).headers["location"] == "/onboarding"
        hub = await _ui("GET", "/onboarding", token)
    # Starting manually needs no import, store or cloud connection first.
    assert 'action="/onboarding/complete"' in hub.text
    assert await _items(session, cid) == []

    # Starting is truthful: if the choice cannot be saved, the user is told and can retry.
    import ui.api_client as api
    with _bridged(patch_company=_fail_on(api.patch_company, "onboarding_pending")):
        failed = await _ui("POST", "/onboarding/complete", token)
    assert failed.headers.get("location") != "/dashboard", "completion reported success but was not saved"
    assert (await _settings(session, cid)).get("onboarding_pending") is True
    with _bridged():
        assert (await _ui("GET", "/", token)).headers["location"] == "/onboarding"

    with _bridged():
        done = await _ui("POST", "/onboarding/complete", token)
    assert done.status_code == 303 and done.headers["location"] == "/dashboard"
    assert (await _settings(session, cid)).get("onboarding_pending") is False
    with _bridged():
        assert (await _ui("GET", "/", token)).headers["location"] == "/dashboard"

    customer = await client.post("/crm/contacts", headers=h, json={
        "name": "First Customer", "email": "customer@example.com", "contact_type": "customer",
    })
    assert customer.status_code == 200, customer.text

    if vertical == "consulting":
        # The Consulting category makes a new item a service without the user saying so.
        service = await client.post("/items", headers=h, json={
            "name": "Strategy session", "sell_by": "piece", "category": "consulting_service",
        })
    else:
        service = await client.post("/items", headers=h, json={
            "name": "Strategy session", "sell_by": "piece", "inventory_type": "service",
        })
    assert service.status_code == 200, service.text
    item = (await client.get(f"/items/{service.json()['id']}", headers=h)).json()
    assert item["inventory_type"] == "service", item
    contacts = (await client.get("/crm/contacts", headers=h)).json()
    names = [c.get("name") for c in (contacts.get("items") if isinstance(contacts, dict) else contacts)]
    assert "First Customer" in names


# ---------------------------------------------------------------------------
# J2 - gemstone spreadsheet
# ---------------------------------------------------------------------------

_J2_BASE = ["Name", "Category", "Weight (ct)", "Sell by", "Location"]
_J2_ROW = ["Ruby 1", "Ruby", "1.52", "carat", "Head Office"]
_J2_MAP = {
    "Name": "name", "Category": "category", "Weight (ct)": "weight",
    "Sell by": "sell_by", "Location": "location_name",
}


async def _gem_company(client, session) -> dict:
    owner = await _register(client, session, name="Gem Co")
    r = await _ui_setup(owner["token"], "gemstones", currency="THB")
    await _lands_on_onboarding(r, owner["token"])
    return owner


def _write_upload(owner: dict, text: str) -> str:
    from celerp.ai.files import upload_dir
    file_id = f"ai_up_{uuid.uuid4().hex}"
    data = text.encode()
    (upload_dir() / f"{file_id}.bin").write_bytes(data)
    (upload_dir() / f"{file_id}.meta").write_text(json.dumps({
        "filename": "stones.csv", "content_type": "text/csv", "size": len(data),
        "company_id": owner["company_id"], "user_id": owner["user_id"],
    }))
    return file_id


async def _assert_exact_or_blocked(client, session, owner: dict, text: str, mapping: dict, price_col: str,
                                   *, must_block: bool) -> None:
    """The two acceptable outcomes: an exact preview and import, or a block before commit.

    ``must_block`` marks sources whose meaning cannot be honoured as written. Both the browser importer and the file preview
    transport must agree, and a block leaves no item behind on either.
    """
    cid, token = owner["company_id"], owner["token"]
    _, ref = await _upload(token, text)
    r = await _map(token, ref, mapping)

    file_id = _write_upload(owner, text)
    preview = await client.post("/items/import/preview", headers=owner["h"],
                                json={"file_id": file_id, "mapping": mapping})
    assert preview.status_code == 200, preview.text
    body = preview.json()

    if not _offers_confirm(r.text):
        # Blocked in the browser: the file preview names the problem with a code,
        # and neither commit path writes anything.
        assert body["errors"], body
        assert all(e.get("code") and e.get("message") for e in body["errors"]), body["errors"]
        # The block names the price column, not some unrelated cell.
        price_fields = {price_col, mapping[price_col]}
        assert any(e.get("field") in price_fields for e in body["errors"]), body["errors"]
        commit = await client.post("/items/import/commit", headers=owner["h"], json={
            "file_id": file_id, "mapping": mapping, "preview_hash": body["preview_hash"],
        })
        assert commit.status_code == 422, commit.text
        assert await _items(session, cid) == [] and await _batches(session, cid) == []
        return

    # The source states a currency or basis Celerp cannot honour as written.
    assert not must_block, f"{price_col!r} reached a clean preview without clarification"
    # Previewed exactly: the value and unit the source wrote are the ones imported.
    assert body["errors"] == [], body
    await _confirm(token, _hidden(r.text, "csv_ref"), _hidden(r.text, "preview_hash"))
    items = await _items(session, cid)
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "Ruby 1" and item["category"] == "ruby"
    assert float(item["weight"]) == 1.52 and item["weight_unit"] == "carat", item
    assert item["sell_by"] == "carat" and float(item[mapping[price_col]]) == 45000.0, item


@pytest.mark.asyncio
@pytest.mark.parametrize("price_header,price_target,expect", [
    ("Price/ct (THB)", "retail_price", "exact_or_blocked"),
    ("Cost (USD)", "cost_price", "blocked"),
    ("usd cost", "cost_price", "blocked"),
    ("Price/box", "retail_price", "blocked"),
    ("Price ($)", "retail_price", "blocked"),
])
async def test_journey_j2_gemstone_spreadsheet(price_header, price_target, expect, client, session, stage_dir):
    owner = await _gem_company(client, session)
    settings = await _settings(session, owner["company_id"])
    assert settings.get("currency") == "THB"
    assert settings["category_display_names"]["ruby"] == "Ruby"

    text = _csv([*_J2_BASE, price_header], [[*_J2_ROW, "45000"]])
    mapping = {**_J2_MAP, price_header: price_target}
    await _assert_exact_or_blocked(client, session, owner, text, mapping, price_header,
                                   must_block=expect == "blocked")


# ---------------------------------------------------------------------------
# J3 - Food & Beverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journey_j3_food_beverage(client, session):
    from celerp.services.vertical_presets import _UNIT_FIELDS, load_category, load_preset

    owner = await _register(client, session, name="Food Co")
    r = await _ui_setup(owner["token"], "food_beverage")

    # Before the hub is reached the company already holds the canonical preset.
    settings = await _settings(session, owner["company_id"])
    assert settings.get("vertical") == "food_beverage"
    assert settings.get("inventory_method") == "fefo"
    assert settings.get("onboarding_pending") is True
    units = await _unit_names(client, owner["h"])
    for slug in load_preset("food_beverage")["categories"]:
        cat = load_category(slug)
        assert slug in settings["category_schemas"], slug
        assert settings["category_display_names"][slug] == cat.get("display_name", slug)
        for field in _UNIT_FIELDS:
            if cat.get(field):
                assert cat[field] in units, (slug, field, cat[field])
    await _lands_on_onboarding(r, owner["token"])


# ---------------------------------------------------------------------------
# J4 - retry and recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journey_j4_retry_recovery(client, session, stage_dir):
    from ui.routes.inventory import _import_operation_key

    owner = await _register(client, session)
    cid, token = owner["company_id"], owner["token"]
    # Larger than one write chunk, so a single import spans several writes.
    count = 501
    text = _csv(["Item", "Unit", "Qty"], [[f"Bead {n:04d}", "piece", "1"] for n in range(count)])

    _, ref = await _upload(token, text)
    r = await _map(token, ref, {"Item": "name", "Unit": "sell_by", "Qty": "quantity"})
    assert _offers_confirm(r.text), r.text
    ref, first_hash = _hidden(r.text, "csv_ref"), _hidden(r.text, "preview_hash")

    # Refresh the review: the same file and choice give the same preview.
    with _bridged():
        again = await _ui("POST", "/inventory/import/review", token, data={"csv_ref": ref})
    assert _hidden(again.text, "preview_hash") == first_hash
    rows = _stage_rows(stage_dir, ref)

    await _confirm(token, ref, first_hash)
    assert len(await _items(session, cid)) == count
    ledger = await _ledger_count(session, cid)

    # Submitting the exact confirm again, and replaying it on the wire, change nothing.
    await _confirm(token, ref, first_hash)
    replay = await client.post("/items/import/rows", headers=owner["h"], json={
        "rows": rows, "upsert": False, "idempotency_key": _import_operation_key(rows, False),
        "preview_hash": first_hash,
    })
    assert replay.status_code == 200, replay.text
    assert len(await _items(session, cid)) == count
    assert await _ledger_count(session, cid) == ledger

    # One logical import is one history entry covering every row.
    history = (await client.get("/items/import/batches", headers=owner["h"])).json()["batches"]
    assert len(history) == 1, [(b["row_count"], b["status"]) for b in history]
    assert history[0]["row_count"] == count and history[0]["status"] != "undone"


# ---------------------------------------------------------------------------
# J5 - tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journey_j5_tenant_isolation(client, session, stage_dir):
    owner = await _register(client, session, name="Company A")
    token_a, cid_a = owner["token"], owner["company_id"]

    created = await client.post("/companies", headers=owner["h"], json={"name": "Company B"})
    assert created.status_code == 200, created.text
    from celerp.services.auth import decode_access_token
    cid_b = decode_access_token(created.json()["access_token"])["company_id"]
    assert cid_b != cid_a

    # Stage under A.
    text = _csv(["Item", "Unit", "Qty"], [["Widget", "piece", "1"]])
    _, ref = await _upload(token_a, text)
    r = await _map(token_a, ref, {"Item": "name", "Unit": "sell_by", "Qty": "quantity"})
    staged_ref, staged_hash = _hidden(r.text, "csv_ref"), _hidden(r.text, "preview_hash")
    assert staged_hash
    staged = {p.name: p.read_bytes() for p in stage_dir.iterdir()}

    # Switch to B the way the company switcher does.
    with _bridged():
        switched = await _ui("POST", f"/switch-company/{cid_b}", token_a)
    assert switched.status_code == 302
    token_b = switched.cookies.get("celerp_token")
    assert token_b and decode_access_token(token_b)["company_id"] == cid_b

    # Reusing A's exact references under B fails closed at every step.
    with _bridged():
        assert await ci.load_import_csv(token_b, staged_ref) is None
        mapped = await _ui("POST", "/inventory/import/mapped", token_b,
                           data={"csv_ref": ref, "map__Item": "name", "map__Unit": "sell_by", "map__Qty": "quantity"})
        review = await _ui("POST", "/inventory/import/review", token_b, data={"csv_ref": staged_ref})
        confirm = await _ui("POST", "/inventory/import/confirm", token_b,
                            data={"csv_ref": staged_ref, "preview_hash": staged_hash})
    for resp in (mapped, review, confirm):
        assert resp.status_code == 200
        assert not _offers_confirm(resp.text)
        assert "Widget" not in resp.text
    assert await _items(session, cid_b) == [] and await _batches(session, cid_b) == []
    assert await _items(session, cid_a) == []
    # A's stage is neither read into B nor removed by B's attempt.
    assert {p.name: p.read_bytes() for p in stage_dir.iterdir()} == staged


# ---------------------------------------------------------------------------
# J6 - backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journey_j6_backward_compatibility(client, session, stage_dir):
    owner = await _register(client, session, name="Established Co")
    cid, token = owner["company_id"], owner["token"]
    await _set_settings(session, cid, currency="USD")
    assert "onboarding_pending" not in await _settings(session, cid)

    # Signing in and opening the app lands on the dashboard.
    with _bridged():
        login = await _ui("POST", "/login", None, data={"email": owner["email"], "password": _PASSWORD})
        assert login.status_code == 302
        fresh = login.cookies.get("celerp_token") or token
        assert (await _ui("GET", "/", fresh)).headers["location"] == "/dashboard"

    # A direct inventory import keeps its own result flow.
    with _bridged():
        page = await _ui("GET", "/inventory/import", token)
    assert page.status_code == 200
    _, ref = await _upload(token, _csv(["Item", "Unit", "Qty"], [["Widget", "piece", "4"]]))
    r = await _map(token, ref, {"Item": "name", "Unit": "sell_by", "Qty": "quantity"})
    done = await _confirm(token, _hidden(r.text, "csv_ref"), _hidden(r.text, "preview_hash"))
    assert 'href="/inventory"' in done.text and 'href="/inventory/import"' in done.text
    assert 'href="/onboarding"' not in done.text
    assert [(i["name"], float(i["quantity"])) for i in await _items(session, cid)] == [("Widget", 4.0)]
    assert len(await _batches(session, cid)) == 1
    assert "onboarding_pending" not in await _settings(session, cid)
    with _bridged():
        assert (await _ui("GET", "/", token)).headers["location"] == "/dashboard"


# ---------------------------------------------------------------------------
# J7 - invited user of an established company
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [True, False])
@pytest.mark.parametrize("role", ["manager", "operator"])
async def test_journey_j7_invited_user(role, pending, client, session):
    from celerp.services.auth import decode_access_token
    from celerp.services.session_tracker import clear as clear_tracker

    owner = await _register(client, session, name="Configured Co")
    cid = owner["company_id"]
    r = await _ui_setup(owner["token"], "consulting")
    await _lands_on_onboarding(r, owner["token"])
    if not pending:
        with _bridged():
            await _ui("POST", "/onboarding/complete", owner["token"])
    assert (await _settings(session, cid)).get("onboarding_pending") is pending
    settings_before = await _settings(session, cid)

    email = f"invited-{uuid.uuid4().hex[:8]}@example.com"
    invite = await client.post("/companies/me/users", headers=owner["h"], json={
        "email": email, "name": "Team Member", "role": role, "password": "userpw123",
    })
    assert invite.status_code == 200, invite.text
    await clear_tracker(session)

    # First sign-in lands in the existing company's app, not company setup.
    with _bridged():
        login = await _ui("POST", "/login", None, data={"email": email, "password": "userpw123"})
    assert login.status_code == 302, login.text
    token = login.cookies.get("celerp_token")
    assert token and decode_access_token(token)["company_id"] == cid
    with _bridged():
        root = await _ui("GET", "/", token)
        assert root.status_code == 302 and root.headers["location"] == "/dashboard"
        dashboard = await _ui("GET", "/dashboard", token)
    assert dashboard.status_code == 200
    assert 'action="/setup/company"' not in dashboard.text

    mine = (await client.get("/auth/my-companies", headers={"Authorization": f"Bearer {token}"})).json()
    items = mine.get("items") if isinstance(mine, dict) else mine
    assert [str(c.get("id") or c.get("company_id")) for c in items] == [cid]
    # Their arrival changes nothing about the company's setup.
    assert await _settings(session, cid) == settings_before

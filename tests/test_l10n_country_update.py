# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company's country is a setting of its own, chosen at setup or in Settings > General, and
never read from a location's address. Its country pack is applied, updated and undone through
Update localization: a preview lists what changes, Confirm applies it in one recorded event,
Undo puts back what the update changed and keeps what was edited since. A pack update that moves
an item rate offers a review of the items; nothing changes an item until that review is applied.
"""
from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from celerp.tax_regimes import get_regime
from test_helpers import company_auth

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def other_auth(session):
    """A second company with its own admin."""
    return await company_auth(session, uuid.uuid4(), uuid.uuid4())


async def _company(client, auth) -> dict:
    r = await client.get("/companies/me", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _set_country(client, auth, country):
    return await client.patch("/companies/me", headers=auth["headers"], json={"settings": {"country": country}})


async def _taxes(client, auth) -> list[dict]:
    r = await client.get("/companies/me/taxes", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _preview(client, auth) -> dict:
    r = await client.post("/settings/taxes/localization/preview", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def _confirm(client, auth, binding: str, key: str | None = None):
    return await client.post("/settings/taxes/localization/confirm", headers=auth["headers"],
                             json={"binding": binding, "idempotency_key": key or uuid.uuid4().hex})


async def _apply_country(client, auth, country: str) -> dict:
    r = await _set_country(client, auth, country)
    assert r.status_code == 200, r.text
    preview = await _preview(client, auth)
    r = await _confirm(client, auth, preview["fingerprint"])
    assert r.status_code == 200, r.text
    return r.json()


# ── S1: setup ───────────────────────────────────────────────────────────────────

def _stub(stack: ExitStack, name: str, **kw) -> AsyncMock:
    mock = AsyncMock(**kw)
    stack.enter_context(patch(f"ui.api_client.{name}", new=mock))
    return mock


async def test_setup_country_applies_pack_and_records_snapshot(client, auth):
    from ui.routes.setup import apply_company_setup
    with ExitStack() as stack:
        patch_company = _stub(stack, "patch_company", return_value={})
        locations = _stub(stack, "get_locations", return_value={"items": []})
        patch_location = _stub(stack, "patch_location", return_value={})
        preview = _stub(stack, "localization_preview", return_value={"fingerprint": "f1", "diff": {}})
        confirm = _stub(stack, "localization_confirm", return_value={"operation_id": "op1"})
        _stub(stack, "set_business_type", return_value={"restart_required": False})
        assert await apply_company_setup("tok", "THB", "Asia/Bangkok", "blank", "th") == "/dashboard"
    sent = [call.args[1] for call in patch_company.await_args_list]
    assert any((body.get("settings") or {}).get("country") == "TH" for body in sent)
    locations.assert_not_awaited()
    patch_location.assert_not_awaited()
    preview.assert_awaited_once()
    assert confirm.await_args.args[1] == "f1"

    applied = await _apply_country(client, auth, "TH")
    settings = (await _company(client, auth))["settings"]
    assert settings["country"] == "TH"
    loc = settings["localization"]
    assert loc["country"] == "TH" and loc["version"] and loc["digest"] and loc["applied_at"]
    assert loc["operation_id"] == applied["operation_id"]
    pack_names = {row["name"] for row in get_regime("TH")["taxes"]}
    assert pack_names <= {row["name"] for row in await _taxes(client, auth)}
    assert "taxes_seeded_for" not in {k.lower() for k in settings}


async def test_other_country_uses_neutral_list_and_says_so(client, auth):
    from fasthtml.common import to_xml
    from ui.routes.setup import apply_company_setup
    from ui.routes.settings import _localization_card
    with ExitStack() as stack:
        patch_company = _stub(stack, "patch_company", return_value={})
        preview = _stub(stack, "localization_preview", return_value={})
        confirm = _stub(stack, "localization_confirm", return_value={})
        _stub(stack, "set_business_type", return_value={"restart_required": False})
        await apply_company_setup("tok", "EUR", "UTC", "blank", "")
    assert all("country" not in (call.args[1].get("settings") or {}) for call in patch_company.await_args_list)
    preview.assert_not_awaited()
    confirm.assert_not_awaited()

    neutral = [row["name"] for row in await _taxes(client, auth)]
    settings = (await _company(client, auth))["settings"]
    assert not settings.get("country")
    assert neutral == [row["name"] for row in await _taxes(client, auth)]
    html = to_xml(_localization_card(settings))
    assert "No country pack applied" in html and "Country: --" in html


# ── S2: company country cell ────────────────────────────────────────────────────

async def test_company_country_cell_validation(client, auth):
    before = await _company(client, auth)
    taxes = await _taxes(client, auth)
    r = await _set_country(client, auth, "TH")
    assert r.status_code == 200, r.text
    after = await _company(client, auth)
    assert after["settings"]["country"] == "TH"
    assert {k: v for k, v in after["settings"].items() if k != "country"} == \
        {k: v for k, v in before["settings"].items() if k != "country"}
    assert await _taxes(client, auth) == taxes  # saving the country applies nothing by itself

    r = await _set_country(client, auth, "XQ")
    assert r.status_code == 422
    assert "XQ" in r.text and "Other country" in r.text
    assert (await _company(client, auth))["settings"]["country"] == "TH"


async def test_country_explicit_no_address_side_effect(client, auth):
    taxes = await _taxes(client, auth)
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": "Head office", "type": "office", "is_default": True,
                                "address": {"text": "1 Silom Road", "country": "TH"}})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/companies/me/locations/{r.json()['id']}", headers=auth["headers"],
                           json={"address": {"text": "12 MG Road", "country": "IN"}})
    assert r.status_code == 200, r.text
    assert "taxes_from" not in r.json()
    assert await _taxes(client, auth) == taxes
    assert not (await _company(client, auth))["settings"].get("country")


# ── S3 to S5: Update localization ───────────────────────────────────────────────

async def _operations(session) -> int:
    from celerp.models.ledger import LedgerEntry
    return (await session.execute(select(func.count()).select_from(LedgerEntry)
                                  .where(LedgerEntry.event_type == "sys.localization.applied"))).scalar_one()


async def test_localization_update_preview_confirm_undo(client, auth, session):
    r = await _set_country(client, auth, "TH")
    assert r.status_code == 200, r.text
    before = await _taxes(client, auth)
    preview = await _preview(client, auth)
    added = preview["diff"]["added"]
    assert {row["name"] for row in added} and all(row["versions"] for row in added)
    assert preview["fingerprint"]

    # a mid-apply failure leaves no operation row and the list unchanged
    # the test client raises what the app raised instead of answering 500
    with patch("celerp.localization.apply.emit_event", side_effect=RuntimeError("boom")), \
            pytest.raises(RuntimeError, match="boom"):
        await _confirm(client, auth, preview["fingerprint"])
    assert await _operations(session) == 0
    assert await _taxes(client, auth) == before

    key = uuid.uuid4().hex
    r = await _confirm(client, auth, preview["fingerprint"], key)
    assert r.status_code == 200, r.text
    operation = r.json()["operation_id"]
    assert await _operations(session) == 1
    again = await _confirm(client, auth, preview["fingerprint"], key)  # same key replays
    assert again.status_code == 200 and again.json()["operation_id"] == operation
    assert await _operations(session) == 1
    assert {row["name"] for row in added} <= {row["name"] for row in await _taxes(client, auth)}

    r = await client.post("/settings/taxes/localization/undo", headers=auth["headers"],
                          json={"operation_id": operation})
    assert r.status_code == 200, r.text
    assert await _taxes(client, auth) == before
    assert await _operations(session) == 2  # the undo is its own event naming what it undoes


async def test_localization_update_stale_preview_refused(client, auth, other_auth):
    r = await _set_country(client, auth, "TH")
    assert r.status_code == 200, r.text
    preview = await _preview(client, auth)
    taxes = await _taxes(client, auth)
    r = await client.patch("/companies/me/taxes", headers=auth["headers"],
                           json={"taxes": [{**taxes[0], "description": "edited after the preview"}, *taxes[1:]]})
    assert r.status_code == 200, r.text
    r = await _confirm(client, auth, preview["fingerprint"])
    assert r.status_code == 409
    assert "Taxes changed since the preview; preview again" in r.text
    r = await _confirm(client, auth, "forged")
    assert r.status_code == 409

    fresh = await _preview(client, auth)
    r = await _confirm(client, auth, fresh["fingerprint"])
    assert r.status_code == 200, r.text
    r = await client.post("/settings/taxes/localization/undo", headers=other_auth["headers"],
                          json={"operation_id": r.json()["operation_id"]})
    assert r.status_code == 404


async def test_localization_undo_keeps_later_edits(client, auth):
    applied = await _apply_country(client, auth, "TH")
    taxes = await _taxes(client, auth)
    edited = {**taxes[0], "description": "my own words"}
    r = await client.patch("/companies/me/taxes", headers=auth["headers"], json={"taxes": [edited, *taxes[1:]]})
    assert r.status_code == 200, r.text
    r = await client.post("/settings/taxes/localization/undo", headers=auth["headers"],
                          json={"operation_id": applied["operation_id"]})
    assert r.status_code == 200, r.text
    kept = r.json()["kept"]
    assert any(item["name"] == edited["name"] for item in kept)
    assert any(row.get("description") == "my own words" for row in await _taxes(client, auth))


# ── S6: item rate review ────────────────────────────────────────────────────────

async def test_rate_review_offered_never_applied(client, auth):
    pack = get_regime("IN")
    table = pack.get("item_rate_table") or []
    assert table, "the IN pack carries an item_rate_table"
    applied = await _apply_country(client, auth, "IN")
    moved = table[0]
    items = []
    for n in range(3):
        r = await client.post("/items", headers=auth["headers"],
                              json={"sku": f"HS-{n}", "name": f"Item {n}", "hs_code": moved["hs_prefix"] + "00",
                                    "tax_codes": ["old-code"]})
        assert r.status_code == 200, r.text
        items.append(r.json()["id"])

    r = await client.get(f"/settings/taxes/rate-review/{applied['operation_id']}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    proposal = r.json()
    assert {row["item_id"] for row in proposal["items"]} == set(items)
    for item_id in items:  # offered, never applied
        assert (await client.get(f"/items/{item_id}", headers=auth["headers"])).json()["tax_codes"] == ["old-code"]

    r = await client.patch(f"/items/{items[2]}", headers=auth["headers"], json={"name": "Edited since"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/settings/taxes/rate-review/{applied['operation_id']}/apply", headers=auth["headers"],
                          json={"item_ids": ["not-proposed"], "binding": proposal["fingerprint"]})
    assert r.status_code == 422
    r = await client.post(f"/settings/taxes/rate-review/{applied['operation_id']}/apply", headers=auth["headers"],
                          json={"item_ids": [items[0], items[2]], "binding": proposal["fingerprint"]})
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == [items[2]]
    codes = {i: (await client.get(f"/items/{i}", headers=auth["headers"])).json()["tax_codes"] for i in items}
    assert codes[items[0]] == [moved["code_id"]]
    assert codes[items[1]] == ["old-code"] and codes[items[2]] == ["old-code"]

    r = await client.post(f"/settings/taxes/rate-review/{applied['operation_id']}/undo", headers=auth["headers"])
    assert r.status_code == 200, r.text
    assert (await client.get(f"/items/{items[0]}", headers=auth["headers"])).json()["tax_codes"] == ["old-code"]

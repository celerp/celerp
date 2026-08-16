# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for celerp.services.pricing - derived price lists (base price × factor).

Covers the pure helpers (rounding, derivation, injection, validation, resolution) and
the behavior they drive end to end: settings validation, derived values on item reads,
write guards, valuation totals, role stripping, schema editability, scan-line pricing,
and the settings/pricing UI fragments.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from celerp.services.pricing import (
    derived_price,
    derived_price_keys,
    inject_derived_prices,
    is_cost_list_name,
    is_derived,
    price_key,
    resolve_price,
    round_half_up_to_increment,
    validate_price_lists,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestRounding:
    def test_none_leaves_value_unrounded(self):
        assert round_half_up_to_increment(503.30, None) == 503.30

    def test_nearest_whole(self):
        assert round_half_up_to_increment(503.30, 1) == 503.0

    def test_nearest_five(self):
        assert round_half_up_to_increment(503.30, 5) == 505.0

    def test_half_rounds_up(self):
        assert round_half_up_to_increment(502.5, 5) == 505.0
        assert round_half_up_to_increment(0.5, 1) == 1.0

    def test_decimal_beats_float_representation(self):
        """round() would give 2.67 here; half-up via Decimal gives 2.68."""
        assert round_half_up_to_increment(2.675, 0.01) == 2.68

    def test_small_increments(self):
        assert round_half_up_to_increment(1.234, 0.05) == 1.25
        assert round_half_up_to_increment(1.22, 0.05) == 1.2


class TestDerivedPrice:
    def test_factor_only(self):
        assert derived_price(719, {"multiplier": 0.7}) == 503.3

    def test_factor_and_rounding(self):
        assert derived_price(719, {"multiplier": 0.7, "rounding": 5}) == 505.0
        assert derived_price(719, {"multiplier": 0.7, "rounding": 1}) == 503.0

    def test_already_clean_value(self):
        assert derived_price(250, {"multiplier": 0.7, "rounding": 5}) == 175.0

    def test_positive_price_never_rounds_to_zero(self):
        """An increment coarser than the raw value floors at one increment, not free."""
        assert derived_price(2.10, {"multiplier": 0.7, "rounding": 5}) == 5.0
        assert derived_price(0.01, {"multiplier": 0.5, "rounding": 1}) == 1.0
        # At or above the half-increment it rounds normally
        assert derived_price(3.60, {"multiplier": 0.7, "rounding": 5}) == 5.0


class TestResolvePrice:
    def test_by_direct_name(self):
        item = {"Retail": 100, "Wholesale": 65}
        assert resolve_price(item, "Retail") == 100.0
        assert resolve_price(item, "Wholesale") == 65.0

    def test_conventional_key(self):
        item = {"retail_price": 99, "cost_price": 40}
        assert resolve_price(item, "Retail") == 99.0
        assert resolve_price(item, "Cost") == 40.0

    def test_no_match_returns_zero(self):
        item = {"retail_price": 50}
        assert resolve_price(item, "VIP") == 0.0

    def test_prefers_direct_over_conventional(self):
        item = {"Retail": 200, "retail_price": 150}
        assert resolve_price(item, "Retail") == 200.0


_CONFIG = [
    {"name": "Retail", "description": ""},
    {"name": "Wholesale", "description": ""},
    {"name": "Trade", "description": "", "multiplier": 0.7, "rounding": 5},
]


class TestInjectDerivedPrices:
    def test_injects_computed_key(self):
        flat = {"retail_price": 719}
        inject_derived_prices(flat, _CONFIG, "Retail", "USD")
        assert flat["trade_price"] == 505.0

    def test_manual_lists_untouched(self):
        flat = {"retail_price": 719, "wholesale_price": 600}
        inject_derived_prices(flat, _CONFIG, "Retail", "USD")
        assert flat["wholesale_price"] == 600

    def test_missing_base_leaves_key_absent(self):
        flat = {"wholesale_price": 600}
        inject_derived_prices(flat, _CONFIG, "Retail", "USD")
        assert "trade_price" not in flat

    def test_zero_base_leaves_key_absent(self):
        flat = {"retail_price": 0}
        inject_derived_prices(flat, _CONFIG, "Retail", "USD")
        assert "trade_price" not in flat

    def test_formula_wins_over_stale_stored_value(self):
        """A value stored before the list became derived is overwritten (or removed)."""
        flat = {"retail_price": 100, "trade_price": 999, "Trade": 888}
        inject_derived_prices(flat, _CONFIG, "Retail", "USD")
        assert flat["trade_price"] == 70.0
        assert "Trade" not in flat

    def test_stale_stored_value_removed_when_base_missing(self):
        flat = {"trade_price": 999}
        inject_derived_prices(flat, _CONFIG, "Retail", "USD")
        assert "trade_price" not in flat

    def test_cost_as_base(self):
        flat = {"cost_price": 100}
        config = [{"name": "Cost"}, {"name": "Retail", "multiplier": 2.5}]
        inject_derived_prices(flat, config, "Cost", "USD")
        assert flat["retail_price"] == 250.0

    def test_derived_value_obeys_rate_ceiling(self):
        """A derived unit price never exceeds rate_dp(currency), so round(rate × qty) can
        still reconcile against entered totals; the base's dynamic decimals stay untouched."""
        flat = {"retail_price": 15.285}  # dynamic-decimal rate, e.g. back-calculated from a total
        config = [{"name": "Retail"}, {"name": "Trade", "multiplier": 0.333333}]
        inject_derived_prices(flat, config, "Retail", "USD")
        # Raw product is 5.094994905 (9 dp); USD rate ceiling is 6 dp
        assert flat["trade_price"] == 5.094995
        assert flat["retail_price"] == 15.285


class TestValidatePriceLists:
    def test_valid_config(self):
        assert validate_price_lists(_CONFIG, "Retail") is None

    def test_factor_must_be_positive_number(self):
        for bad in (0, -1, "abc"):
            err = validate_price_lists([{"name": "Trade", "multiplier": bad}, {"name": "Retail"}], "Retail")
            assert "greater than 0" in err

    def test_rounding_requires_factor(self):
        err = validate_price_lists([{"name": "Trade", "rounding": 5}, {"name": "Retail"}], "Retail")
        assert "requires a factor" in err

    def test_rounding_must_be_positive_number(self):
        err = validate_price_lists([{"name": "Trade", "multiplier": 0.7, "rounding": 0}, {"name": "Retail"}], "Retail")
        assert "greater than 0" in err

    def test_base_cannot_be_derived(self):
        err = validate_price_lists([{"name": "Retail", "multiplier": 0.9}], "Retail")
        assert "base price list" in err

    def test_cost_cannot_be_derived(self):
        err = validate_price_lists([{"name": "Cost", "multiplier": 0.5}, {"name": "Retail"}], "Retail")
        assert "cannot be derived" in err

    def test_base_must_exist(self):
        err = validate_price_lists([{"name": "Retail"}], "Standard")
        assert "does not exist" in err

    def test_unconfigured_base_binds_only_once_something_derives(self):
        assert validate_price_lists([{"name": "Standard"}], "Retail", base_explicit=False) is None
        err = validate_price_lists(
            [{"name": "Standard"}, {"name": "Trade", "multiplier": 0.7}], "Retail", base_explicit=False
        )
        assert "does not exist" in err


class TestSmallHelpers:
    def test_price_key(self):
        assert price_key("Retail") == "retail_price"
        assert price_key("Trade Tier 2") == "trade tier 2_price"

    def test_is_cost_list_name(self):
        assert is_cost_list_name("Cost")
        assert is_cost_list_name("Landed Cost")
        assert not is_cost_list_name("Retail")

    def test_is_derived(self):
        assert is_derived({"name": "Trade", "multiplier": 0.7})
        assert not is_derived({"name": "Retail"})

    def test_derived_price_keys(self):
        assert derived_price_keys(_CONFIG) == {"trade_price"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

async def _register(client) -> str:
    email = f"pricing-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "PricingCo", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _set_config(client, headers, price_lists=None, base=None):
    body = {"price_lists": price_lists if price_lists is not None else _CONFIG}
    if base is not None:
        body["base_price_list"] = base
    return await client.patch("/companies/me/price-lists", json=body, headers=headers)


async def _create_item(client, headers, **fields) -> str:
    payload = {"status": "available", "sku": f"PRC-{uuid.uuid4().hex[:6]}", "name": "Widget", "quantity": 1, "sell_by": "piece"}
    payload.update(fields)
    r = await client.post("/items", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_base_price_list_defaults_to_retail(client):
    h = _auth(await _register(client))
    r = await client.get("/companies/me/base-price-list", headers=h)
    assert r.status_code == 200
    assert r.json() == "Retail"


@pytest.mark.asyncio
async def test_patch_base_price_list_roundtrips(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    r = await client.patch("/companies/me/base-price-list", json={"name": "Wholesale"}, headers=h)
    assert r.status_code == 200
    assert (await client.get("/companies/me/base-price-list", headers=h)).json() == "Wholesale"


@pytest.mark.asyncio
async def test_patch_base_price_list_rejects_missing_and_derived(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    r = await client.patch("/companies/me/base-price-list", json={"name": "Nope"}, headers=h)
    assert r.status_code == 422
    r = await client.patch("/companies/me/base-price-list", json={"name": "Trade"}, headers=h)
    assert r.status_code == 422
    assert "cannot be the base" in r.json()["detail"]


@pytest.mark.asyncio
async def test_patch_price_lists_validates(client):
    h = _auth(await _register(client))
    r = await _set_config(client, h, price_lists=[{"name": "Retail"}, {"name": "Trade", "multiplier": 0}])
    assert r.status_code == 422
    r = await _set_config(client, h, price_lists=[{"name": "Retail"}, {"name": "Trade", "rounding": 5}])
    assert r.status_code == 422
    r = await _set_config(client, h, price_lists=[{"name": "Retail"}, {"name": "Cost", "multiplier": 0.5}])
    assert r.status_code == 422
    # A derived list requires the base to exist
    r = await _set_config(client, h, price_lists=[{"name": "Standard"}, {"name": "Trade", "multiplier": 0.7}])
    assert r.status_code == 422
    assert "does not exist" in r.json()["detail"]


@pytest.mark.asyncio
async def test_unconfigured_base_frees_renames_until_something_derives(client):
    """No explicit base, no factors: lists rename freely. Once the base is configured,
    renaming it away without the atomic base_price_list update is rejected."""
    h = _auth(await _register(client))
    r = await _set_config(client, h, price_lists=[{"name": "Standard"}, {"name": "Wholesale"}])
    assert r.status_code == 200
    r = await _set_config(client, h, price_lists=[{"name": "Retail"}, {"name": "Wholesale"}], base="Retail")
    assert r.status_code == 200
    r = await _set_config(client, h, price_lists=[{"name": "Standard"}, {"name": "Wholesale"}])
    assert r.status_code == 422
    assert "does not exist" in r.json()["detail"]


@pytest.mark.asyncio
async def test_rename_base_atomically(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    renamed = [{"name": "Standard"}, {"name": "Wholesale"}, {"name": "Trade", "multiplier": 0.7}]
    r = await _set_config(client, h, price_lists=renamed, base="Standard")
    assert r.status_code == 200, r.text
    assert (await client.get("/companies/me/base-price-list", headers=h)).json() == "Standard"


@pytest.mark.asyncio
async def test_get_price_lists_strips_factors_below_manager(client, session):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200

    from celerp.services.session_tracker import clear as _clear_tracker
    email = f"op-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/companies/me/users",
        json={"email": email, "name": "Operator", "role": "operator", "password": "pw123"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    await _clear_tracker(session)
    r = await client.post("/auth/login", json={"email": email, "password": "pw"[:2] + "123"})
    assert r.status_code == 200, r.text
    op_h = _auth(r.json()["access_token"])

    lists = (await client.get("/companies/me/price-lists", headers=op_h)).json()
    names = [pl["name"] for pl in lists]
    assert "Trade" in names and "Cost" not in names
    for pl in lists:
        assert set(pl.keys()) == {"name", "description"}

    # Manager+ keeps the factor fields
    admin_lists = (await client.get("/companies/me/price-lists", headers=h)).json()
    trade = next(pl for pl in admin_lists if pl["name"] == "Trade")
    assert trade["multiplier"] == 0.7 and trade["rounding"] == 5


@pytest.mark.asyncio
async def test_item_read_returns_derived_price(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    item_id = await _create_item(client, h, retail_price=719)

    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item["retail_price"] == 719
    assert item["trade_price"] == 505.0

    listed = (await client.get("/items", headers=h)).json()
    rows = listed.get("items", listed)
    row = next(i for i in rows if i["id"] == item_id)
    assert row["trade_price"] == 505.0


@pytest.mark.asyncio
async def test_item_without_base_price_has_no_derived_key(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    item_id = await _create_item(client, h, wholesale_price=50)
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert "trade_price" not in item


@pytest.mark.asyncio
async def test_derived_key_dropped_on_create(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    item_id = await _create_item(client, h, retail_price=100, trade_price=999)
    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item["trade_price"] == 70.0  # computed, not the smuggled 999


@pytest.mark.asyncio
async def test_patch_item_rejects_derived_key(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    item_id = await _create_item(client, h, retail_price=100)
    r = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {"trade_price": {"new": 50}}},
        headers=h,
    )
    assert r.status_code == 422
    assert "computed" in r.json()["detail"]


@pytest.mark.asyncio
async def test_set_item_price_rejects_derived_key(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    item_id = await _create_item(client, h, retail_price=100)
    r = await client.post(
        f"/items/{item_id}/price",
        json={"price_type": "trade_price", "new_price": 50},
        headers=h,
    )
    assert r.status_code == 422
    assert "computed" in r.json()["detail"]


@pytest.mark.asyncio
async def test_valuation_includes_derived_totals(client):
    """Derived list totals move with created items (delta-based: registration seeds demo items)."""
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    before = (await client.get("/items/valuation", headers=h)).json()["price_totals"]
    await _create_item(client, h, retail_price=100, quantity=2)
    await _create_item(client, h, retail_price=719, quantity=1)

    after = (await client.get("/items/valuation", headers=h)).json()["price_totals"]
    assert after["Retail"] - before.get("Retail", 0) == pytest.approx(100 * 2 + 719)
    assert after["Trade"] - before.get("Trade", 0) == pytest.approx(70 * 2 + 505)


@pytest.mark.asyncio
async def test_fractional_quantity_holds_end_to_end(client):
    """Derivation is a unit-price operation; a decimal quantity (35.56 kg) multiplies the
    derived unit exactly like a manual one, in item reads and in valuation."""
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    before = (await client.get("/items/valuation", headers=h)).json()["price_totals"]
    item_id = await _create_item(client, h, retail_price=15.285, quantity=35.56, sell_by="kg")

    item = (await client.get(f"/items/{item_id}", headers=h)).json()
    assert item["quantity"] == 35.56
    assert item["retail_price"] == 15.285          # dynamic-decimal base untouched
    assert item["trade_price"] == 10.0             # 15.285 x 0.7 = 10.6995, rounded to nearest 5

    after = (await client.get("/items/valuation", headers=h)).json()["price_totals"]
    assert after["Retail"] - before.get("Retail", 0) == pytest.approx(15.285 * 35.56)
    assert after["Trade"] - before.get("Trade", 0) == pytest.approx(10.0 * 35.56)


@pytest.mark.asyncio
async def test_effective_schema_marks_derived_column_readonly(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    schema = (await client.get("/companies/me/item-schema", headers=h)).json()
    by_key = {f["key"]: f for f in schema}
    assert by_key["trade_price"]["editable"] is False
    assert by_key["retail_price"]["editable"] is True


@pytest.mark.asyncio
async def test_flip_factor_reprices_reads(client):
    """Change the factor in settings; the next item read reflects it, nothing stored per item."""
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    item_id = await _create_item(client, h, retail_price=100)
    assert (await client.get(f"/items/{item_id}", headers=h)).json()["trade_price"] == 70.0

    flipped = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Trade", "multiplier": 0.8}]
    assert (await _set_config(client, h, price_lists=flipped)).status_code == 200
    assert (await client.get(f"/items/{item_id}", headers=h)).json()["trade_price"] == 80.0


# ---------------------------------------------------------------------------
# Scan-line pricing (celerp-docs uses the same flatten + resolve path)
# ---------------------------------------------------------------------------

def test_scan_line_prices_derived_list():
    from celerp_docs.routes import _scan_line_from_item

    proj = SimpleNamespace(
        entity_id="item:scan",
        state={"sku": "S-1", "name": "Widget", "barcode": "123", "retail_price": 719, "quantity": 3},
    )
    line = _scan_line_from_item(proj, "quotation", "Trade", price_config=(_CONFIG, "Retail", "USD"))
    assert line["unit_price"] == 505.0
    line = _scan_line_from_item(proj, "quotation", "Retail", price_config=(_CONFIG, "Retail", "USD"))
    assert line["unit_price"] == 719.0


# ---------------------------------------------------------------------------
# UI fragments
# ---------------------------------------------------------------------------

def test_price_lists_tab_renders_factor_rounding_and_base_select():
    from ui.routes.settings import _price_lists_tab

    html = str(_price_lists_tab(_CONFIG, "Retail", "Retail"))
    assert "Factor" in html and "Rounding" in html
    assert "base-price-list-select" in html
    assert "default-price-list-select" in html
    assert "0.7" in html and "5" in html
    assert "--" in html  # manual lists show the empty placeholder in Factor/Rounding


def test_pricing_form_renders_derived_row_readonly_with_note():
    from ui.routes.inventory import _pricing_form

    item = {"retail_price": 719, "trade_price": 505.0, "quantity": 1, "sell_by": "piece"}
    pricing_fields = [
        {"key": "retail_price", "editable": True},
        {"key": "wholesale_price", "editable": True},
        {"key": "trade_price", "editable": False},
    ]
    html = str(_pricing_form("item:1", item, _CONFIG, "USD", pricing_fields, "Retail"))
    assert "Trade is Retail × 0.7, rounded to the nearest 5" in html
    assert 'name="retail_price"' in html      # manual list stays an editable input
    assert 'name="trade_price"' not in html   # derived list renders read-only
    # The derived value spans carry ids so a base-price save can refresh them in place
    assert 'id="derived_unit_trade_price"' in html
    assert 'id="derived_total_trade_price"' in html


def test_readonly_price_cells_oob_refresh_fragments():
    """After a base-price save, the same spans return as out-of-band swaps: the open
    pricing form updates derived rows immediately, with no reload and no focus loss."""
    from fasthtml.common import to_xml
    from ui.routes.inventory import _readonly_price_cells

    unit, total = _readonly_price_cells("trade_price", 505.0, 2.0, True, 6, oob=True)
    html = to_xml(unit) + to_xml(total)
    assert 'id="derived_unit_trade_price"' in html and "505" in html
    assert 'id="derived_total_trade_price"' in html and "1010.00" in html
    assert html.count('hx-swap-oob="true"') == 2
    # An unpriced derived list refreshes to the empty placeholder, not a stale number
    unit_empty, _ = _readonly_price_cells("trade_price", 0.0, 2.0, True, 6, oob=True)
    assert "--" in to_xml(unit_empty)


# ---------------------------------------------------------------------------
# Hardening: the validation gate must be at least as strict as the read path,
# and the read path must never fail a whole item read on a bad config or value.
# ---------------------------------------------------------------------------

class TestValidationGateHardening:
    def test_rejects_non_finite_factors(self):
        for bad in (float("inf"), "inf", "Infinity", 1e309, float("nan"), "nan"):
            err = validate_price_lists([{"name": "Retail"}, {"name": "Trade", "multiplier": bad}], "Retail")
            assert err and "Factor" in err, bad

    def test_rejects_boolean_factor_and_rounding(self):
        err = validate_price_lists([{"name": "Retail"}, {"name": "Trade", "multiplier": True}], "Retail")
        assert err and "Factor" in err
        err = validate_price_lists(
            [{"name": "Retail"}, {"name": "Trade", "multiplier": 0.7, "rounding": True}], "Retail"
        )
        assert err and "Rounding" in err

    def test_rejects_oversized_factor_and_rounding(self):
        err = validate_price_lists([{"name": "Retail"}, {"name": "Trade", "multiplier": 2_000_000}], "Retail")
        assert err and "at most" in err
        err = validate_price_lists(
            [{"name": "Retail"}, {"name": "Trade", "multiplier": 0.7, "rounding": 2_000_000}], "Retail"
        )
        assert err and "at most" in err

    def test_rejects_empty_and_duplicate_names(self):
        assert "empty" in validate_price_lists([{"name": "  "}, {"name": "Retail"}], "Retail")
        err = validate_price_lists([{"name": "Retail"}, {"name": "retail"}], "Retail")
        assert err and "unique" in err

    def test_new_cost_name_rejected_existing_grandfathered(self):
        from celerp.services.pricing import new_cost_name_error

        stored = [{"name": "Cost"}, {"name": "Retail"}]
        assert new_cost_name_error([{"name": "Cost"}, {"name": "Retail"}], stored) is None
        err = new_cost_name_error([{"name": "Landed Cost"}, {"name": "Retail"}], stored)
        assert err and "reserved" in err

    def test_normalized_price_lists_coerces_to_float(self):
        from celerp.services.pricing import normalized_price_lists

        out = normalized_price_lists([{"name": "Trade", "multiplier": "0.7", "rounding": "5"}])
        assert out[0]["multiplier"] == 0.7 and isinstance(out[0]["multiplier"], float)
        assert out[0]["rounding"] == 5.0 and isinstance(out[0]["rounding"], float)


class TestCoercePrice:
    def test_garbage_reads_as_missing(self):
        from celerp.services.pricing import coerce_price

        for garbage in (None, True, False, "abc", "", [1], {}, float("nan"), float("inf"), "inf"):
            assert coerce_price(garbage) is None, garbage
        assert coerce_price("12.5") == 12.5
        assert coerce_price(7) == 7.0

    def test_resolve_price_survives_garbage_state(self):
        assert resolve_price({"vip_price": "abc"}, "VIP") == 0.0
        assert resolve_price({"vip_price": [1]}, "VIP") == 0.0
        assert resolve_price({"VIP": True, "vip_price": 9}, "VIP") == 9.0  # bool is not a price


class TestInjectDefensive:
    """Invalid configs that bypass validation (e.g. a raw settings write) must degrade
    per list to 'unpriced', never crash a read or overwrite cost/base keys."""

    def test_infinite_factor_degrades_to_unpriced(self):
        flat = {"retail_price": 10}
        inject_derived_prices(flat, [{"name": "Retail"}, {"name": "VIP", "multiplier": float("inf")}], "Retail", "USD")
        assert "vip_price" not in flat

    def test_junk_factor_degrades_to_unpriced(self):
        flat = {"retail_price": 10}
        inject_derived_prices(flat, [{"name": "Retail"}, {"name": "VIP", "multiplier": "abc"}], "Retail", "USD")
        assert "vip_price" not in flat

    def test_overflow_factor_degrades_to_unpriced(self):
        flat = {"retail_price": 10}
        inject_derived_prices(flat, [{"name": "Retail"}, {"name": "VIP", "multiplier": 1e300}], "Retail", "USD")
        assert "vip_price" not in flat

    def test_sub_ceiling_value_reads_unpriced_not_free(self):
        flat = {"retail_price": 10}
        inject_derived_prices(flat, [{"name": "Retail"}, {"name": "VIP", "multiplier": 1e-8}], "Retail", "USD")
        assert "vip_price" not in flat  # would collapse to 0.0 at the USD rate ceiling

    def test_cost_list_never_overwritten(self):
        flat = {"cost_price": 10.0, "retail_price": 25.0}
        inject_derived_prices(flat, [{"name": "Retail"}, {"name": "Cost", "multiplier": 2.5}], "Retail", "USD")
        assert flat["cost_price"] == 10.0

    def test_base_list_never_overwritten(self):
        flat = {"retail_price": 50.0}
        inject_derived_prices(flat, [{"name": "Retail", "multiplier": 2.0}], "Retail", "USD")
        assert flat["retail_price"] == 50.0

    def test_garbage_base_value_degrades_to_unpriced(self):
        flat = {"retail_price": "abc"}
        inject_derived_prices(flat, _CONFIG, "Retail", "USD")
        assert "trade_price" not in flat


# ---------------------------------------------------------------------------
# Hardening: every settings door validates; every item write door is guarded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_company_settings_merge_validates_price_config(client):
    h = _auth(await _register(client))
    r = await client.patch(
        "/companies/me",
        json={"settings": {"price_lists": [{"name": "Retail"}, {"name": "Cost", "multiplier": 0.5}]}},
        headers=h,
    )
    assert r.status_code == 422
    # Valid config through the same door is normalized to floats
    r = await client.patch(
        "/companies/me",
        json={"settings": {"price_lists": [
            {"name": "Retail"}, {"name": "Wholesale"}, {"name": "Trade", "multiplier": "0.7"},
        ]}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    lists = (await client.get("/companies/me/price-lists", headers=h)).json()
    trade = next(pl for pl in lists if pl["name"] == "Trade")
    assert trade["multiplier"] == 0.7 and isinstance(trade["multiplier"], float)


@pytest.mark.asyncio
async def test_patch_price_lists_rejects_exotic_factors_and_new_cost_names(client):
    h = _auth(await _register(client))
    r = await _set_config(client, h, price_lists=[{"name": "Retail"}, {"name": "Trade", "multiplier": "inf"}])
    assert r.status_code == 422
    r = await _set_config(client, h, price_lists=[{"name": "Retail"}, {"name": "Trade", "multiplier": True}])
    assert r.status_code == 422
    r = await _set_config(client, h, price_lists=[{"name": "Retail"}, {"name": "Landed Cost"}])
    assert r.status_code == 422
    assert "reserved" in r.json()["detail"]
    r = await _set_config(client, h, price_lists=[{"name": "Retail"}, {"name": "retail"}])
    assert r.status_code == 422
    assert "unique" in r.json()["detail"]


@pytest.mark.asyncio
async def test_patch_item_rejects_non_numeric_price(client):
    h = _auth(await _register(client))
    item_id = await _create_item(client, h, retail_price=100)
    r = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {"retail_price": {"new": "abc"}}},
        headers=h,
    )
    assert r.status_code == 422
    assert "must be a number" in r.json()["detail"]


@pytest.mark.asyncio
async def test_write_guards_block_direct_list_name(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    item_id = await _create_item(client, h, retail_price=100)
    r = await client.post(
        f"/items/{item_id}/price",
        json={"price_type": "Trade", "new_price": 50},
        headers=h,
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/items/{item_id}",
        json={"fields_changed": {"Trade": {"new": 50}}},
        headers=h,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_batch_import_drops_derived_keys(client):
    h = _auth(await _register(client))
    assert (await _set_config(client, h)).status_code == 200
    record = {
        "entity_id": f"item:imp-{uuid.uuid4().hex[:8]}",
        "event_type": "item.created",
        "data": {"sku": f"IMP-{uuid.uuid4().hex[:6]}", "name": "Imported", "sell_by": "piece",
                 "quantity": 1, "retail_price": 100, "trade_price": 999},
        "source": "csv_import",
        "idempotency_key": f"test:imp:{uuid.uuid4()}",
    }
    r = await client.post("/items/import/batch", headers=h, json={"records": [record]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    item = (await client.get(f"/items/{record['entity_id']}", headers=h)).json()
    assert item["trade_price"] == 70.0  # computed from the base, not the smuggled 999


def test_settings_cell_error_reverts_value_and_toasts():
    """A rejected cell save swaps the saved value back (cell stays click-to-edit) and
    carries the error as an auto-dismissing toast, so no stale error text persists."""
    from ui.routes.settings import _pl_cell_error

    resp = _pl_cell_error(2, "multiplier", {"name": "Trade", "multiplier": 0.7}, "price-lists",
                          "Factor for 'Trade' must be a number greater than 0")
    body = resp.body.decode()
    assert "cell--clickable" in body                     # cell remains editable
    assert "0.7" in body                                 # reverted to the saved value
    assert "cell-error" not in body                      # no persistent inline error text
    trigger = resp.headers["HX-Trigger"]
    assert "celerpToast" in trigger and "greater than 0" in trigger


def test_settings_status_error_resets_span_and_toasts():
    """An auto-saving control's error resets its inline status span and toasts the
    message; nothing error-colored persists next to the control."""
    from ui.routes.settings import _status_error

    resp = _status_error("base-price-list-status", "'Trade' is derived and cannot be the base")
    body = resp.body.decode()
    assert 'id="base-price-list-status"' in body
    assert "flash--error" not in body
    trigger = resp.headers["HX-Trigger"]
    assert "celerpToast" in trigger and "cannot be the base" in trigger

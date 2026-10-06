# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Item import fields and category defaults match the canonical item model.

Every field the importer advertises is stored where the item model keeps it,
validated the way the item model validates it, and category defaults mean the
same thing on import as on ordinary item creation. Each import property is
checked through both semantic transports: mapped rows (preview then bound
commit) and an uploaded file (preview then commit).
"""

from __future__ import annotations

import csv
import io
import json
import math
import uuid

import pytest

_TRANSPORTS = ["rows", "file"]

# Library categories whose declared defaults the tests rely on.
_BULLION = "gold_bullion"        # sells by piece, buys by gram, weighs in gram
_SERVICE = "consulting_service"  # a service line sold by piece
_COMPONENT = "component_part"    # a stocked part, no inventory type default

_NON_FINITE = ["heavy", "nan", "inf", "-inf", "1e999"]


def _non_finite_code(value: str) -> str:
    """Text that is not a number is invalid_value; a number that is not finite is not_finite."""
    return "invalid_value" if value == "heavy" else "not_finite"


# ---------------------------------------------------------------------------
# Company, transports and state readers
# ---------------------------------------------------------------------------


async def _set_company_settings(session, company_id: str, **values) -> None:
    from celerp.services.company_lock import locked_company
    company = await locked_company(session, uuid.UUID(company_id))
    company.settings = {**(company.settings or {}), **values}
    await session.commit()


@pytest.fixture
async def ctx(client, session):
    """A fresh company with one default location and the library categories applied."""
    from celerp.services.auth import decode_access_token
    from celerp.services.vertical_presets import load_category

    r = await client.post("/auth/register", json={
        "company_name": "FieldCo", "email": f"fields-{uuid.uuid4().hex[:8]}@example.com",
        "name": "Owner", "password": "pwvalid12",
    })
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    loc = await client.post(
        "/companies/me/locations",
        json={"name": "Main", "type": "warehouse", "address": None, "is_default": True},
        headers=h,
    )
    assert loc.status_code == 200, loc.text
    claims = decode_access_token(token)
    company_id = claims["company_id"]

    cats = {slug: load_category(slug) for slug in (_BULLION, _SERVICE, _COMPONENT)}
    assert all(cats.values()), cats
    current = (await client.get("/companies/me", headers=h)).json()["settings"]
    await _set_company_settings(
        session, company_id,
        category_schemas={**(current.get("category_schemas") or {}),
                          **{s: c.get("fields") or [] for s, c in cats.items()}},
        category_display_names={**(current.get("category_display_names") or {}),
                                **{s: c.get("display_name", s) for s, c in cats.items()}},
    )
    return {"h": h, "company_id": company_id, "user_id": claims["sub"], "location_id": loc.json()["id"]}


def _upload(ctx: dict, rows: list[dict]) -> str:
    """Seed an owned transient CSV upload, as the upload endpoint would."""
    from celerp.ai.files import upload_dir

    cols: list[str] = []
    for row in rows:
        cols += [k for k in row if k not in cols]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in cols})
    data = buf.getvalue().encode()
    file_id = f"ai_up_{uuid.uuid4().hex}"
    (upload_dir() / f"{file_id}.bin").write_bytes(data)
    (upload_dir() / f"{file_id}.meta").write_text(json.dumps({
        "filename": "items.csv", "content_type": "text/csv", "size": len(data),
        "company_id": ctx["company_id"], "user_id": ctx["user_id"],
    }))
    return file_id


async def _import(client, ctx: dict, transport: str, rows: list[dict]) -> dict:
    """Preview then commit through one transport, binding the commit to the preview."""
    h = ctx["h"]
    key = f"op-{uuid.uuid4().hex[:8]}"
    if transport == "rows":
        p = await client.post("/items/import/rows/preview", json={
            "rows": rows, "upsert": False, "idempotency_key": key,
        }, headers=h)
        assert p.status_code == 200, p.text
        c = await client.post("/items/import/rows", json={
            "rows": rows, "upsert": False, "idempotency_key": key, "preview_hash": p.json()["preview_hash"],
        }, headers=h)
    else:
        file_id = _upload(ctx, rows)
        p = await client.post("/items/import/preview", json={"file_id": file_id}, headers=h)
        assert p.status_code == 200, p.text
        c = await client.post("/items/import/commit", json={
            "file_id": file_id, "preview_hash": p.json()["preview_hash"],
        }, headers=h)
    return {"preview_errors": p.json()["errors"], "commit": c}


def _commit_errors(result: dict) -> list[dict]:
    body = result["commit"].json()
    detail = body.get("detail") if isinstance(body, dict) else None
    return (detail or {}).get("errors") or [] if isinstance(detail, dict) else []


def _field_codes(errors: list[dict]) -> set[tuple]:
    return {(e.get("field"), e.get("code")) for e in errors}


async def _states_by_sku(session, company_id: str) -> dict[str, dict]:
    from sqlalchemy import select

    from celerp.models.projections import Projection
    session.expire_all()
    rows = (await session.execute(
        select(Projection).where(Projection.company_id == uuid.UUID(company_id), Projection.entity_type == "item")
    )).scalars().all()
    return {str((p.state or {}).get("sku") or ""): p.state for p in rows}


async def _state_by_id(session, company_id: str, entity_id: str) -> dict:
    from celerp.models.projections import Projection
    session.expire_all()
    row = await session.get(Projection, {"company_id": uuid.UUID(company_id), "entity_id": entity_id})
    assert row is not None, entity_id
    return row.state


async def _import_clean(client, session, ctx: dict, transport: str, rows: list[dict]) -> dict[str, dict]:
    result = await _import(client, ctx, transport, rows)
    assert result["preview_errors"] == [], result["preview_errors"]
    assert result["commit"].status_code == 200, result["commit"].text
    assert result["commit"].json()["errors"] == [], result["commit"].text
    return await _states_by_sku(session, ctx["company_id"])


async def _assert_rejected(client, session, ctx: dict, transport: str, rows: list[dict], field: str, code: str) -> list[dict]:
    """The preview reports the field error, the bound commit refuses it, and nothing is written."""
    result = await _import(client, ctx, transport, rows)
    assert (field, code) in _field_codes(result["preview_errors"]), result["preview_errors"]
    assert result["commit"].status_code == 422, result["commit"].text
    assert (field, code) in _field_codes(_commit_errors(result)), result["commit"].text
    states = await _states_by_sku(session, ctx["company_id"])
    assert not {str(r.get("sku")) for r in rows} & set(states), "a rejected row was written"
    return result["preview_errors"]


def _attributes(state: dict) -> dict:
    return state.get("attributes") or {}


async def _create(client, ctx: dict, payload: dict):
    return await client.post("/items", json={"location_id": ctx["location_id"], **payload}, headers=ctx["h"])


# ---------------------------------------------------------------------------
# Purchase fields are stored at their canonical top-level location
# ---------------------------------------------------------------------------


class TestPurchaseFields:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_purchase_fields_import_to_canonical_top_level_state(self, client, session, ctx, transport):
        states = await _import_clean(client, session, ctx, transport, [{
            "sku": "PF-1", "name": "Hex bolt", "sell_by": "piece", "quantity": "10",
            "purchase_sku": "VEN-778", "purchase_name": "Bolt M6 box", "purchase_unit": "kg",
            "purchase_conversion_factor": "12.5",
        }])
        state = states["PF-1"]
        assert state.get("purchase_sku") == "VEN-778"
        assert state.get("purchase_name") == "Bolt M6 box"
        assert state.get("purchase_unit") == "kg"
        assert state.get("purchase_conversion_factor") == 12.5
        leaked = {"purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor"} & set(_attributes(state))
        assert not leaked, f"purchase fields stored as custom attributes: {sorted(leaked)}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_purchase_unit_is_not_shadowed_by_sell_by_fallback(self, client, session, ctx, transport):
        states = await _import_clean(client, session, ctx, transport, [{
            "sku": "PU-1", "name": "Silver coin", "sell_by": "piece", "quantity": "3", "purchase_unit": "gram",
        }])
        state = states["PU-1"]
        assert state.get("sell_by") == "piece"
        assert state.get("purchase_unit") == "gram"
        assert "purchase_unit" not in _attributes(state)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_purchase_conversion_factor_is_not_shadowed_by_one(self, client, session, ctx, transport):
        states = await _import_clean(client, session, ctx, transport, [{
            "sku": "PC-1", "name": "Soda can", "sell_by": "piece", "quantity": "48",
            "purchase_unit": "piece", "purchase_conversion_factor": "24",
        }])
        state = states["PC-1"]
        assert state.get("purchase_conversion_factor") == 24
        assert "purchase_conversion_factor" not in _attributes(state)


# ---------------------------------------------------------------------------
# Advertised numeric fields accept only finite numbers
# ---------------------------------------------------------------------------


def _both_specs():
    from celerp_inventory.services import build_item_import_spec
    from ui.routes.inventory import _IMPORT_SPEC
    return {"server": build_item_import_spec([]), "browser": _IMPORT_SPEC}


class TestFiniteNumericFields:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    @pytest.mark.parametrize("value", _NON_FINITE)
    async def test_gross_weight_requires_finite_numeric_value(self, client, session, ctx, transport, value):
        from celerp.importers.tabular import validate_cell
        for name, spec in _both_specs().items():
            assert not validate_cell(spec, "gross_weight", value, {}), (name, value)
        await _assert_rejected(client, session, ctx, transport, [{
            "sku": "GW-1", "name": "Cast part", "sell_by": "piece", "quantity": "1",
            "gross_weight": value, "gross_weight_unit": "gram",
        }], "gross_weight", _non_finite_code(value))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    @pytest.mark.parametrize("value", _NON_FINITE)
    async def test_purchase_conversion_factor_requires_finite_numeric_value(self, client, session, ctx, transport, value):
        from celerp.importers.tabular import validate_cell
        for name, spec in _both_specs().items():
            assert not validate_cell(spec, "purchase_conversion_factor", value, {}), (name, value)
        await _assert_rejected(client, session, ctx, transport, [{
            "sku": "CF-1", "name": "Cased item", "sell_by": "piece", "quantity": "1",
            "purchase_unit": "piece", "purchase_conversion_factor": value,
        }], "purchase_conversion_factor", _non_finite_code(value))


class TestUnknownPriceFields:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["bogus_price", "bogus_price_total"])
    @pytest.mark.parametrize("value", ["5", "abc", "nan"])
    async def test_unknown_price_list_field_is_rejected_not_written(self, client, session, ctx, key, value):
        # A price key that names no company price list is neither written as a
        # stray top-level price nor silently dropped.
        await _assert_rejected(client, session, ctx, "rows", [{
            "sku": "UP-1", "name": "Stray price", "sell_by": "piece", "quantity": "1", key: value,
        }], key, "unknown_target")


# ---------------------------------------------------------------------------
# inventory_type is part of the import contract
# ---------------------------------------------------------------------------


class TestInventoryType:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_consulting_service_import_results_in_service_inventory_type(self, client, session, ctx, transport):
        states = await _import_clean(client, session, ctx, transport, [
            {"sku": "SV-1", "name": "Strategy workshop", "sell_by": "piece", "quantity": "0", "inventory_type": "service"},
            {"sku": "SV-2", "name": "Advisory day", "category": _SERVICE, "quantity": "0", "inventory_type": "service"},
        ])
        for sku in ("SV-1", "SV-2"):
            assert states[sku].get("inventory_type") == "service", sku
            assert "inventory_type" not in _attributes(states[sku]), sku

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_component_import_results_in_component_inventory_type(self, client, session, ctx, transport):
        states = await _import_clean(client, session, ctx, transport, [
            {"sku": "CP-1", "name": "Hinge", "sell_by": "piece", "quantity": "40", "inventory_type": "component"},
            {"sku": "CP-2", "name": "Bracket", "category": _COMPONENT, "quantity": "15", "inventory_type": "component"},
        ])
        for sku in ("CP-1", "CP-2"):
            assert states[sku].get("inventory_type") == "component", sku
            assert "inventory_type" not in _attributes(states[sku]), sku

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    @pytest.mark.parametrize("value", ["gizmo", "services", "stock"])
    async def test_invalid_inventory_type_is_rejected(self, client, session, ctx, transport, value):
        await _assert_rejected(client, session, ctx, transport, [{
            "sku": "IT-1", "name": "Mystery line", "sell_by": "piece", "quantity": "1", "inventory_type": value,
        }], "inventory_type", "invalid_value")


# ---------------------------------------------------------------------------
# Category defaults apply on import; explicit source values always win
# ---------------------------------------------------------------------------


class TestCategoryDefaults:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_bullion_category_applies_default_purchase_unit(self, client, session, ctx, transport):
        states = await _import_clean(client, session, ctx, transport, [
            {"sku": "GB-1", "name": "Gold bar 1 oz", "category": _BULLION, "quantity": "2"},
            {"sku": "GB-2", "name": "Gold bar 10 g", "category": _BULLION, "quantity": "2", "purchase_unit": "oz"},
        ])
        assert states["GB-1"].get("sell_by") == "piece"
        assert states["GB-1"].get("purchase_unit") == "gram"
        assert states["GB-2"].get("purchase_unit") == "oz", "an explicit source value must beat the category default"
        for sku in ("GB-1", "GB-2"):
            assert "purchase_unit" not in _attributes(states[sku]), sku

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_category_default_weight_unit_is_applied_when_source_omits_unit(self, client, session, ctx, transport):
        states = await _import_clean(client, session, ctx, transport, [
            {"sku": "GW-A", "name": "Gold bar", "category": _BULLION, "quantity": "1", "weight": "31.1"},
            {"sku": "GW-B", "name": "Gold coin", "category": _BULLION, "quantity": "1", "weight": "1", "weight_unit": "oz"},
        ])
        # The default names the unit the number was written in; it never converts it.
        assert states["GW-A"].get("weight") == 31.1
        assert states["GW-A"].get("weight_unit") == "gram"
        assert states["GW-A"].get("quantity") == 1
        assert states["GW-B"].get("weight") == 1
        assert states["GW-B"].get("weight_unit") == "oz", "an explicit source unit must beat the category default"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_category_default_inventory_type_is_applied(self, client, session, ctx, transport):
        states = await _import_clean(client, session, ctx, transport, [
            {"sku": "CS-1", "name": "Discovery session", "category": _SERVICE, "quantity": "0"},
            {"sku": "CS-2", "name": "Printed workbook", "category": _SERVICE, "quantity": "5", "inventory_type": "stocked"},
            {"sku": "CS-3", "name": "Bracket", "category": _COMPONENT, "quantity": "5"},
        ])
        assert states["CS-1"].get("inventory_type") == "service"
        assert states["CS-1"].get("sell_by") == "piece"
        assert states["CS-2"].get("inventory_type") == "stocked", "an explicit source value must beat the category default"
        assert states["CS-3"].get("inventory_type") == "stocked", "a category with no type default keeps the item default"


# ---------------------------------------------------------------------------
# Identifier fields use the canonical item validators
# ---------------------------------------------------------------------------


class TestIdentifierFields:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_gtin_import_uses_canonical_gtin_validation(self, client, session, ctx, transport):
        from ui.i18n import t

        states = await _import_clean(client, session, ctx, transport, [{
            "sku": "GT-1", "name": "Boxed lamp", "sell_by": "piece", "quantity": "1", "gtin": "00012345678905",
        }])
        assert states["GT-1"].get("gtin") == "00012345678905", "a valid GTIN keeps its leading zeros"
        assert "gtin" not in _attributes(states["GT-1"])

        for value, message in (("12AB5678", t("inventory.err_gtin_digits")), ("1234567", t("inventory.err_gtin_length"))):
            errors = await _assert_rejected(client, session, ctx, transport, [{
                "sku": "GT-BAD", "name": "Bad code", "sell_by": "piece", "quantity": "1", "gtin": value,
            }], "gtin", "invalid_value")
            assert any(e.get("field") == "gtin" and message in str(e.get("message")) for e in errors), (value, errors)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_rfid_import_uses_canonical_rfid_validation(self, client, session, ctx, transport):
        from celerp.inventory_codes import MAX_RFID_EPC_LEN
        from ui.i18n import t

        states = await _import_clean(client, session, ctx, transport, [{
            "sku": "RF-1", "name": "Tagged tool", "sell_by": "piece", "quantity": "1", "rfid_epc": " e2806894000040 ",
        }])
        assert states["RF-1"].get("rfid_epc") == "E2806894000040", "the EPC is stored in its canonical form"
        assert "rfid_epc" not in _attributes(states["RF-1"])

        for value, message in (("E2-80!", t("inventory.err_rfid_chars")),
                               ("A" * (MAX_RFID_EPC_LEN + 1), t("inventory.err_rfid_too_long", max=MAX_RFID_EPC_LEN))):
            errors = await _assert_rejected(client, session, ctx, transport, [{
                "sku": "RF-BAD", "name": "Bad tag", "sell_by": "piece", "quantity": "1", "rfid_epc": value,
            }], "rfid_epc", "invalid_value")
            assert any(e.get("field") == "rfid_epc" and message in str(e.get("message")) for e in errors), (value, errors)


# ---------------------------------------------------------------------------
# One storage rule for every advertised target; reserved names never become attributes
# ---------------------------------------------------------------------------

# A valid sample for each target the importer advertises or may advertise.
_SAMPLES = {
    "sku": "ADV-1", "name": "Advertised bar", "sell_by": "piece", "category": _BULLION, "quantity": "2",
    "weight": "62.2", "weight_unit": "gram", "gross_weight": "63", "gross_weight_unit": "gram", "pieces": "2",
    "barcode": "99001122", "hs_code": "7108", "purchase_sku": "VEN-ADV", "purchase_name": "Vendor bar",
    "purchase_unit": "gram", "purchase_conversion_factor": "31.1", "short_description": "Short text",
    "description": "Long text", "notes": "A note", "location_name": "Main",
    "inventory_type": "stocked", "gtin": "12345670", "rfid_epc": "ABC123", "reorder_point": "5",
    "reorder_qty": "10", "batch_no": "B-1", "allow_splitting": "true", "pick_method": "fifo",
}
# Import-only pseudo targets: resolved by the importer, never stored under their own name.
_IMPORT_ONLY = {"location_name"}


def _is_price_target(col: str) -> bool:
    return col.endswith(("_price", "_price_total"))


# Canonical item fields a spreadsheet may name even when the importer does not support them.
_RESERVED = {
    "reorder_point": "5", "reorder_qty": "10", "allow_splitting": "false", "pick_method": "fefo",
    "batch_no": "LOT-9", "consignment_flag": "true", "cost_base": "12", "parent_id": "item:x",
}


class TestStorageClass:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_every_advertised_import_target_has_one_canonical_storage_class(self, client, session, ctx, transport):
        from celerp.services.pricing import get_price_config
        from celerp_inventory.projections import is_core_item_key
        from celerp_inventory.services import build_item_import_spec
        from ui.routes.inventory import _IMPORT_SPEC

        price_lists, _default, _currency = await get_price_config(session, uuid.UUID(ctx["company_id"]))
        advertised = list(dict.fromkeys(build_item_import_spec(price_lists).cols + _IMPORT_SPEC.cols))
        # Prices are stored through pricing events and are core by suffix.
        assert all(is_core_item_key(c) for c in advertised if _is_price_target(c) and not c.endswith("_total"))
        targets = [c for c in advertised if not _is_price_target(c)]
        missing = [c for c in targets if c not in _SAMPLES]
        assert not missing, f"advertised targets without a sample value: {missing}"

        row = {c: _SAMPLES[c] for c in targets}
        state = (await _import_clean(client, session, ctx, transport, [row]))["ADV-1"]
        attrs = _attributes(state)
        wrong = []
        for col in targets:
            if col in _IMPORT_ONLY:
                if col in attrs:
                    wrong.append((col, "import-only target stored as an attribute"))
            elif is_core_item_key(col):
                if col in attrs or state.get(col) in (None, ""):
                    wrong.append((col, "core target not stored at the top level"))
            elif col in state or col not in attrs:
                wrong.append((col, "attribute target not stored under attributes"))
        assert not wrong, wrong

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    @pytest.mark.parametrize("field", sorted(_RESERVED))
    async def test_reserved_item_field_cannot_fall_through_as_custom_attribute(self, client, session, ctx, transport, field):
        from celerp_inventory.projections import is_core_item_key

        assert is_core_item_key(field), field
        row = {"sku": "RS-1", "name": "Reserved probe", "sell_by": "piece", "quantity": "1",
               "finish": "matte", field: _RESERVED[field]}
        result = await _import(client, ctx, transport, [row])
        states = await _states_by_sku(session, ctx["company_id"])
        mine = [e for e in result["preview_errors"] if e.get("field") == field]
        if mine:
            # Unsupported: rejected by name, never demoted to a custom attribute.
            assert _field_codes(mine) == {(field, "reserved_field_unsupported")}, mine
            assert result["commit"].status_code == 422, result["commit"].text
            assert "RS-1" not in states
        else:
            # Supported: stored where the item model keeps it.
            assert result["preview_errors"] == [], result["preview_errors"]
            assert result["commit"].status_code == 200, result["commit"].text
            assert field not in _attributes(states["RS-1"]), f"{field} stored as a custom attribute"
            assert states["RS-1"].get(field) not in (None, ""), field
        # A genuine custom column is still a custom attribute.
        assert not [e for e in result["preview_errors"] if e.get("field") == "finish"]
        if "RS-1" in states:
            assert _attributes(states["RS-1"]).get("finish") == "matte"


# ---------------------------------------------------------------------------
# Ordinary item creation and import share the category defaults
# ---------------------------------------------------------------------------


class TestCreateItemDefaults:
    @pytest.mark.asyncio
    async def test_create_item_semantics_unchanged_for_stocked_weighted_service(self, client, session, ctx):
        cid = ctx["company_id"]

        r = await _create(client, ctx, {"sku": "BR-S", "name": "Plain widget", "sell_by": "piece", "quantity": 3})
        assert r.status_code == 200, r.text
        s = await _state_by_id(session, cid, r.json()["id"])
        assert (s["inventory_type"], s["sell_by"], s["quantity"]) == ("stocked", "piece", 3)
        assert (s["purchase_unit"], s["purchase_conversion_factor"]) == ("piece", 1)
        assert s.get("weight_unit") is None

        r = await _create(client, ctx, {"sku": "BR-W", "name": "Loose stone", "sell_by": "gram", "quantity": 2.5,
                                        "weight": 2.5, "weight_unit": "gram"})
        assert r.status_code == 200, r.text
        s = await _state_by_id(session, cid, r.json()["id"])
        assert (s["inventory_type"], s["sell_by"], s["quantity"]) == ("stocked", "gram", 2.5)
        assert (s["weight"], s["weight_unit"], s["purchase_unit"]) == (2.5, "gram", "gram")

        r = await _create(client, ctx, {"sku": "BR-V", "name": "Site visit", "sell_by": "piece", "inventory_type": "service"})
        assert r.status_code == 200, r.text
        s = await _state_by_id(session, cid, r.json()["id"])
        assert (s["inventory_type"], s["sell_by"], s["quantity"]) == ("service", "piece", 0)

        # A category with no library defaults changes nothing.
        r = await _create(client, ctx, {"sku": "BR-C", "name": "House line", "sell_by": "piece", "category": "house_line"})
        assert r.status_code == 200, r.text
        s = await _state_by_id(session, cid, r.json()["id"])
        assert (s["inventory_type"], s["purchase_unit"], s.get("weight_unit")) == ("stocked", "piece", None)

        # An explicit sell_by and purchase_unit still beat the category.
        r = await _create(client, ctx, {"sku": "BR-B", "name": "Bar", "sell_by": "gram", "category": _BULLION,
                                        "purchase_unit": "oz", "quantity": 10})
        assert r.status_code == 200, r.text
        s = await _state_by_id(session, cid, r.json()["id"])
        assert (s["sell_by"], s["purchase_unit"], s["weight_unit"]) == ("gram", "oz", "gram")

        r = await _create(client, ctx, {"name": "No unit", "quantity": 1})
        assert r.status_code == 422, "an item with no unit and no category default is still refused"

    @pytest.mark.asyncio
    async def test_create_item_explicit_inventory_type_beats_category_default(self, client, session, ctx):
        cid = ctx["company_id"]
        expected = {None: "service", "stocked": "stocked", "component": "component"}
        for explicit, want in expected.items():
            payload = {"name": f"Consulting line {explicit}", "category": _SERVICE, "sell_by": "piece"}
            if explicit is not None:
                payload["inventory_type"] = explicit
            r = await _create(client, ctx, payload)
            assert r.status_code == 200, r.text
            state = await _state_by_id(session, cid, r.json()["id"])
            assert state.get("inventory_type") == want, (explicit, state.get("inventory_type"))

        r = await _create(client, ctx, {"name": "Bad type", "category": _SERVICE, "sell_by": "piece", "inventory_type": "gizmo"})
        assert r.status_code == 422, r.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", _TRANSPORTS)
    async def test_create_and_import_apply_the_same_category_defaults(self, client, session, ctx, transport):
        from celerp.services.vertical_presets import category_item_defaults

        assert category_item_defaults(_BULLION) == {"sell_by": "piece", "purchase_unit": "gram", "weight_unit": "gram"}
        assert category_item_defaults(_SERVICE) == {"sell_by": "piece", "purchase_unit": "piece", "inventory_type": "service"}
        assert category_item_defaults("house_line") == {}
        assert category_item_defaults(None) == {}
        assert category_item_defaults("") == {}

        cid = ctx["company_id"]
        cases = {
            _BULLION: {"quantity": 1, "weight": 31.1},
            _SERVICE: {"quantity": 0},
            _COMPONENT: {"quantity": 7, "weight": 12},
        }
        rows = [
            {"sku": f"IMP-{slug}", "name": f"Imported {slug}", "category": slug, **{k: str(v) for k, v in extra.items()}}
            for slug, extra in cases.items()
        ]
        imported = await _import_clean(client, session, ctx, transport, rows)

        fields = ("sell_by", "purchase_unit", "purchase_conversion_factor", "weight_unit", "inventory_type")
        for slug, extra in cases.items():
            r = await _create(client, ctx, {"sku": f"NEW-{slug}", "name": f"Created {slug}", "category": slug, **extra})
            assert r.status_code == 200, (slug, r.text)
            created = await _state_by_id(session, cid, r.json()["id"])
            via_import = imported[f"IMP-{slug}"]
            diff = {f: (created.get(f), via_import.get(f)) for f in fields if created.get(f) != via_import.get(f)}
            assert not diff, (slug, diff)
            for field, value in category_item_defaults(slug).items():
                assert created.get(field) == value == via_import.get(field), (slug, field)
            for f in ("weight",):
                if f in extra:
                    assert math.isclose(created[f], via_import[f]) and math.isclose(created[f], extra[f]), (slug, f)

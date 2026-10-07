# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Source header meaning survives every import transport, or the import stops.

A source header can say something its values do not: the currency a price is
in, the unit a price is quoted per, the unit a weight is measured in. Each test
checks the pure header reader first, then the browser mapper and the file
preview and commit (CSV and XLSX), so every transport reads a header the same
way. Row rules that need the whole row (a total price and its quantity, a unit
price next to its total) are checked where every transport's rows arrive: the
semantic row preview, the bound and unbound row commit, and the file
preview and commit.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from celerp.services import import_stage
from httpx import ASGITransport, AsyncClient

from test_onboarding_import_invariants import (  # noqa: F401  (fixtures)
    _COMPANY_A,
    _import_clean,
    _item_count,
    _item_states,
    _mapping_form,
    _owner_cookies,
    _rows_commit,
    _rows_preview,
    _seed_items,
    _set_company_settings,
    _xlsx,
    perm,
    stage_dir,
)

_PRICE_LISTS = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]
_CURRENCY_CODES = {"price_currency_mismatch", "price_currency_ambiguous"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _semantics(mapping: dict[str, str], currency: str):
    from celerp_inventory.services import source_header_semantics
    return source_header_semantics(mapping, currency)


def _header_codes(mapping: dict[str, str], currency: str, header: str) -> set[str]:
    """Error codes the pure header reader reports for one source column."""
    errors = _semantics(mapping, currency).errors
    for e in errors:
        assert e.get("message"), e
    return {e["code"] for e in errors if e["field"] == header}


def _mapped_row(mapping: dict[str, str], currency: str, source_row: dict) -> dict:
    """One source row as the shared helpers map it: rename columns, then carry header meaning."""
    from celerp_inventory.services import apply_source_semantics
    row = {mapping[col]: value for col, value in source_row.items() if mapping.get(col) not in (None, "__skip__")}
    return apply_source_semantics([row], _semantics(mapping, currency))[0]


def _cell(value: str):
    try:
        return float(value)
    except ValueError:
        return value


def _file_bytes(header: list[str], rows: list[list[str]], fmt: str) -> tuple[bytes, str]:
    if fmt == "xlsx":
        return _xlsx({"Items": [header, *[[_cell(v) for v in row] for row in rows]]}), "items.xlsx"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode(), "items.csv"


def _upload(perm: dict, header: list[str], rows: list[list[str]], fmt: str) -> str:
    """Seed an owned transient upload in CSV or XLSX form, as the upload endpoint would."""
    from celerp.ai.files import upload_dir
    data, filename = _file_bytes(header, rows, fmt)
    file_id = f"ai_up_{uuid.uuid4().hex}"
    (upload_dir() / f"{file_id}.bin").write_bytes(data)
    (upload_dir() / f"{file_id}.meta").write_text(json.dumps({
        "filename": filename, "content_type": "application/octet-stream", "size": len(data),
        "company_id": perm["company_id"], "user_id": perm["admin_user_id"],
    }))
    return file_id


async def _file_preview(client, perm, file_id: str, mapping: dict[str, str]) -> dict:
    r = await client.post("/items/import/preview", json={"file_id": file_id, "mapping": mapping}, headers=perm["admin_h"])
    assert r.status_code == 200, r.text
    return r.json()


async def _file_commit(client, perm, file_id: str, mapping: dict[str, str], preview_hash: str):
    return await client.post("/items/import/commit", json={
        "file_id": file_id, "mapping": mapping, "preview_hash": preview_hash,
    }, headers=perm["admin_h"])


async def _browser_mapped(csv_text: str, mapping: dict[str, str], currency: str):
    """Post a column mapping to the browser importer; the next step is replaced by a recorder."""
    from fasthtml.common import Div

    from ui.app import app as ui_app
    ref = import_stage.write_stage(_COMPANY_A, csv_text)
    company = {"id": _COMPANY_A, "currency": currency, "current_role": "owner", "settings": {}}
    check = AsyncMock(return_value=Div("mapped rows checked"))
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
         patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=_PRICE_LISTS)), \
         patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})), \
         patch("ui.routes.inventory._item_import_check", new=check):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            r = await c.post("/inventory/import/mapped", data={"csv_ref": ref, **_mapping_form(mapping)}, cookies=_owner_cookies())
    assert r.status_code == 200, r.text
    return r, check


def _csv_text(header: list[str], rows: list[list[str]]) -> str:
    return _file_bytes(header, rows, "csv")[0].decode()


def _price_file(header: str, target: str, *, sell_by: str = "piece", quantity: str = "1", value: str = "10"):
    cols = ["name", "sell_by", "quantity", header]
    mapping = {"name": "name", "sell_by": "sell_by", "quantity": "quantity", header: target}
    return cols, [["Widget", sell_by, quantity, value]], mapping


async def _assert_header_blocks_every_transport(client, session, perm, header: str, target: str,
                                                currency: str, allowed_codes: set[str]) -> None:
    """The column is refused by the pure reader, the file preview and commit (CSV and XLSX),
    and the browser mapper; nothing is written."""
    cols, rows, mapping = _price_file(header, target)

    assert _header_codes(mapping, currency, header) & allowed_codes, (header, target)

    await _set_company_settings(session, perm["company_id"], currency=currency)
    before = await _item_count(session, perm["company_id"])
    for fmt in ("csv", "xlsx"):
        file_id = _upload(perm, cols, rows, fmt)
        preview = await _file_preview(client, perm, file_id, mapping)
        codes = {e["code"] for e in preview["errors"] if e["field"] == header}
        assert codes & allowed_codes, (fmt, header, preview["errors"])
        r = await _file_commit(client, perm, file_id, mapping, preview["preview_hash"])
        assert r.status_code == 422, (fmt, r.text)
    assert await _item_count(session, perm["company_id"]) == before

    _page, check = await _browser_mapped(_csv_text(cols, rows), mapping, currency)
    check.assert_not_awaited()


# ---------------------------------------------------------------------------
# Price basis: "/basis" and "per basis" follow one grammar
# ---------------------------------------------------------------------------


class TestPriceBasisInvariant:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["Price/box", "Price / dozen", "Retail price/set", "Price/100g"])
    async def test_price_slash_unknown_basis_is_rejected(self, client, session, perm, stage_dir, header):
        mapping = {header: "retail_price"}
        assert "price_basis_unsupported" in _header_codes(mapping, "USD", header)
        assert "retail_price_basis" not in _mapped_row(mapping, "USD", {header: "10"})
        await _assert_header_blocks_every_transport(client, session, perm, header, "retail_price", "USD", {"price_basis_unsupported"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,target", [
        ("Cost/dozen", "cost_price"),
        ("Cost/box", "cost_price"),
        ("Cost price / set", "cost_price"),
        ("Cost/dozen", "cost_price_total"),
    ])
    async def test_cost_slash_unknown_basis_is_rejected(self, client, session, perm, stage_dir, header, target):
        assert "price_basis_unsupported" in _header_codes({header: target}, "USD", header)
        await _assert_header_blocks_every_transport(client, session, perm, header, target, "USD", {"price_basis_unsupported"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,target", [
        ("Price per dozen", "retail_price"),
        ("Price per box", "retail_price"),
        ("Price per 100g", "retail_price"),
        ("Cost per 100 g", "cost_price"),
    ])
    async def test_price_per_unknown_basis_is_rejected(self, client, session, perm, stage_dir, header, target):
        assert "price_basis_unsupported" in _header_codes({header: target}, "USD", header)
        await _assert_header_blocks_every_transport(client, session, perm, header, target, "USD", {"price_basis_unsupported"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,target", [
        ("Price_per_box", "retail_price"),
        ("price-per-box", "retail_price"),
        ("Cost_per_dozen", "cost_price"),
    ])
    async def test_price_basis_written_with_underscores_or_hyphens_follows_the_same_grammar(
            self, client, session, perm, stage_dir, header, target):
        assert "price_basis_unsupported" in _header_codes({header: target}, "USD", header)
        await _assert_header_blocks_every_transport(client, session, perm, header, target, "USD", {"price_basis_unsupported"})

    @pytest.mark.parametrize("header,target,basis", [
        ("Price_per_ct", "retail_price", "carat"),
        ("price-per-gram", "retail_price", "gram"),
        ("Cost_per_pc", "cost_price", "piece"),
    ])
    def test_recognized_basis_written_with_underscores_or_hyphens_is_preserved(self, header, target, basis):
        mapping = {header: target}
        assert _header_codes(mapping, "USD", header) == set()
        assert _mapped_row(mapping, "USD", {header: "10"})[f"{target}_basis"] == basis

    @pytest.mark.parametrize("header,target,basis", [
        ("Price/ct", "retail_price", "carat"),
        ("Price / ct", "retail_price", "carat"),
        ("Price/g", "retail_price", "gram"),
        ("Price per gram", "retail_price", "gram"),
        ("Cost/pc", "cost_price", "piece"),
    ])
    def test_recognized_slash_basis_is_preserved(self, header, target, basis):
        mapping = {header: target}
        assert _header_codes(mapping, "USD", header) == set()
        assert _mapped_row(mapping, "USD", {header: "10"})[f"{target}_basis"] == basis

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", ["csv", "xlsx"])
    async def test_recognized_slash_basis_is_preserved_on_file_and_browser_import(self, client, session, perm, stage_dir, fmt):
        cols, rows, mapping = _price_file("Price/ct", "retail_price", sell_by="carat", quantity="2", value="100")
        preview = await _file_preview(client, perm, _upload(perm, cols, rows, fmt), mapping)
        assert preview["errors"] == []
        assert preview["sample"][0]["retail_price_basis"] == "carat"

        file_id = _upload(perm, cols, [["Loose " + fmt, "carat", "2", "100"]], fmt)
        preview = await _file_preview(client, perm, file_id, mapping)
        r = await _file_commit(client, perm, file_id, mapping, preview["preview_hash"])
        assert r.status_code == 200 and r.json()["created"] == 1, r.text
        loose = next(s for s in await _item_states(session, perm["company_id"]) if s["name"] == "Loose " + fmt)
        assert (loose["retail_price"], loose["sell_by"]) == (100.0, "carat")

        _page, check = await _browser_mapped(_csv_text(cols, rows), mapping, "USD")
        check.assert_awaited_once()
        assert check.await_args.args[2][0]["retail_price_basis"] == "carat"


# ---------------------------------------------------------------------------
# Currency: an explicit source currency is never erased
# ---------------------------------------------------------------------------


class TestSourceCurrencyInvariant:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,target", [
        ("Price (USD)", "retail_price"),
        ("Price USD", "retail_price"),
        ("Cost [eur]", "cost_price"),
        ("Wholesale price GBP", "wholesale_price"),
        ("Total price (USD)", "retail_price_total"),
    ])
    async def test_foreign_iso_currency_is_blocked(self, client, session, perm, stage_dir, header, target):
        await _assert_header_blocks_every_transport(client, session, perm, header, target, "THB", {"price_currency_mismatch"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,target", [
        ("Retail usd", "retail_price"),
        ("usd retail", "retail_price"),
        ("Retail price in usd", "retail_price"),
        ("retail_usd", "retail_price"),
        ("Unit Price usd", "retail_price"),
        ("Wholesale eur", "wholesale_price"),
    ])
    async def test_lowercase_iso_currency_anywhere_in_monetary_header_is_detected(self, client, session, perm, stage_dir, header, target):
        # The company's own currency, written the same way, is not an error.
        same = header.replace("usd", "thb").replace("eur", "thb")
        assert _header_codes({same: target}, "THB", same) == set()
        await _assert_header_blocks_every_transport(client, session, perm, header, target, "THB", {"price_currency_mismatch"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["Price $", "Price ($)", "$ Retail", "Retail price ¥"])
    async def test_ambiguous_currency_symbol_requires_clarification(self, client, session, perm, stage_dir, header):
        # A symbol that several currencies share is never read as one ISO code,
        # and never stripped to import the bare number.
        assert "price_currency_ambiguous" in _header_codes({header: "retail_price"}, "THB", header)
        await _assert_header_blocks_every_transport(client, session, perm, header, "retail_price", "THB", {"price_currency_ambiguous"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,target", [
        ("Price (USD)", "retail_price"),
        ("Retail usd", "retail_price"),
        ("Price $", "retail_price"),
        ("Cost €", "cost_price"),
        ("Price in eur", "retail_price"),
        ("Wholesale (GBP)", "wholesale_price"),
        ("Price/ct USD", "retail_price"),
        ("Total cost usd", "cost_price_total"),
    ])
    async def test_source_currency_is_never_silently_dropped(self, client, session, perm, stage_dir, header, target):
        await _assert_header_blocks_every_transport(client, session, perm, header, target, "THB", _CURRENCY_CODES)


# ---------------------------------------------------------------------------
# Weight headers: the unit is read per mapped weight target, never invented
# ---------------------------------------------------------------------------


class TestWeightHeaderInvariant:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,unit", [
        ("Gross weight g", "gram"),
        ("gross_weight_ct", "carat"),
        ("Gross wt (kg)", "kg"),
        ("Gross weight oz", "oz"),
    ])
    async def test_gross_weight_header_carries_its_unit(self, client, session, perm, stage_dir, header, unit):
        mapping = {"Title": "name", "Sell by": "sell_by", "Qty": "quantity", "Carats": "weight", header: "gross_weight"}
        expected = {"weight": "1.5", "weight_unit": "carat", "gross_weight": "2.5", "gross_weight_unit": unit}
        source = {"Title": "Stone", "Sell by": "piece", "Qty": "1", "Carats": "1.5", header: "2.5"}
        mapped = _mapped_row(mapping, "USD", source)
        assert {k: mapped.get(k) for k in expected} == expected

        cols = list(source)
        for fmt in ("csv", "xlsx"):
            name = f"Stone {fmt}"
            file_id = _upload(perm, cols, [[name, "piece", "1", "1.5", "2.5"]], fmt)
            preview = await _file_preview(client, perm, file_id, mapping)
            assert preview["errors"] == []
            sample = preview["sample"][0]
            assert (str(_cell(str(sample["gross_weight"]))), sample.get("gross_weight_unit"), sample.get("weight_unit")) == ("2.5", unit, "carat")
            r = await _file_commit(client, perm, file_id, mapping, preview["preview_hash"])
            assert r.status_code == 200 and r.json()["created"] == 1, r.text
            stone = next(s for s in await _item_states(session, perm["company_id"]) if s["name"] == name)
            assert (stone["gross_weight"], stone["gross_weight_unit"], stone["weight"], stone["weight_unit"]) == (2.5, unit, 1.5, "carat")

        _page, check = await _browser_mapped(_csv_text(cols, [list(source.values())]), mapping, "USD")
        check.assert_awaited_once()
        browser = check.await_args.args[2][0]
        assert {k: browser.get(k) for k in expected} == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,target", [
        ("Weight", "weight"),
        ("Mass", "weight"),
        ("Weight (approx)", "weight"),
        ("weight_stone", "weight"),
        ("Gross weight", "gross_weight"),
        ("Gross mass tola", "gross_weight"),
    ])
    async def test_unknown_weight_header_does_not_invent_a_unit(self, client, session, perm, stage_dir, header, target):
        unit_key = f"{target}_unit"
        mapping = {"Title": "name", "Sell by": "sell_by", "Qty": "quantity", header: target}
        source = {"Title": "Stone", "Sell by": "piece", "Qty": "1", header: "2.5"}
        assert not _mapped_row(mapping, "USD", source).get(unit_key)
        # A net weight header's unit never spills onto an unlabelled gross weight.
        if target == "gross_weight":
            both = {**mapping, "Carats": "weight"}
            row = _mapped_row(both, "USD", {**source, "Carats": "1.5"})
            assert row.get("weight_unit") == "carat" and not row.get(unit_key)

        cols = list(source)
        for fmt in ("csv", "xlsx"):
            name = f"Plain {fmt}"
            file_id = _upload(perm, cols, [[name, "piece", "1", "2.5"]], fmt)
            preview = await _file_preview(client, perm, file_id, mapping)
            assert preview["errors"] == [] and not preview["sample"][0].get(unit_key)
            r = await _file_commit(client, perm, file_id, mapping, preview["preview_hash"])
            assert r.status_code == 200, r.text
            stone = next(s for s in await _item_states(session, perm["company_id"]) if s["name"] == name)
            assert not stone.get(unit_key)

        _page, check = await _browser_mapped(_csv_text(cols, [list(source.values())]), mapping, "USD")
        check.assert_awaited_once()
        assert not check.await_args.args[2][0].get(unit_key)


# ---------------------------------------------------------------------------
# Total price: a unit rate is derived only from a proven positive quantity,
# and a unit price never silently wins over its total
# ---------------------------------------------------------------------------


async def _sorted_states(session, company_id: str) -> list[str]:
    return sorted(json.dumps(s, sort_keys=True, default=str) for s in await _item_states(session, company_id))


def _row_codes(errors: list[dict]) -> set[tuple]:
    return {(e["field"], e["code"]) for e in errors}


async def _assert_rows_rejected(client, session, perm, rows: list[dict], expected: tuple, key: str, *, upsert: bool = False) -> None:
    """The row preview reports the error; a bound commit is refused with 422 and an
    unbound commit writes nothing for the row."""
    h = perm["admin_h"]
    before = await _sorted_states(session, perm["company_id"])
    preview = await _rows_preview(client, h, rows, upsert=upsert, key=key)
    assert expected in _row_codes(preview["errors"]), preview["errors"]
    r = await _rows_commit(client, h, rows, upsert=upsert, key=key, preview_hash=preview["preview_hash"])
    assert r.status_code == 422, r.text
    assert expected in _row_codes(r.json()["detail"]["errors"])
    r = await _rows_commit(client, h, rows, upsert=upsert, key=key + "-unbound")
    assert r.status_code in (200, 422), r.text
    assert await _sorted_states(session, perm["company_id"]) == before


async def _assert_file_rejected(client, session, perm, cols, rows, mapping, expected: tuple) -> None:
    before = await _item_count(session, perm["company_id"])
    for fmt in ("csv", "xlsx"):
        file_id = _upload(perm, cols, rows, fmt)
        preview = await _file_preview(client, perm, file_id, mapping)
        assert expected in _row_codes(preview["errors"]), (fmt, preview["errors"])
        r = await _file_commit(client, perm, file_id, mapping, preview["preview_hash"])
        assert r.status_code == 422, (fmt, r.text)
    assert await _item_count(session, perm["company_id"]) == before


class TestTotalPriceInvariant:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("row,field", [
        ({"name": "Widget", "sell_by": "piece", "quantity": "0", "retail_price_total": "100"}, "retail_price_total"),
        ({"name": "Widget", "sell_by": "piece", "pieces": "0", "retail_price_total": "100"}, "retail_price_total"),
        ({"name": "Widget", "sell_by": "piece", "quantity": "0", "wholesale_price_total": "80"}, "wholesale_price_total"),
    ])
    async def test_total_price_with_zero_quantity_is_rejected(self, client, session, perm, row, field):
        # A positive quantity derives the unit price; this keeps the rule from rejecting everything.
        await _import_clean(client, perm["admin_h"], [{"name": "Counted", "sell_by": "piece", "quantity": "4", "retail_price_total": "100"}], "total-ok")
        counted = next(s for s in await _item_states(session, perm["company_id"]) if s["name"] == "Counted")
        assert counted["retail_price"] == 25.0

        expected = (field, "price_total_needs_quantity")
        await _assert_rows_rejected(client, session, perm, [row], expected, "total-zero")
        cols = list(row)
        await _assert_file_rejected(client, session, perm, cols, [list(row.values())], {c: c for c in cols}, expected)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("row", [
        {"name": "Widget", "sell_by": "piece", "retail_price_total": "100"},
        {"name": "Loose", "sell_by": "carat", "retail_price_total": "100"},
    ])
    async def test_total_price_with_unresolved_quantity_is_rejected(self, client, session, perm, row):
        expected = ("retail_price_total", "price_total_needs_quantity")
        await _assert_rows_rejected(client, session, perm, [row], expected, "total-unresolved")
        cols = list(row)
        await _assert_file_rejected(client, session, perm, cols, [list(row.values())], {c: c for c in cols}, expected)

    @pytest.mark.asyncio
    async def test_total_price_with_unresolved_quantity_is_rejected_on_price_only_upsert(self, client, session, perm):
        # The existing item holds no stock, so a price-only update has no quantity to divide by.
        await _seed_items(client, perm["admin_h"], [{"name": "Held", "sku": "H-1", "sell_by": "piece", "quantity": "0"}], "seed-held")
        rows = [{"name": "Held", "sku": "H-1", "retail_price_total": "100"}]
        await _assert_rows_rejected(client, session, perm, rows, ("retail_price_total", "price_total_needs_quantity"), "upsert-total", upsert=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unit_key,unit,total", [
        ("retail_price", "25", "100"),
        ("retail_price", "10", "100"),
        ("cost_price", "25", "100"),
    ])
    async def test_unit_and_total_price_same_list_conflict_is_rejected(self, client, session, perm, unit_key, unit, total):
        total_key = f"{unit_key}_total"
        row = {"name": "Widget", "sell_by": "piece", "quantity": "4", unit_key: unit, total_key: total}
        expected = (total_key, "price_unit_total_conflict")
        await _assert_rows_rejected(client, session, perm, [row], expected, "unit-total")

        cols = ["name", "sell_by", "quantity", "Unit", "Total"]
        mapping = {"name": "name", "sell_by": "sell_by", "quantity": "quantity", "Unit": unit_key, "Total": total_key}
        await _assert_file_rejected(client, session, perm, cols, [["Widget", "piece", "4", unit, total]], mapping, expected)

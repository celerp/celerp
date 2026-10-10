# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Column mapping is one server-side contract for every import transport.

The browser mapping form and the file/agent preview and commit routes resolve a
source-column mapping through the same effective-mapping check: caller choices
override the server's suggestions instead of replacing them, and duplicate,
unknown, reserved, conflicting, or missing targets are refused by the server
rather than by browser script.
"""

from __future__ import annotations

import ast
import csv
import inspect
import io
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from test_import_invariants import _set_company_settings, write_upload  # noqa: F401

_REPO = Path(__file__).resolve().parents[1]

# Mapping-level error codes. A mapping error belongs to no row, so its "row" is None.
_DUPLICATE = "duplicate_target"
_UNKNOWN_TARGET = "unknown_target"
_INVALID_CATATTR = "invalid_category_attribute"
_RESERVED = "reserved_field_conflict"
_UNKNOWN_SOURCE = "unknown_source_column"
_PRICE_CONFLICT = "price_target_conflict"
_REQUIRED = "required_target_missing"

_CATEGORY_SCHEMAS = {
    "gem": [
        {"key": "clarity", "label": "Clarity", "type": "text"},
        {"key": "cut", "label": "Cut", "type": "text"},
    ],
}


@pytest.fixture
async def mapping_company(client, session):
    """An owner, a manager, and a company with one category that has attributes."""
    from celerp.services.auth import decode_access_token
    from test_helpers import perm_setup
    s = await perm_setup(client, session)
    claims = decode_access_token(s["admin_h"]["Authorization"].split()[1])
    s["company_id"], s["admin_user_id"] = claims["company_id"], claims["sub"]
    await _set_company_settings(
        session, s["company_id"],
        category_schemas=_CATEGORY_SCHEMAS,
        category_display_names={"gem": "Gem"},
    )
    return s


def _csv(header: list[str], *rows: list[str]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    for row in rows:
        w.writerow(row)
    return out.getvalue()


async def _preview(client, company, file_id: str, mapping: dict | None) -> dict:
    body: dict = {"file_id": file_id}
    if mapping is not None:
        body["mapping"] = mapping
    r = await client.post("/items/import/preview", json=body, headers=company["admin_h"])
    assert r.status_code == 200, r.text
    return r.json()


async def _commit(client, company, file_id: str, mapping: dict | None, preview_hash: str):
    body: dict = {"file_id": file_id, "preview_hash": preview_hash}
    if mapping is not None:
        body["mapping"] = mapping
    return await client.post("/items/import/commit", json=body, headers=company["admin_h"])


def _mapping_errors(errors: list[dict]) -> list[dict]:
    return [e for e in errors if e.get("row") is None]


def _codes(errors: list[dict]) -> set[str]:
    return {e.get("code") for e in _mapping_errors(errors)}


async def _assert_commit_refused(client, company, file_id, mapping, preview, code, monkeypatch):
    """The commit route recomputes the same mapping check and never reaches the writer."""
    import celerp_inventory.routes as routes
    writer = AsyncMock()
    monkeypatch.setattr(routes, "import_items", writer)
    r = await _commit(client, company, file_id, mapping, preview["preview_hash"])
    assert r.status_code == 422, r.text
    assert code in _codes(r.json()["detail"]["errors"]), r.text
    writer.assert_not_awaited()


# ---------------------------------------------------------------------------
# File/agent preview and commit refuse invalid mappings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["name", "sku", "retail_price"])
async def test_file_mapping_rejects_duplicate_core_targets(client, mapping_company, write_upload, monkeypatch, target):
    header = ["Title", "First", "Second"]
    fid = write_upload(mapping_company, _csv(header, ["Ruby", "10", "20"]))
    mapping = {"Title": "name", "First": target, "Second": target}
    if target == "name":
        mapping["Title"] = "__skip__"
    preview = await _preview(client, mapping_company, fid, mapping)
    dup = [e for e in _mapping_errors(preview["errors"]) if e["code"] == _DUPLICATE]
    assert dup and dup[0]["field"] == target, preview["errors"]
    await _assert_commit_refused(client, mapping_company, fid, mapping, preview, _DUPLICATE, monkeypatch)


@pytest.mark.asyncio
async def test_file_mapping_rejects_duplicate_category_attribute_targets(client, mapping_company, write_upload):
    fid = write_upload(mapping_company, _csv(["Name", "Clarity", "Clarity Grade"], ["Ruby", "VS1", "VS2"]))
    mapping = {"Name": "name", "Clarity": "__catattr:clarity", "Clarity Grade": "__catattr:clarity"}
    preview = await _preview(client, mapping_company, fid, mapping)
    dup = [e for e in _mapping_errors(preview["errors"]) if e["code"] == _DUPLICATE]
    assert dup and dup[0]["field"] == "clarity", preview["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["not_a_field", "Retail Price", "weight_total"])
async def test_file_mapping_rejects_unknown_destination(client, mapping_company, write_upload, target):
    fid = write_upload(mapping_company, _csv(["Name", "Extra"], ["Ruby", "x"]))
    preview = await _preview(client, mapping_company, fid, {"Name": "name", "Extra": target})
    assert _UNKNOWN_TARGET in _codes(preview["errors"]), preview["errors"]
    # An unknown destination never becomes an implicit custom attribute.
    assert all(target not in row for row in preview["sample"]), preview["sample"]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["sku", "quantity", "retail_price", "Name"])
async def test_file_mapping_rejects_reserved_field_as_custom_attribute(client, mapping_company, write_upload, header):
    # The column kept as a custom attribute is named like a canonical item field.
    fid = write_upload(mapping_company, _csv(["Title", header], ["Ruby", "7"]))
    mapping = {"Title": "name", header: "__attr__"}
    preview = await _preview(client, mapping_company, fid, mapping)
    assert _RESERVED in _codes(preview["errors"]), preview["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("header,value", [
    ("qty", "9"), ("Supplier_price", "12"), ("List_price", "abc"), ("bogus_price_total", "5"), ("Retail_price_basis", "box"),
])
async def test_custom_attribute_named_like_a_field_the_importer_reads_is_rejected(
        client, mapping_company, write_upload, monkeypatch, header, value):
    # Kept as a custom attribute, the column would otherwise be read as stock
    # quantity or a top-level price, or dropped.
    fid = write_upload(mapping_company, _csv(["Title", "sell_by", "quantity", header], ["Ruby", "piece", "1", value]))
    mapping = {"Title": "name", "sell_by": "sell_by", "quantity": "quantity", header: "__attr__"}
    preview = await _preview(client, mapping_company, fid, mapping)
    codes = {e["code"] for e in _mapping_errors(preview["errors"]) if e["field"] == header}
    assert codes & {_RESERVED, "reserved_field_unsupported"}, preview["errors"]
    code = next(iter(codes & {_RESERVED, "reserved_field_unsupported"}))
    await _assert_commit_refused(client, mapping_company, fid, mapping, preview, code, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["__catattr:not_an_attribute", "__catattr:", "__catattr:Clarity "])
async def test_file_mapping_rejects_invalid_category_attribute_target(client, mapping_company, write_upload, target):
    fid = write_upload(mapping_company, _csv(["Name", "Grade"], ["Ruby", "A"]))
    preview = await _preview(client, mapping_company, fid, {"Name": "name", "Grade": target})
    assert _INVALID_CATATTR in _codes(preview["errors"]), preview["errors"]


@pytest.mark.asyncio
async def test_unknown_source_key_in_mapping_is_rejected(client, mapping_company, write_upload):
    fid = write_upload(mapping_company, _csv(["Name", "SKU"], ["Ruby", "R-1"]))
    preview = await _preview(client, mapping_company, fid, {"Carats": "weight"})
    errs = [e for e in _mapping_errors(preview["errors"]) if e["code"] == _UNKNOWN_SOURCE]
    assert errs and errs[0]["field"] == "Carats", preview["errors"]


@pytest.mark.asyncio
async def test_unit_and_total_price_mapping_conflict_is_rejected(client, mapping_company, write_upload, monkeypatch):
    fid = write_upload(mapping_company, _csv(["Name", "Unit Price", "Line Total"], ["Ruby", "10", "999"]))
    mapping = {"Name": "name", "Unit Price": "retail_price", "Line Total": "retail_price_total"}
    preview = await _preview(client, mapping_company, fid, mapping)
    assert _PRICE_CONFLICT in _codes(preview["errors"]), preview["errors"]
    await _assert_commit_refused(client, mapping_company, fid, mapping, preview, _PRICE_CONFLICT, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,mapping", [
    (_csv(["Label", "SKU"], ["Ruby", "R-1"]), None),
    (_csv(["Name", "SKU"], ["Ruby", "R-1"]), {"Name": "__skip__"}),
    (_csv(["Label", "SKU"]), None),
], ids=["not_suggested", "skipped", "header_only"])
async def test_file_mapping_requires_name_target(client, mapping_company, write_upload, monkeypatch, text, mapping):
    fid = write_upload(mapping_company, text)
    preview = await _preview(client, mapping_company, fid, mapping)
    req = [e for e in _mapping_errors(preview["errors"]) if e["code"] == _REQUIRED]
    assert req and req[0]["field"] == "name", preview["errors"]
    await _assert_commit_refused(client, mapping_company, fid, mapping, preview, _REQUIRED, monkeypatch)


# ---------------------------------------------------------------------------
# Caller choices override suggestions; the effective mapping is shared
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_mapping_overrides_preserve_other_suggestions(client, mapping_company, write_upload):
    fid = write_upload(mapping_company, _csv(["Title", "Code", "Qty", "Carats"], ["Ruby", "R-1", "1", "1.2"]))
    suggested = (await _preview(client, mapping_company, fid, None))["mapping"]
    assert suggested["Title"] == "name" and suggested["Code"] == "sku", suggested

    preview = await _preview(client, mapping_company, fid, {"Carats": "weight"})
    assert preview["mapping"] == {**suggested, "Carats": "weight"}, preview["mapping"]
    assert _mapping_errors(preview["errors"]) == [], preview["errors"]
    assert preview["sample"][0]["name"] == "Ruby"
    assert preview["sample"][0]["sku"] == "R-1"
    assert preview["sample"][0]["weight"] == "1.2"


# (case id, header, row, file overrides, browser form choices beyond the suggestion)
_EFFECTIVE_CASES = [
    ("suggested_only", ["Title", "Code", "Clarity"], ["Ruby", "R-1", "VS1"], None, {}),
    ("partial_override", ["Title", "Code", "Carats"], ["Ruby", "R-1", "1.2"], {"Carats": "weight"}, {"Carats": "weight"}),
    ("skip_and_attribute", ["Title", "Code", "Cut", "Memo"], ["Ruby", "R-1", "Oval", "x"],
     {"Memo": "__skip__", "Cut": "__catattr:cut"}, {"Memo": "__skip__", "Cut": "__catattr:cut"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _EFFECTIVE_CASES, ids=[c[0] for c in _EFFECTIVE_CASES])
async def test_browser_and_file_mapping_produce_same_effective_mapping(client, mapping_company, write_upload, monkeypatch, case):
    import celerp.importers.tabular as tabular
    from celerp_inventory.services import (
        apply_source_semantics, build_item_import_spec, is_item_field_key, source_header_semantics,
    )
    from ui.routes.inventory import _union_category_attr_keys

    _id, header, row, overrides, browser_choices = case
    text = _csv(header, row)
    h = mapping_company["admin_h"]

    # Both transports resolve their mapping through the one shared primitive.
    real = tabular.normalize_and_validate_mapping
    results: list = []

    def spy(*a, **k):
        result = real(*a, **k)
        results.append(result)
        return result

    monkeypatch.setattr(tabular, "normalize_and_validate_mapping", spy)

    # Browser: the mapping form starts from the suggestion the browser renders
    # (core targets plus the company's category attributes) and submits every column.
    price_lists = (await client.get("/companies/me/price-lists", headers=h)).json()
    schemas = (await client.get("/companies/me/category-schemas", headers=h)).json()
    spec = build_item_import_spec(price_lists)
    browser_mapping = {**tabular.suggest_mapping(header, spec.cols, _union_category_attr_keys(schemas),
                                                    skip_cols=spec.skip_cols), **browser_choices}
    form = {f"map__{col}": target for col, target in browser_mapping.items()}
    assert tabular.validate_column_mapping(form, header, core_fields=spec.cols, required_targets=spec.required,
                                          is_reserved_field=is_item_field_key) == []
    assert len(results) == 1, "the browser mapping check must resolve through normalize_and_validate_mapping"
    assert results[0].mapping == browser_mapping
    _csv_text, _cols = tabular.apply_column_mapping(form, text)
    currency = (await client.get("/companies/me", headers=h)).json().get("settings", {}).get("currency") or "USD"
    browser_rows = apply_source_semantics(
        list(csv.DictReader(io.StringIO(_csv_text))), source_header_semantics(browser_mapping, currency),
    )

    # File/agent: the same file with only the caller's corrections.
    fid = write_upload(mapping_company, text)
    preview = await _preview(client, mapping_company, fid, overrides)
    assert len(results) == 2, "the file preview must resolve through normalize_and_validate_mapping"
    assert results[1].mapping == browser_mapping
    assert preview["mapping"] == browser_mapping
    assert _mapping_errors(preview["errors"]) == []
    assert preview["sample"] == browser_rows


# ---------------------------------------------------------------------------
# The row remapper never silently drops a value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cols,mapping", [
    (["A", "B"], {"A": "name", "B": "name"}),
    (["A", "B"], {"A": "__catattr:clarity", "B": "__catattr:clarity"}),
    (["A", "sku"], {"A": "sku", "sku": "__attr__"}),
], ids=["core", "category_attribute", "custom_named_like_core"])
def test_remap_rows_never_silently_overwrites_duplicate_destination(cols, mapping):
    from celerp.importers.tabular import remap_rows
    rows = [{c: f"v-{c}" for c in cols}]
    with pytest.raises(ValueError):
        remap_rows(cols, rows, mapping)


# ---------------------------------------------------------------------------
# Every mapper states its required-target policy
# ---------------------------------------------------------------------------

_MAPPER_NAMES = {"validate_column_mapping", "_csv_validate_column_mapping", "normalize_and_validate_mapping"}


def _mapper_calls(root: Path, pattern: str):
    for path in sorted(root.glob(pattern)):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if fname in _MAPPER_NAMES:
                yield path, node, fname


def test_every_mapper_caller_states_required_targets():
    import celerp.importers.tabular as tabular

    for name in ("validate_column_mapping", "normalize_and_validate_mapping"):
        fn = getattr(tabular, name, None)
        assert fn is not None, f"celerp.importers.tabular.{name} must exist"
        param = inspect.signature(fn).parameters.get("required_targets")
        assert param is not None, name
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert param.default is inspect.Parameter.empty, name

    seen: dict[str, int] = {}
    for root, pattern in ((_REPO / "ui" / "routes", "*.py"), (_REPO / "default_modules", "**/*.py")):
        for path, node, fname in _mapper_calls(root, pattern):
            seen[fname] = seen.get(fname, 0) + 1
            where = f"{path.relative_to(_REPO)}:{node.lineno}"
            assert not any(k.arg is None for k in node.keywords), f"{where} passes **kwargs"
            assert "required_targets" in {k.arg for k in node.keywords}, where

    # The file/agent inventory import resolves its mapping through the shared primitive.
    inventory_routes = _REPO / "default_modules" / "celerp-inventory" / "celerp_inventory"
    assert any(
        fname == "normalize_and_validate_mapping"
        for _p, _n, fname in _mapper_calls(inventory_routes, "*.py")
    ), "the file/agent import must call normalize_and_validate_mapping"
    assert seen.get("validate_column_mapping", 0) + seen.get("_csv_validate_column_mapping", 0) >= 9

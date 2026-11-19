# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A tax code says what it is, not only how much it charges: its category, its accounts, its
withholding point and its rate versions, each dated. A code keeps one id for life; documents
copy what they need from it when a line is added, so editing the code later changes no line
already written. Saving a tax list sends the fingerprint of the list the page showed, and a
list someone else changed since is refused instead of overwritten.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

import pytest

pytestmark = pytest.mark.asyncio

_PATH = {"sales": "/companies/me/taxes", "purchase": "/companies/me/purchasing-taxes"}
_STALE = "Taxes changed since this page loaded"


async def _register(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Tax Codes Co", "email": f"tc-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _list(client, h, side: str = "sales") -> list[dict]:
    r = await client.get(_PATH[side], headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _save(client, h, rows: list[dict], side: str = "sales", fingerprint: str | None = None):
    """PATCH a whole list with the fingerprint of the list as it stands now, unless one is given."""
    from celerp.localization.taxcodes import list_fingerprint
    if fingerprint is None:
        fingerprint = list_fingerprint(await _list(client, h, side))
    return await client.patch(_PATH[side], headers=h, json={"taxes": rows, "fingerprint": fingerprint})


async def _saved(client, h, rows: list[dict], side: str = "sales") -> list[dict]:
    r = await _save(client, h, rows, side)
    assert r.status_code == 200, r.text
    return await _list(client, h, side)


def _code(name: str, rate: float = 7.0, **fields) -> dict:
    return {"name": name, "tax_type": "both", "category": "standard", "versions": [{"rate": rate}], **fields}


def _refused(r, *names: str, status: int = 422) -> None:
    assert r.status_code == status, r.text
    for name in names:
        assert name in r.text, (name, r.text)


def _by_name(rows: list[dict], name: str) -> dict:
    return next(row for row in rows if row["name"] == name)


# ── S7 / V3: every I-1 field round-trips ────────────────────────────────────────

_FULL = {
    "name": "Input VAT", "tax_type": "purchase", "category": "reduced", "component": "VAT",
    "output_account": "2120", "input_account": "1150", "report_tags": ["box4", "input.reduced"],
    "cash_basis": False, "withholding_point": None,
    "versions": [{"rate": 5.0, "valid_from": None, "valid_to": "2026-06-30", "base_fraction": None},
                 {"rate": 6.0, "valid_from": "2026-07-01", "valid_to": None, "base_fraction": 0.5}],
    "is_default": True, "is_compound": False, "default_order": 2, "description": "Reduced input VAT",
}


async def test_tax_code_semantics_fields_roundtrip(client):
    h = await _register(client)
    [stored] = await _saved(client, h, [_FULL], "purchase")
    assert re.fullmatch(r"u-[0-9a-f]{12}", stored["id"]), stored
    assert {k: stored[k] for k in _FULL} == _FULL
    assert "rate" not in stored

    wht = {**_code("WHT 3%", 3.0, tax_type="purchase"), "category": "withholding", "withholding_point": "payment"}
    rows = await _saved(client, h, [stored, wht], "purchase")
    assert _by_name(rows, "WHT 3%")["withholding_point"] == "payment"

    bad = {
        "tax_type": "luxury", "category": "luxury", "withholding_point": "monthly",
        "output_account": "9999", "input_account": "9999",
        "is_default": "yes", "is_compound": "yes", "default_order": -1,
    }
    for field, value in bad.items():
        _refused(await _save(client, h, [{**_FULL, field: value}], "purchase"), field)
    _refused(await _save(client, h, [{**_FULL, "default_order": 1.5}], "purchase"), "default_order")
    no_point = {**wht, "withholding_point": None}
    _refused(await _save(client, h, [no_point], "purchase"), "withholding_point")
    assert await _list(client, h, "purchase") == rows


# ── S8 / V2: versions ───────────────────────────────────────────────────────────

async def test_overlapping_versions_refused(client):
    h = await _register(client)
    adjacent = _code("VAT", versions=[{"rate": 7.0, "valid_to": "2026-12-31"},
                                      {"rate": 8.0, "valid_from": "2027-01-01"}])
    [stored] = await _saved(client, h, [adjacent])
    assert [v["rate"] for v in stored["versions"]] == [7.0, 8.0]

    overlap = {**stored, "versions": [{"rate": 7.0, "valid_from": "2026-01-01", "valid_to": "2026-12-31"},
                                      {"rate": 8.0, "valid_from": "2026-06-01"}]}
    _refused(await _save(client, h, [overlap]), "VAT", "2026-12-31", "2026-06-01")
    backwards = {**stored, "versions": [{"rate": 7.0, "valid_from": "2026-06-01", "valid_to": "2026-01-01"}]}
    _refused(await _save(client, h, [backwards]), "VAT", "2026-06-01", "2026-01-01")
    late_open = {**stored, "versions": [{"rate": 7.0, "valid_to": "2026-12-31"}, {"rate": 8.0}]}
    _refused(await _save(client, h, [late_open]), "VAT", "valid_from")
    for fraction in (0, 1.5):
        _refused(await _save(client, h, [{**stored, "versions": [{"rate": 7.0, "base_fraction": fraction}]}]),
                 "VAT", "base_fraction")
    assert await _list(client, h) == [stored]


async def test_negative_rate_refused(client):
    h = await _register(client)
    [stored] = await _saved(client, h, [_code("Input VAT", 10.0, tax_type="purchase")], "purchase")
    for side in ("purchase", "sales"):
        _refused(await _save(client, h, [_code("WHT 3%", -3.0, tax_type="purchase")], side), "WHT 3%")
    assert await _list(client, h, "purchase") == [stored]


# ── S7 / V2: a stale table never drops what another writer added ───────────────

async def test_tax_list_stale_fingerprint_refused(client):
    from celerp.localization.taxcodes import list_fingerprint
    from test_l10n_country_update import _confirm, _preview

    h = await _register(client)
    rendered = list_fingerprint(await _list(client, h))
    r = await client.patch("/companies/me", headers=h, json={"settings": {"country": "TH"}})
    assert r.status_code == 200, r.text
    preview = await _preview(client, {"headers": h})
    assert (await _confirm(client, {"headers": h}, preview["fingerprint"])).status_code == 200
    applied = await _list(client, h)

    _refused(await _save(client, h, [_code("Mine")], fingerprint=rendered), _STALE, status=409)
    assert await _list(client, h) == applied

    shown = list_fingerprint(applied)
    vat = _by_name(applied, "VAT 7%")
    [first] = vat["versions"]
    dated = {**vat, "versions": [{**first, "valid_to": "2026-12-31"}, {"rate": 8.0, "valid_from": "2027-01-01"}]}
    others = [row for row in applied if row["id"] != vat["id"]]
    after = await _saved(client, h, [dated, *others])
    _refused(await _save(client, h, applied, fingerprint=shown), _STALE, status=409)
    assert await _list(client, h) == after
    assert len(_by_name(after, "VAT 7%")["versions"]) == 2


# ── V34: identity and lifecycle ─────────────────────────────────────────────────

async def _draft_invoice(client, h, code_id: str, day: str = "2026-10-01") -> dict:
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "issue_date": day, "line_items": [
        {"description": "Service", "quantity": 1, "unit_price": 100.0, "taxes": [{"code_id": code_id}]}]})
    assert r.status_code == 200, r.text
    return (await client.get(f"/docs/{r.json()['id']}", headers=h)).json()


async def test_tax_code_identity_and_lifecycle(client):
    h = await _register(client)
    [vat] = await _saved(client, h, [_code("VAT", output_account="2120")])
    assert re.fullmatch(r"u-[0-9a-f]{12}", vat["id"])

    _refused(await _save(client, h, [vat, {**_code("Other"), "id": "u-000000000000"}]), "id")
    _refused(await _save(client, h, [{**vat, "id": "u-ffffffffffff"}]), "id")
    _refused(await _save(client, h, [vat, {**vat, "name": "VAT copy"}]), "id")
    long_tag = "t" * 33
    for field, value in (("name", ""), ("name", "n" * 65), ("component", "c" * 33),
                         ("report_tags", [f"t{i}" for i in range(21)]), ("report_tags", ["bad tag"]),
                         ("report_tags", [long_tag]), ("description", "d" * 501)):
        _refused(await _save(client, h, [{**vat, field: value}]), field)
    _refused(await _save(client, h, [vat, {**_code("vat")}]), "name")
    assert await _list(client, h) == [vat]

    draft = await _draft_invoice(client, h, vat["id"])
    [line_tax] = draft["line_items"][0]["taxes"]
    assert line_tax["code_id"] == vat["id"] and line_tax["name"] == "VAT"

    [renamed] = await _saved(client, h, [{**vat, "name": "Output VAT", "output_account": "2130",
                                         "report_tags": ["box1"], "component": "VAT-A"}])
    assert renamed["id"] == vat["id"]
    kept = (await client.get(f"/docs/{draft['id']}", headers=h)).json()["line_items"][0]["taxes"][0]
    assert kept == line_tax

    r = await client.post("/items", headers=h, json={"sku": f"S-{uuid.uuid4().hex[:6]}", "name": "Widget",
                                                     "tax_codes": [vat["id"]]})
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    removal = await _save(client, h, [])
    _refused(removal, "1 item", "1 draft", status=409)
    assert f"tax_codes={vat['id']}" in removal.text
    assert await _list(client, h) == [renamed]

    assert (await client.post(f"/docs/{draft['id']}/finalize", headers=h)).status_code == 200
    r = await client.patch(f"/items/{item_id}", headers=h, json={"tax_codes": []})
    assert r.status_code == 200, r.text
    assert await _saved(client, h, []) == []
    issued = (await client.get(f"/docs/{draft['id']}", headers=h)).json()["line_items"][0]["taxes"][0]
    assert issued == line_tax

    [again] = await _saved(client, h, [_code("VAT")])
    assert again["id"] != vat["id"]


# ── V35: no rate is ever guessed ────────────────────────────────────────────────

async def test_rate_on_no_version_in_force_refused(client):
    from celerp.localization.taxcodes import NoRateInForce, TaxCode, rate_on

    gap = TaxCode(name="VAT", tax_type="both", category="standard", versions=[
        {"rate": 7.0, "valid_from": "2026-01-01", "valid_to": "2026-06-30"},
        {"rate": 8.0, "valid_from": "2026-08-01", "valid_to": "2026-12-31"}])
    assert rate_on(gap, dt.date(2026, 1, 1)) == 7.0 and rate_on(gap, dt.date(2026, 12, 31)) == 8.0
    for day in (dt.date(2025, 12, 31), dt.date(2026, 7, 15), dt.date(2027, 1, 1)):
        with pytest.raises(NoRateInForce) as caught:
            rate_on(gap, day)
        assert "VAT" in str(caught.value) and day.isoformat() in str(caught.value)
    open_start = TaxCode(name="Old VAT", tax_type="both", versions=[{"rate": 7.0}])
    assert rate_on(open_start, dt.date(1990, 1, 1)) == 7.0

    h = await _register(client)
    [stored] = await _saved(client, h, [{**_code("VAT"), "versions": [
        {"rate": 7.0, "valid_from": "2026-01-01", "valid_to": "2026-06-30"},
        {"rate": 8.0, "valid_from": "2026-08-01"}]}])
    for day in ("2025-12-31", "2026-07-15"):
        r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "issue_date": day, "line_items": [
            {"description": "Service", "quantity": 1, "unit_price": 100.0, "taxes": [{"code_id": stored["id"]}]}]})
        _refused(r, f"No rate for VAT on {day}", "Settings > Taxes")
    in_force = await _draft_invoice(client, h, stored["id"], "2026-08-01")
    assert in_force["line_items"][0]["taxes"][0]["rate"] == 8.0


# ── S7 / V36: cash basis needs a cut-off and a category that waits for cash ────

async def test_cash_basis_code_needs_cutoff_and_category(client):
    from test_l10n_country_update import _confirm, _preview

    h = await _register(client)
    _refused(await _save(client, h, [_code("IVA caja", 21.0, cash_basis=True)]), "IVA caja")

    async def apply(country: str):
        r = await client.patch("/companies/me", headers=h, json={"settings": {"country": country}})
        assert r.status_code == 200, r.text
        preview = await _preview(client, {"headers": h})
        return await _confirm(client, {"headers": h}, preview["fingerprint"])

    assert (await apply("TH")).status_code == 200
    _refused(await _save(client, h, [*await _list(client, h), _code("IVA caja", 21.0, cash_basis=True)]),
             "IVA caja")

    assert (await apply("zzES")).status_code == 200
    rows = await _saved(client, h, [*await _list(client, h), _code("IVA caja", 21.0, cash_basis=True)])
    assert _by_name(rows, "IVA caja")["cash_basis"] is True
    for category, extra in (("withholding", {"withholding_point": "payment"}), ("perception", {}),
                            ("reverse_charge", {}), ("self_assessed_import", {})):
        code = {**_code(f"Caja {category}", 1.0, cash_basis=True), "category": category, **extra}
        _refused(await _save(client, h, [*rows, code]), f"Caja {category}")

    r = await client.patch("/companies/me", headers=h, json={"settings": {"country": "TH"}})
    assert r.status_code == 200, r.text
    preview = await _preview(client, {"headers": h})
    _refused(await _confirm(client, {"headers": h}, preview["fingerprint"]), "IVA caja", status=409)
    assert await _list(client, h) == rows


# ── S9: a date move across a rate version is noticed, never re-snapshotted silently ─────

async def _patch_doc(client, h, doc_id: str, field: str, value):
    from test_helpers import doc_version
    return await client.patch(f"/docs/{doc_id}", headers=h, json={
        "expected_version": await doc_version(client, h, doc_id), "fields_changed": {field: {"new": value}}})


async def _doc(client, h, doc_id: str) -> dict:
    return (await client.get(f"/docs/{doc_id}", headers=h)).json()


async def test_version_boundary_notice_on_date_move(client):
    from ui.routes.documents import _tax_version_notice

    h = await _register(client)
    [vat] = await _saved(client, h, [{**_code("VAT"), "versions": [
        {"rate": 7.0, "valid_to": "2026-06-30"}, {"rate": 8.0, "valid_from": "2026-07-01"}]}])
    draft = await _draft_invoice(client, h, vat["id"], "2026-06-15")
    [before] = draft["line_items"][0]["taxes"]
    assert before["rate"] == 7.0
    assert _tax_version_notice(draft, [vat]) is None

    r = await _patch_doc(client, h, draft["id"], "issue_date", "2026-07-15")
    assert r.status_code == 200, r.text
    moved = await _doc(client, h, draft["id"])
    assert moved["line_items"][0]["taxes"] == [before]
    notice = _tax_version_notice(moved, [vat])
    assert notice is not None
    from fasthtml.common import to_xml
    text = to_xml(notice)
    assert "Service" in text and "Use the rate in force" in text

    lines = [{**moved["line_items"][0], "taxes": [{"code_id": vat["id"]}]}]
    r = await _patch_doc(client, h, draft["id"], "line_items", lines)
    assert r.status_code == 200, r.text
    resnapped = await _doc(client, h, draft["id"])
    [after] = resnapped["line_items"][0]["taxes"]
    assert after["rate"] == 8.0 and after["version_from"] == "2026-07-01"
    assert _tax_version_notice(resnapped, [vat]) is None


# ── V37: the line tax snapshot is built by the server from the code ────────────────

_FROM_CODE = "Tax details come from the tax code VAT; change the code, or its rate in Settings > Taxes"


async def test_line_snapshot_fields_server_built(client):
    h = await _register(client)
    [vat] = await _saved(client, h, [_code("VAT", output_account="2120")])
    draft = await _draft_invoice(client, h, vat["id"])
    line = draft["line_items"][0]
    [snap] = line["taxes"]
    for field, value in (("rate", 9.0), ("category", "zero"), ("account", "2130"), ("inclusive", True),
                         ("base", 1.0), ("amount", -1.0), ("name", "Other"), ("version_from", "2020-01-01")):
        r = await _patch_doc(client, h, draft["id"], "line_items", [{**line, "taxes": [{**snap, field: value}]}])
        _refused(r, _FROM_CODE)
        assert (await _doc(client, h, draft["id"]))["line_items"][0]["taxes"] == [snap], field
    r = await _patch_doc(client, h, draft["id"], "line_items", [{**line, "taxes": [snap]}])
    assert r.status_code == 200, r.text
    assert (await _doc(client, h, draft["id"]))["line_items"][0]["taxes"] == [snap]
    r = await _patch_doc(client, h, draft["id"], "line_items", [{**line, "taxes": [{**snap, "amount": 3.5}]}])
    assert r.status_code == 200, r.text
    assert (await _doc(client, h, draft["id"]))["line_items"][0]["taxes"][0]["amount"] == 3.5

    def _new(tax: dict) -> dict:
        return {"doc_type": "invoice", "issue_date": "2026-10-01", "line_items": [
            {"description": "Service", "quantity": 1, "unit_price": 100.0, "taxes": [tax]}]}
    _refused(await client.post("/docs", headers=h, json=_new({"code_id": vat["id"], "rate": 9})), _FROM_CODE)
    r = await client.post("/docs", headers=h, json=_new({"code_id": vat["id"], "name": "VAT", "rate": 7.0}))
    assert r.status_code == 200, r.text


async def test_line_snapshot_frozen_on_rename_retag(client):
    h = await _register(client)
    [vat] = await _saved(client, h, [_code("VAT", output_account="2120", component="VAT", report_tags=["box1"])])
    draft = await _draft_invoice(client, h, vat["id"])
    issued = await _draft_invoice(client, h, vat["id"])
    assert (await client.post(f"/docs/{issued['id']}/finalize", headers=h)).status_code == 200
    snaps = {d: (await _doc(client, h, d))["line_items"][0]["taxes"] for d in (draft["id"], issued["id"])}
    assert snaps[draft["id"]][0]["report_tags"] == ["box1"] and snaps[draft["id"]][0]["account"] == "2120"

    await _saved(client, h, [{**vat, "name": "Output VAT", "report_tags": ["box6"], "component": "GST",
                             "output_account": "2130"}])
    for doc_id, snap in snaps.items():
        assert (await _doc(client, h, doc_id))["line_items"][0]["taxes"] == snap


# ── P2.3: an item's own tax codes win on its line ────────────────────────────────

async def test_item_tax_codes_line_default(client):
    h = await _register(client)
    vat, reduced = await _saved(client, h, [_code("VAT 7%", is_default=True), _code("Reduced 5%", 5.0)])
    skus = {}
    for key, codes in (("a", [reduced["id"]]), ("b", None)):
        sku = f"S-{uuid.uuid4().hex[:6]}"
        body = {"sku": sku, "name": f"Item {key}", "status": "available", "sell_by": "piece", "quantity": 5,
                "retail_price": 20, "cost_price": 8, **({"tax_codes": codes} if codes else {})}
        r = await client.post("/items", headers=h, json=body)
        assert r.status_code == 200, r.text
        skus[key] = sku
    r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "issue_date": "2026-10-01",
                                                    "line_items": []})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    for key in ("a", "b"):
        r = await client.post(f"/docs/{doc_id}/scan", headers=h, json={"barcode": skus[key], "run_key": key})
        assert r.status_code == 200 and r.json()["scanned"] == 1, r.text
    lines = (await _doc(client, h, doc_id))["line_items"]
    taxes = {line["sku"]: line["taxes"] for line in lines}
    assert [(t["code_id"], t["rate"]) for t in taxes[skus["a"]]] == [(reduced["id"], 5.0)]
    assert [(t["code_id"], t["rate"]) for t in taxes[skus["b"]]] == [(vat["id"], 7.0)]

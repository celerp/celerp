# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shipping documents: one shipment record (list_type="shipping_doc") with two
printouts - the Delivery Note (no prices) and the Commercial Invoice (customs)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from celerp.output.doc_print import render_doc_print_html
from celerp.services.list_behavior import LIST_BEHAVIOR, LIST_TYPES, behavior, is_money_list
from celerp.services.shipping import (
    COUNTRIES, INCOTERMS_2020, REASONS_FOR_EXPORT, SHIPMENT_HEADER_FIELDS,
    customs_backfill, line_gross_weight,
)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _token(client: AsyncClient) -> str:
    return (await client.post("/auth/register", json={
        "company_name": "ShipCo", "email": "ship@test.com", "name": "A", "password": "password123"})).json()["access_token"]


_LINE = {"name": "Widget", "sku": "W1", "quantity": 3, "unit_price": 10.5,
         "line_total": 31.5, "hs_code": "8471.30", "country_of_origin": "US", "weight": 1.2}


async def _make_list(client, tok, list_type, **extra) -> dict:
    r = await client.post("/lists", json={
        "list_type": list_type, "customer_name": "Buyer",
        "line_items": [dict(_LINE)],
        **extra,
    }, headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


async def _issued_doc(client, tok, doc_type: str) -> str:
    """Create and finalize an invoice or memo carrying one customs-complete line."""
    r = await client.post("/docs", json={
        "doc_type": doc_type, "contact_name": "Buyer GmbH",
        "contact_shipping_address": "1 Dock St, Hamburg, DE",
        "currency": "USD",
        "line_items": [dict(_LINE)],
        "subtotal": 31.5, "total": 31.5,
    }, headers=_h(tok))
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    r = await client.post(f"/docs/{doc_id}/finalize", headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json().get("new_entity_id") or doc_id


# ── behavior registry ─────────────────────────────────────────────────────────

def test_shipping_doc_is_registered_non_financial_customs():
    assert "shipping_doc" in LIST_TYPES
    assert "delivery_note" not in LIST_TYPES  # the rename is complete, no alias survives
    b = behavior("shipping_doc")
    assert b.label == "Shipping Document"
    assert is_money_list("shipping_doc") is False
    assert b.customs is True
    # regression: existing types unchanged
    assert is_money_list("quotation") is True
    assert all(not LIST_BEHAVIOR[lt].customs for lt in LIST_TYPES if lt != "shipping_doc")


def test_country_of_origin_is_a_base_item_field():
    from celerp.services.field_schema import _BASE_FIELDS
    keys = {f["key"] for f in _BASE_FIELDS}
    assert "country_of_origin" in keys
    assert "hs_code" in keys  # regression


def test_incoterms_is_the_closed_2020_set():
    assert INCOTERMS_2020 == ("EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF")


def test_shipment_fields_are_declared_on_the_payload():
    """The shipment contract is declared, never smuggled through extra="allow"."""
    from celerp_docs.routes import ListCreatePayload
    declared = set(ListCreatePayload.model_fields)
    missing = set(SHIPMENT_HEADER_FIELDS) - declared
    assert not missing, f"undeclared shipment fields: {missing}"
    assert {"contact_shipping_address", "shipping_attn", "source_doc"} <= declared


# ── numbering ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shipping_doc_numbered_ship_and_lists_stay_lst(client: AsyncClient):
    tok = await _token(client)
    ship = await _make_list(client, tok, "shipping_doc")
    quote = await _make_list(client, tok, "quotation")
    assert ship["id"].startswith("list:SHIP-")
    assert quote["id"].startswith("list:LST-")


@pytest.mark.asyncio
async def test_duplicate_keeps_the_ship_counter(client: AsyncClient):
    tok = await _token(client)
    ship = await _make_list(client, tok, "shipping_doc")
    r = await client.post(f"/lists/{ship['id']}/duplicate", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["id"].startswith("list:SHIP-"), "a duplicated shipment must stay in the SHIP sequence"


# ── shipment fields: persist, validate, no financial effect ──────────────────

@pytest.mark.asyncio
async def test_shipment_and_customs_fields_persist(client: AsyncClient):
    tok = await _token(client)
    ship = await _make_list(client, tok, "shipping_doc",
                            contact_shipping_address="123 Dock St", carrier="FedEx",
                            tracking="FX123", package_count=2, incoterms="DAP",
                            gross_weight="3.6 kg", reason_for_export="sale",
                            country_of_export="Thailand", country_of_destination="Germany",
                            importer="Import GmbH, Hamburg")
    state = (await client.get(f"/lists/{ship['id']}", headers=_h(tok))).json()
    assert state["carrier"] == "FedEx"
    assert state["tracking"] == "FX123"
    assert state["contact_shipping_address"] == "123 Dock St"
    assert state["incoterms"] == "DAP"
    assert state["package_count"] == 2
    assert state["gross_weight"] == "3.6 kg"
    assert state["reason_for_export"] == "sale"
    assert state["country_of_export"] == "Thailand"
    assert state["country_of_destination"] == "Germany"
    assert state["importer"] == "Import GmbH, Hamburg"
    line = state["line_items"][0]
    assert line["hs_code"] == "8471.30"
    assert line["country_of_origin"] == "US"


@pytest.mark.asyncio
async def test_closed_set_fields_reject_unknown_values(client: AsyncClient):
    tok = await _token(client)
    r = await client.post("/lists", json={"list_type": "shipping_doc", "incoterms": "FREE SHIPPING"},
                          headers=_h(tok))
    assert r.status_code == 422
    ship = await _make_list(client, tok, "shipping_doc")
    r = await client.patch(f"/lists/{ship['id']}", json={
        "fields_changed": {"reason_for_export": {"new": "smuggling"}}}, headers=_h(tok))
    assert r.status_code == 422
    r = await client.patch(f"/lists/{ship['id']}", json={
        "fields_changed": {"incoterms": {"new": "CIF"}}}, headers=_h(tok))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_finalize_posts_no_journal_entry_and_moves_no_stock(client: AsyncClient, session):
    tok = await _token(client)
    ship = await _make_list(client, tok, "shipping_doc")
    r = await client.post(f"/lists/{ship['id']}/finalize", headers=_h(tok))
    assert r.status_code == 200, r.text
    import sqlalchemy as sa
    from celerp.models.projections import Projection
    jes = (await session.execute(sa.select(Projection).where(
        Projection.entity_type == "je"))).scalars().all()
    assert [j for j in jes if ship["id"] in str(j.state)] == []
    state = (await client.get(f"/lists/{ship['id']}", headers=_h(tok))).json()
    assert state["status"] == "finalized"
    assert state.get("issued_at")


# ── create from invoice / consignment ────────────────────────────────────────

@pytest.mark.asyncio
async def test_shipment_from_invoice_copies_lines_contact_and_links_source(client: AsyncClient):
    tok = await _token(client)
    doc_id = await _issued_doc(client, tok, "invoice")
    r = await client.post(f"/docs/{doc_id}/shipment", headers=_h(tok))
    assert r.status_code == 200, r.text
    ship_id = r.json()["id"]
    assert ship_id.startswith("list:SHIP-")
    state = (await client.get(f"/lists/{ship_id}", headers=_h(tok))).json()
    assert state["list_type"] == "shipping_doc"
    assert state["status"] == "draft"
    assert state["source_doc"] == doc_id
    assert state["contact_name"] == "Buyer GmbH"
    assert state["contact_shipping_address"] == "1 Dock St, Hamburg, DE"
    assert state["reason_for_export"] == "sale"
    line = state["line_items"][0]
    assert line["quantity"] == 3
    assert line["unit_price"] == 10.5  # doubles as the customs value
    assert line["line_total"] == 31.5
    assert state["subtotal"] == 31.5  # the declared total the commercial invoice prints


@pytest.mark.asyncio
async def test_shipment_from_memo_defaults_not_for_resale(client: AsyncClient):
    tok = await _token(client)
    doc_id = await _issued_doc(client, tok, "memo")
    r = await client.post(f"/docs/{doc_id}/shipment", headers=_h(tok))
    assert r.status_code == 200, r.text
    state = (await client.get(f"/lists/{r.json()['id']}", headers=_h(tok))).json()
    assert state["reason_for_export"] == "not_for_resale"
    assert state["source_doc"] == doc_id


@pytest.mark.asyncio
async def test_shipment_rejects_wrong_type_and_draft(client: AsyncClient):
    tok = await _token(client)
    r = await client.post("/docs", json={"doc_type": "purchase_order",
                                         "line_items": [dict(_LINE)]}, headers=_h(tok))
    po_id = r.json()["id"]
    assert (await client.post(f"/docs/{po_id}/shipment", headers=_h(tok))).status_code == 422
    r = await client.post("/docs", json={"doc_type": "invoice",
                                         "line_items": [dict(_LINE)]}, headers=_h(tok))
    draft_id = r.json()["id"]
    assert (await client.post(f"/docs/{draft_id}/shipment", headers=_h(tok))).status_code == 409


# ── printouts: the two papers off one record ─────────────────────────────────

_PRINT_DOC = {
    "doc_type": "list", "list_type": "shipping_doc", "ref_id": "SHIP-2607-0001",
    "currency": "USD", "issue_date": "2026-07-14",
    "company_name": "Acme Exports", "company_tax_id": "US-TX-1",
    "contact_name": "Buyer GmbH", "contact_tax_id": "DE123456789",
    "contact_shipping_address": "1 Dock St, Hamburg, DE",
    "carrier": "FedEx", "tracking": "FX123", "incoterms": "DAP", "package_count": 2,
    "reason_for_export": "sale", "source_doc": "doc:INV-2607-0009",
    "country_of_export": "United States", "country_of_destination": "Germany",
    "line_items": [{"sku": "W1", "name": "Widget", "quantity": 3, "unit_price": 10.5,
                    "line_total": 31.5, "hs_code": "8471.30", "country_of_origin": "US",
                    "weight": 1.2, "weight_unit": "kg",
                    "gross_weight": 1.5, "gross_weight_unit": "kg"}],
}


def test_delivery_note_prints_logistics_and_no_money():
    html = render_doc_print_html(dict(_PRINT_DOC))
    assert "Delivery Note" in html
    assert "SHIP-2607-0001" in html
    for logistics in ("FedEx", "FX123", "DAP", "1 Dock St, Hamburg, DE",
                      "Net Weight", "Received in good order"):
        assert logistics in html, f"delivery note missing {logistics}"
    # Structurally price-free, and no customs columns either: the receiver checks
    # goods in; customs reads the commercial invoice.
    for absent in ("Unit Price", "Unit Value", "Subtotal", "Total", "Customs", "$", "10.5", "31.5",
                   "HS Code", "8471.30", "Country of Origin"):
        assert absent not in html, f"delivery note leaked {absent}"


def test_commercial_invoice_follows_the_carrier_form():
    html = render_doc_print_html(dict(_PRINT_DOC), layout="commercial_invoice")
    assert "Commercial Invoice" in html
    assert "Shipping Invoice" not in html  # carriers reject any other title
    for required in (
            # FedEx-style header grid
            "Date of Export", "2026-07-14", "Export References", "INV-2607-0009",
            "Shipper / Exporter", "US-TX-1", "Recipient / Consignee", "DE123456789",
            "Country of Export", "United States", "Country of Manufacture",
            "Country of Ultimate Destination", "Germany",
            "Importer - if other than recipient",
            "Carrier / Air Waybill No.", "FedEx / FX123", "Currency",
            "Incoterms", "DAP", "Reason for Export", "Sale",
            # goods table: net vs gross weight per line
            "8471.30", "Net Wt", "Gross Wt", "1.2 kg", "1.5 kg",
            "Unit Value", "Total Value",
            # totals band + declaration
            "Total Packages", "Total Net Weight", "Total Gross Weight",
            "Total Declared Value (USD)", "31.50",
            "I declare all the information contained in this invoice to be true and correct"):
        assert required in html, f"commercial invoice missing {required}"


def test_signature_blocks_are_vertical():
    dn = render_doc_print_html(dict(_PRINT_DOC))
    ci = render_doc_print_html(dict(_PRINT_DOC), layout="commercial_invoice")
    for html in (dn, ci):
        block = html[html.rindex("dp-sign-v"):]  # the body block (earlier hits are CSS rules)
        assert block.index("Name:") < block.index("Signature:") < block.index("Date:")


def test_missing_weights_print_blank_on_both_papers():
    """No weight data anywhere: the papers still work, with genuinely blank cells -
    never "0 kg", a bare unit, or a placeholder."""
    doc = dict(_PRINT_DOC)
    doc.pop("gross_weight", None)
    doc.pop("package_count", None)
    doc["line_items"] = [{"sku": "W1", "name": "Widget", "quantity": 3, "unit_price": 10.5,
                          "line_total": 31.5, "hs_code": "8471.30", "country_of_origin": "US"}]
    dn = render_doc_print_html(dict(doc))
    ci = render_doc_print_html(dict(doc), layout="commercial_invoice")
    for html in (dn, ci):
        body = html[html.index("<body"):]
        for bad in ("0 kg", "0kg", ">kg<", "> kg<", "None", "0.0", ">--<", ">__<"):
            assert bad not in body, f"missing weight rendered as {bad!r}"
    # The delivery note's logistics strip simply omits the Gross Weight slot...
    assert "Gross Weight" not in dn
    # ...while the customs form keeps its labeled slots with blank values.
    assert "Total Net Weight" in ci and "Total Gross Weight" in ci and "Total Packages" in ci


def test_partially_weighed_lines_never_print_a_partial_total():
    """One weighed line + one unweighed line: a summed total would understate the
    shipment, so the computed weight totals stay blank (the typed package weight
    still wins when present)."""
    doc = dict(_PRINT_DOC)
    doc.pop("gross_weight", None)
    doc["line_items"] = [
        {"sku": "A", "name": "Weighed", "quantity": 1, "unit_price": 10, "line_total": 10,
         "weight": 1.2, "weight_unit": "kg", "gross_weight": 1.5, "gross_weight_unit": "kg"},
        {"sku": "B", "name": "Unweighed", "quantity": 1, "unit_price": 5, "line_total": 5},
    ]
    ci = render_doc_print_html(dict(doc), layout="commercial_invoice")
    assert "1.2 kg" in ci and "1.5 kg" in ci      # per-line weights still print
    body = ci[ci.index("Total Net Weight"):ci.index("Total Declared Value")]
    assert "kg" not in body                        # but no partial totals


def test_gross_weight_header_prefers_the_weighed_package():
    # Typed final-package weight wins; without it, per-line gross weights sum.
    typed = render_doc_print_html({**_PRINT_DOC, "gross_weight": "5.2 kg"}, layout="commercial_invoice")
    assert "5.2 kg" in typed
    computed = render_doc_print_html(dict(_PRINT_DOC), layout="commercial_invoice")
    assert "1.5 kg" in computed  # single line: the sum IS the line's gross


def test_missing_customs_data_prints_blank_never_fabricated():
    doc = dict(_PRINT_DOC)
    doc["line_items"] = [{"sku": "W1", "name": "Widget", "quantity": 3}]
    doc.pop("incoterms")
    html = render_doc_print_html(doc, layout="commercial_invoice")
    assert "8471.30" not in html
    assert ">--<" not in html  # blank on paper, not a "--" placeholder


def test_layout_is_only_valid_for_shipping_documents():
    with pytest.raises(ValueError):
        render_doc_print_html({"doc_type": "invoice", "line_items": []}, layout="commercial_invoice")
    with pytest.raises(ValueError):
        render_doc_print_html(dict(_PRINT_DOC), layout="packing_list")


def test_quotation_print_regression_keeps_prices():
    html = render_doc_print_html({
        "doc_type": "list", "list_type": "quotation", "ref_id": "LST-1", "currency": "USD",
        "subtotal": 31.5, "total": 31.5,
        "line_items": [{"sku": "W1", "name": "Widget", "quantity": 3, "unit_price": 10.5}],
    })
    assert "Subtotal" in html and "Unit Price" in html


# ── customs helpers ──────────────────────────────────────────────────────────

def test_customs_backfill_fills_only_gaps():
    line = {"hs_code": "", "country_of_origin": "TH"}
    customs_backfill(line, {"hs_code": "7113.19", "country_of_origin": "US"})
    assert line["hs_code"] == "7113.19"        # filled from the item
    assert line["country_of_origin"] == "TH"   # the line's own value wins


def test_line_gross_weight_multiplies_per_unit_packaged_weight():
    item = {"gross_weight": 1.5, "gross_weight_unit": "kg"}
    assert line_gross_weight({"quantity": 3}, item, qty_is_weight=False) == (4.5, "kg")
    # No catalog gross weight, or weight-sold quantity: blank, never fabricated.
    assert line_gross_weight({"quantity": 3}, {}, qty_is_weight=False) == (None, "")
    assert line_gross_weight({"quantity": 3}, item, qty_is_weight=True) == (None, "")


def test_reason_for_export_covers_the_carrier_set():
    assert set(REASONS_FOR_EXPORT) == {"sale", "sample", "repair_return", "gift",
                                       "personal_effects", "not_for_resale"}


def test_country_picker_list_is_usable():
    assert len(COUNTRIES) > 190
    assert COUNTRIES == tuple(sorted(COUNTRIES))  # datalist stays alphabetical
    for c in ("Thailand", "Germany", "United States", "Vietnam"):
        assert c in COUNTRIES

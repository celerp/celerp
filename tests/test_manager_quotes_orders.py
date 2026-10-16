# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager quotes and orders arrive as Celerp quotations and purchase orders, with their
status, lines, validity and the links to the invoices and bills they became.

Celerp has a quotation and a purchase order. A sales order becomes a quotation and a
purchase quote a draft purchase order, each disclosed. None of them posts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from celerp.importers.adapters.base import MigrationDecisions
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io import specs
from fixtures.manager_io.support import CHECKPOINTS, ORDERS, actual_rows, adapter, artifact, expected_rows, ref
from migration_support import real_engine  # noqa: F401 - fixture
from test_migration_inventory_provenance import _migrated

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
CUTOVER = MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=specs.ORDERS_CUTOVER)
TYPES = {"Q": "SalesQuote", "S": "SalesOrder", "P": "PurchaseOrder", "I": "SalesInvoice", "B": "PurchaseInvoice"}


def _type(label: str) -> str:
    return "PurchaseQuote" if label.startswith("PQ") else TYPES[label[0]]


def _source(tmp_path):
    return specs.build_quotes_orders(tmp_path / "orders.manager")


def _documents(tmp_path, decisions=FULL) -> dict:
    bundle = adapter().build_manifest([artifact(_source(tmp_path))], decisions).bundle
    return {d.source_ref: d for d in bundle.documents}


def test_quotes_and_orders_scan_as_carried(tmp_path):
    """RED before the change: every quote and order is listed as not carried."""
    rows = {r.source_type: r for r in adapter().inspect([artifact(_source(tmp_path))]).coverage}
    assert rows["SalesQuote"].coverage_class == CoverageClass.MAPPED and rows["SalesQuote"].count == 4
    assert rows["PurchaseOrder"].coverage_class == CoverageClass.MAPPED and rows["PurchaseOrder"].count == 2
    assert rows["SalesOrder"].coverage_class == CoverageClass.MAPPED_WITH_LOSS
    assert "quotation" in rows["SalesOrder"].note
    assert rows["PurchaseQuote"].coverage_class == CoverageClass.MAPPED_WITH_LOSS
    assert "draft purchase order" in rows["PurchaseQuote"].note
    assert not [r for r in rows.values() if r.coverage_class in (CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER,
                                                                 CoverageClass.UNCLASSIFIED)]


def test_manager_imports_quotes_and_orders(tmp_path):
    """RED before the change: no quote or order reaches the bundle."""
    docs = _documents(tmp_path)
    kinds = {ref: (d.doc_type.value, d.status) for ref, d in docs.items() if not ref.startswith(("INV", "BILL"))}
    assert kinds == {
        "Q-1": ("quotation", "awaiting_payment"), "Q-X": ("quotation", "void"),
        "Q-2": ("quotation", "awaiting_payment"), "SO-1": ("quotation", "awaiting_payment"),
        "Q-3": ("quotation", "awaiting_payment"), "SO-2": ("quotation", "awaiting_payment"),
        "PQ-1": ("purchase_order", "awaiting_payment"), "PO-1": ("purchase_order", "awaiting_payment"),
        "PO-2": ("purchase_order", "awaiting_payment"),
    }
    q1 = docs["Q-1"]
    assert (q1.issue_date, q1.metadata["valid_until"], q1.total, q1.tax_total) == (
        date(2026, 1, 5), "2026-02-04", D("110"), D("10"))
    assert q1.contact_external_id == ref("CA")
    (line,) = q1.line_items
    assert (line.description, line.quantity, line.unit_price, line.tax_code_external_id, line.total_price) == (
        "Consulting hours", D("2"), D("50"), ref("VAT"), D("100"))
    assert docs["SO-1"].line_items[0].item_external_id == ref("WID")
    po2 = docs["PO-2"].line_items[0]
    assert (po2.discount_percent, po2.total_price, docs["PO-2"].total) == (D("10"), D("36"), D("36"))
    assert docs["SO-2"].total == D("55") and docs["SO-2"].tax_total == D("5")


def test_conversions_link_both_ways(tmp_path):
    """RED before the change: no record names the one it became or came from."""
    docs = _documents(tmp_path)

    def link(label):
        return {"source_type": _type(label), "external_id": ref(label.replace("-", ""))}

    assert docs["Q-2"].metadata["converted_to"] == {**link("SO-1"), "doc_type": "quotation"}
    assert docs["SO-1"].metadata["converted_to"] == {**link("INV-O"), "doc_type": "invoice"}
    assert docs["Q-3"].metadata["converted_to"] == {**link("INV-Q"), "doc_type": "invoice"}
    assert docs["PQ-1"].metadata["converted_to"] == {**link("PO-1"), "doc_type": "purchase_order"}
    assert docs["PO-1"].metadata["converted_to"] == {**link("BILL-O"), "doc_type": "bill"}
    assert docs["INV-O"].metadata["converted_from"] == {**link("SO-1"), "ref": "SO-1"}
    assert docs["INV-Q"].metadata["converted_from"] == {**link("Q-3"), "ref": "Q-3"}
    assert docs["BILL-O"].metadata["converted_from"] == {**link("PO-1"), "ref": "PO-1"}
    for open_record in ("Q-1", "Q-X", "SO-2", "PO-2"):
        assert "converted_to" not in docs[open_record].metadata


def test_cutover_carries_only_open_and_later_quotes_and_orders(tmp_path):
    """RED before the change: no quote or order reaches the bundle."""
    docs = _documents(tmp_path, CUTOVER)
    assert sorted(r for r in docs if not r.startswith(("INV", "BILL"))) == ["PO-2", "Q-1", "SO-2"]


def test_quotes_and_orders_reconcile_by_type_and_status():
    """RED before the change: the expectations carry no quotation or purchase order."""
    manifest = adapter().build_manifest([artifact(ORDERS)], FULL)
    expected = expected_rows(CHECKPOINTS["orders"]["full_history"], "USD")
    assert expected <= actual_rows(manifest.reconciliation_expectations)


async def test_migrated_quotes_and_orders_keep_status_validity_and_links(real_engine, monkeypatch, tmp_path):
    """RED before the change: the migration brings in no quotation or purchase order."""
    books = await _migrated(real_engine, monkeypatch, tmp_path, source=_source(tmp_path))
    from test_migration_e2e import _projections

    docs = await _projections(books.engine, books.run, "doc")

    def doc(label):
        return docs[books.id(_type(label), label.replace("-", ""))]

    assert (doc("Q-1")["doc_type"], doc("Q-1")["status"], doc("Q-1")["valid_until"]) == (
        "quotation", "sent", "2026-02-04")
    assert doc("Q-X")["status"] == "void"
    assert doc("SO-2")["status"] == "sent" and doc("PO-2")["status"] == "sent"
    for label, successor, kind in (("Q-2", "SO-1", "quotation"), ("SO-1", "INV-O", "invoice"),
                                   ("Q-3", "INV-Q", "invoice"), ("PQ-1", "PO-1", "purchase_order"),
                                   ("PO-1", "BILL-O", "bill")):
        assert (doc(label)["status"], doc(label)["converted_to"], doc(label)["converted_to_type"]) == (
            "converted", books.id(_type(successor), successor.replace("-", "")), kind), label
    assert doc("INV-O")["source_quotation_id"] == books.id("SalesOrder", "SO1")
    assert doc("INV-Q")["source_quotation_id"] == books.id("SalesQuote", "Q3")
    assert doc("BILL-O")["source_po_ref"] == "PO-1"
    assert [ln["discount_pct"] for ln in doc("PO-2")["line_items"]] == [10]

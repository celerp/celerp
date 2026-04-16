# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Import reconciliation tests.

Validates that:
1. A migration script produces a CIF manifest matching truth anchors
2. The importer dry-run validates the manifest successfully
3. parse_money() handles all GemCloud money formats correctly
4. Status mapping covers all known GemCloud statuses
5. CIF schema validates required fields and types

Truth anchors are the verified ground-truth figures from the source data.
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

# ── Paths (resolved from env or sibling dirs; never hardcoded) ─────────────────
_BASE = Path(__file__).resolve().parents[2]
MIGRATION_SCRIPT = Path(
    __import__("os").environ.get("CELERP_MIGRATION_SCRIPT", str(_BASE / "migration" / "gemcloud_adapter.py"))
)
CIF_MANIFEST = Path(
    __import__("os").environ.get("CELERP_CIF_MANIFEST", str(_BASE / "migration" / "gemcloud_cif.json"))
)
DATA_DIR = Path(
    __import__("os").environ.get("CELERP_GEMCLOUD_DATA_DIR", str(_BASE / "gemcloud_export" / "ledger_clean"))
)

# These tests rely on a private one-off migration script outside the repo.
# Skip the entire module if it isn't present (standard CI, fresh checkouts).
if not MIGRATION_SCRIPT.exists():
    pytest.skip("gemcloud_adapter.py not found — skipping migration reconciliation tests", allow_module_level=True)

# ── Truth anchors ──────────────────────────────────────────────────────────────
TRUTH_ANCHORS = {
    "item_count": 4416,
    "inventory_cost": Decimal("2559729.75"),
    "inventory_wholesale": Decimal("9798289.57"),
    "inventory_retail": Decimal("19134279.60"),
    # Updated 2026-02-23: full history export (2019 → 2026), 1948 total / 76 void
    "invoice_count_non_void": 1872,
    "ar_gross": Decimal("17020113.29"),
    "ar_paid": Decimal("14601767.76"),
    "ar_outstanding": Decimal("2552663.73"),
    "memo_total": Decimal("594801.60"),
}

MONEY_TOLERANCE = Decimal("1.00")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _approx_decimal(expected: Decimal) -> object:
    """pytest.approx wrapper for Decimal comparisons with $1 tolerance."""
    return pytest.approx(float(expected), abs=float(MONEY_TOLERANCE))


# ── parse_money tests ──────────────────────────────────────────────────────────

class TestParseMoney:
    """Unit tests for the parse_money helper in the migration adapter."""

    @pytest.fixture(scope="class")
    def parse_money(self):
        # Import directly from the migration script (not in repo, import via path)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gemcloud_adapter", str(MIGRATION_SCRIPT)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.parse_money

    def test_formatted_dollar_string(self, parse_money):
        assert parse_money("$1,234.56") == Decimal("1234.56")

    def test_zero_dollar(self, parse_money):
        assert parse_money("$0.00") is None

    def test_none_input(self, parse_money):
        assert parse_money(None) is None

    def test_integer_zero(self, parse_money):
        assert parse_money(0) is None

    def test_plain_numeric_string(self, parse_money):
        assert parse_money("140.24") == Decimal("140.24")

    def test_per_carat_suffix(self, parse_money):
        assert parse_money("$140.24 /ct") == Decimal("140.24")

    def test_numeric_float(self, parse_money):
        assert parse_money(698.38) == Decimal("698.38")

    def test_dash_sentinel(self, parse_money):
        assert parse_money("--") is None

    def test_large_number(self, parse_money):
        assert parse_money("$2,559,729.75") == Decimal("2559729.75")

    def test_negative_value(self, parse_money):
        # Negative values ARE parsed (callers use abs() for totals)
        assert parse_money("$-174.00") == Decimal("-174.00")


# ── Status mapping tests ───────────────────────────────────────────────────────

class TestStatusMapping:
    """Unit tests for GemCloud → CIF status mappings."""

    @pytest.fixture(scope="class")
    def adapter(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gemcloud_adapter", str(MIGRATION_SCRIPT)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_item_available(self, adapter):
        assert adapter.map_item_status("Available") == "available"

    def test_item_memo_out(self, adapter):
        assert adapter.map_item_status("Memo out") == "memo_out"

    def test_item_production(self, adapter):
        assert adapter.map_item_status("Production") == "production"

    def test_item_numeric_sold(self, adapter):
        assert adapter.map_item_status("4") == "sold"

    def test_item_numeric_memo_out(self, adapter):
        assert adapter.map_item_status("5") == "memo_out"

    def test_item_numeric_production(self, adapter):
        assert adapter.map_item_status("6") == "production"

    def test_invoice_void_flag(self, adapter):
        assert adapter.map_invoice_status({"is_void": 1, "status": "Awaiting payment"}) == "void"

    def test_invoice_paid(self, adapter):
        assert adapter.map_invoice_status({"is_void": 0, "status": "Paid", "is_payment_recieved": "1"}) == "paid"

    def test_invoice_awaiting(self, adapter):
        assert adapter.map_invoice_status({"is_void": 0, "status": "Awaiting payment", "is_payment_recieved": 0}) == "awaiting_payment"

    def test_memo_out(self, adapter):
        assert adapter.map_memo_status("Memo out") == "out"

    def test_memo_returned(self, adapter):
        assert adapter.map_memo_status("Returned to stock") == "returned"

    def test_memo_invoiced(self, adapter):
        assert adapter.map_memo_status("Invoiced") == "invoiced"

    def test_memo_draft(self, adapter):
        assert adapter.map_memo_status("Draft") == "draft"


# ── CIF schema validation tests ────────────────────────────────────────────────

class TestCIFSchema:
    """Unit tests for the CIF Pydantic schema."""

    def test_cif_item_required_fields(self):
        from celerp.importers.schema import CIFItem
        item = CIFItem(
            external_id="gc:123",
            name="Test Stone",
            status="available",
        )
        assert item.external_id == "gc:123"
        assert item.name == "Test Stone"
        assert item.status == "available"
        assert item.total_cost is None

    def test_cif_item_decimal_money(self):
        from celerp.importers.schema import CIFItem
        item = CIFItem(
            external_id="gc:123",
            name="Test",
            status="available",
            total_cost=Decimal("1234.56"),
            wholesale_price=Decimal("9000.00"),
        )
        assert isinstance(item.total_cost, Decimal)
        assert item.total_cost == Decimal("1234.56")

    def test_cif_document_status_literal(self):
        from celerp.importers.schema import CIFDocument
        doc = CIFDocument(
            external_id="gc:invoice:1",
            doc_type="invoice",
            status="paid",
            total=Decimal("5000"),
            amount_paid=Decimal("5000"),
            amount_outstanding=Decimal("0"),
        )
        assert doc.status == "paid"

    def test_cif_document_invalid_status(self):
        from celerp.importers.schema import CIFDocument
        with pytest.raises(Exception):
            CIFDocument(
                external_id="gc:invoice:1",
                doc_type="invoice",
                status="invalid_status",  # not in Literal
                total=Decimal("0"),
                amount_paid=Decimal("0"),
                amount_outstanding=Decimal("0"),
            )

    def test_cif_memo_status_literal(self):
        from celerp.importers.schema import CIFMemo
        memo = CIFMemo(external_id="gc:memo:1", status="out", total=Decimal("1000"))
        assert memo.status == "out"

    def test_cif_import_manifest_structure(self):
        from datetime import datetime, timezone
        from celerp.importers.schema import CIFImportBundle, CIFImportManifest
        manifest = CIFImportManifest(
            source="test",
            exported_at=datetime.now(timezone.utc),
            bundle=CIFImportBundle(),
            stats={"item_count": 0},
        )
        assert manifest.source == "test"
        assert manifest.bundle.items == []

    def test_cif_line_item(self):
        from celerp.importers.schema import CIFLineItem
        li = CIFLineItem(
            item_external_id="gc:123",
            quantity=Decimal("1"),
            unit_price=Decimal("3000"),
            total_price=Decimal("3000"),
            weight=Decimal("1.70"),
            weight_unit="ct",
            cost_basis=Decimal("3060"),
        )
        assert li.cost_basis == Decimal("3060")


# ── Migration script integration tests ────────────────────────────────────────

class TestMigrationOutput:
    """
    Integration tests that load the pre-generated CIF manifest and assert
    truth anchors. The migration script must have been run before these tests.

    If the manifest doesn't exist, the test is skipped (CI-friendly).
    """

    @pytest.fixture(scope="class")
    def manifest(self):
        if not CIF_MANIFEST.exists():
            pytest.skip(f"CIF manifest not found at {CIF_MANIFEST}. Run gemcloud_adapter.py first.")
        from celerp.importers.schema import CIFImportManifest
        raw = json.loads(CIF_MANIFEST.read_text(encoding="utf-8"))
        return CIFImportManifest.model_validate(raw)

    def test_item_count(self, manifest):
        assert len(manifest.bundle.items) == TRUTH_ANCHORS["item_count"]

    def test_inventory_cost(self, manifest):
        total = sum(
            (i.total_cost for i in manifest.bundle.items if i.total_cost), Decimal("0")
        )
        assert float(total) == _approx_decimal(TRUTH_ANCHORS["inventory_cost"])

    def test_inventory_wholesale(self, manifest):
        total = sum(
            (i.wholesale_price for i in manifest.bundle.items if i.wholesale_price), Decimal("0")
        )
        assert float(total) == _approx_decimal(TRUTH_ANCHORS["inventory_wholesale"])

    def test_inventory_retail(self, manifest):
        total = sum(
            (i.retail_price for i in manifest.bundle.items if i.retail_price), Decimal("0")
        )
        assert float(total) == _approx_decimal(TRUTH_ANCHORS["inventory_retail"])

    def test_invoice_count_non_void(self, manifest):
        non_void = [d for d in manifest.bundle.documents if d.status != "void"]
        assert len(non_void) == TRUTH_ANCHORS["invoice_count_non_void"]

    def test_ar_gross(self, manifest):
        non_void = [d for d in manifest.bundle.documents if d.status != "void"]
        total = sum((d.total for d in non_void), Decimal("0"))
        assert float(total) == _approx_decimal(TRUTH_ANCHORS["ar_gross"])

    def test_ar_paid(self, manifest):
        non_void = [d for d in manifest.bundle.documents if d.status != "void"]
        total = sum((d.amount_paid for d in non_void), Decimal("0"))
        assert float(total) == _approx_decimal(TRUTH_ANCHORS["ar_paid"])

    def test_ar_outstanding(self, manifest):
        non_void = [d for d in manifest.bundle.documents if d.status != "void"]
        total = sum((d.amount_outstanding for d in non_void), Decimal("0"))
        assert float(total) == _approx_decimal(TRUTH_ANCHORS["ar_outstanding"])

    def test_memo_total(self, manifest):
        total = sum((m.total for m in manifest.bundle.memos), Decimal("0"))
        assert float(total) == _approx_decimal(TRUTH_ANCHORS["memo_total"])

    def test_manifest_stats_match_bundle(self, manifest):
        """Stats dict in manifest must match computed bundle values."""
        stats = manifest.stats
        assert int(stats["item_count"]) == len(manifest.bundle.items)
        assert int(stats["invoice_count_non_void"]) == sum(
            1 for d in manifest.bundle.documents if d.status != "void"
        )

    def test_all_items_have_external_id(self, manifest):
        for item in manifest.bundle.items:
            assert item.external_id.startswith("gc:")

    def test_all_docs_have_valid_status(self, manifest):
        valid_statuses = {"draft", "awaiting_payment", "paid", "void"}
        for doc in manifest.bundle.documents:
            assert doc.status in valid_statuses

    def test_all_memos_have_valid_status(self, manifest):
        valid_statuses = {"draft", "out", "returned", "invoiced"}
        for memo in manifest.bundle.memos:
            assert memo.status in valid_statuses


# ── Importer dry-run test ──────────────────────────────────────────────────────

class TestImporterDryRun:
    """Test that the importer dry-run validates the manifest without error."""

    def test_dry_run_validates_manifest(self):
        if not CIF_MANIFEST.exists():
            pytest.skip(f"CIF manifest not found at {CIF_MANIFEST}. Run gemcloud_adapter.py first.")

        from celerp.importers.schema import CIFImportManifest
        from celerp.importers.importer import BundleImporter
        import asyncio

        raw = json.loads(CIF_MANIFEST.read_text(encoding="utf-8"))
        manifest = CIFImportManifest.model_validate(raw)

        importer = BundleImporter(
            api_base="http://localhost:8000",
            token="dryrun",
            dry_run=True,
        )
        result = asyncio.run(importer.run(manifest))
        # Dry run: no records are counted as total/imported/failed
        assert result.failed == 0

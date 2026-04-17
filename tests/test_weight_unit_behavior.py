# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Thorough unit tests for weight/weight_unit/cost_per_unit universal fields.

Context: In the CIF schema refactor (2026-03-08), gemstone-specific fields
were removed and replaced with universal ones:
  - weight_ct  → weight (Decimal) + weight_unit (str, e.g. "ct")
  - cost_per_ct → cost_per_unit (generic cost per unit of weight)

These tests verify:
1. Old _ct fields are gone from CIFItem and CIFLineItem schemas
2. New universal fields work correctly across all weight unit types
3. weight_unit is independent of weight (can be None when weight is None)
4. cost_per_unit is independent of weight_unit
5. Importer serialises weight/weight_unit/cost_per_unit correctly into HTTP payloads
6. gemcloud_adapter.py correctly maps GemCloud fields to the new schema
7. ItemCreate router model carries weight_unit and sell_by
8. Round-trip: CIFItem → manifest JSON → re-parse preserves all fields
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest


def _run_coroutine(coro):
    """Run *coro* safely regardless of whether a pytest-asyncio loop is running.

    asyncio.run() raises RuntimeError when called from inside a running event
    loop (e.g. after test_visual.py which leaves Playwright's loop alive).
    We always spin up a private, isolated loop to avoid that coupling.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

from celerp.importers.schema import (
    CIF_VERSION,
    CIFImportBundle,
    CIFImportManifest,
    CIFItem,
    CIFLineItem,
)
from celerp.importers.importer import BundleImporter


# ─────────────────────────────────────────────────────────────────────────────
# 1. Schema field existence — old fields must be absent
# ─────────────────────────────────────────────────────────────────────────────

class TestRemovedFields:
    """Verify that gemstone-specific _ct fields no longer exist on the schema."""

    def test_cif_item_no_weight_ct(self) -> None:
        assert not hasattr(CIFItem.model_fields.get("weight_ct", None), "default"), \
            "weight_ct should not be a field on CIFItem"
        assert "weight_ct" not in CIFItem.model_fields

    def test_cif_item_no_cost_per_ct(self) -> None:
        assert "cost_per_ct" not in CIFItem.model_fields

    def test_cif_line_item_no_weight_ct(self) -> None:
        assert "weight_ct" not in CIFLineItem.model_fields

    def test_cif_item_old_weight_ct_kwarg_is_silently_dropped(self) -> None:
        """Pydantic default config ignores extra kwargs — old weight_ct is dropped, not stored."""
        item = CIFItem(
            external_id="i:1",
            name="Stone",
            status="available",
            weight_ct=Decimal("1.5"),  # extra kwarg — silently dropped
        )
        # The old name is NOT stored anywhere
        assert not hasattr(item, "weight_ct")
        assert item.weight is None  # weight was NOT populated from weight_ct

    def test_cif_line_item_old_weight_ct_kwarg_is_silently_dropped(self) -> None:
        li = CIFLineItem(
            item_external_id="i:1",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            total_price=Decimal("100"),
            weight_ct=Decimal("1.5"),  # extra kwarg — silently dropped
        )
        assert not hasattr(li, "weight_ct")
        assert li.weight is None  # weight was NOT populated from weight_ct


# ─────────────────────────────────────────────────────────────────────────────
# 2. New universal fields exist and accept all weight unit types
# ─────────────────────────────────────────────────────────────────────────────

class TestWeightUnitField:
    """weight_unit accepts any string and is independent of other fields."""

    @pytest.mark.parametrize("unit", ["ct", "kg", "g", "oz", "lb", "t", "tola", "baht"])
    def test_cif_item_accepts_any_weight_unit(self, unit: str) -> None:
        item = CIFItem(
            external_id="i:1",
            name="Widget",
            status="available",
            weight=Decimal("10.0"),
            weight_unit=unit,
        )
        assert item.weight_unit == unit

    def test_cif_item_weight_unit_none_when_no_weight(self) -> None:
        item = CIFItem(external_id="i:1", name="Stone", status="available")
        assert item.weight is None
        assert item.weight_unit is None

    def test_cif_item_weight_without_unit_is_valid(self) -> None:
        """weight can be set without weight_unit — some sources don't record unit."""
        item = CIFItem(
            external_id="i:1",
            name="Stone",
            status="available",
            weight=Decimal("5.0"),
        )
        assert item.weight == Decimal("5.0")
        assert item.weight_unit is None

    def test_cif_item_unit_without_weight_is_valid(self) -> None:
        """weight_unit can theoretically be set without weight (edge case, not rejected)."""
        item = CIFItem(
            external_id="i:1",
            name="Stone",
            status="available",
            weight_unit="ct",
        )
        assert item.weight is None
        assert item.weight_unit == "ct"

    @pytest.mark.parametrize("unit", ["ct", "kg", "g", "oz"])
    def test_cif_line_item_weight_unit(self, unit: str) -> None:
        li = CIFLineItem(
            item_external_id="i:1",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            total_price=Decimal("100"),
            weight=Decimal("2.5"),
            weight_unit=unit,
        )
        assert li.weight_unit == unit

    def test_cif_line_item_no_weight_defaults_none(self) -> None:
        li = CIFLineItem(
            item_external_id="i:1",
            quantity=Decimal("1"),
            unit_price=Decimal("50"),
            total_price=Decimal("50"),
        )
        assert li.weight is None
        assert li.weight_unit is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. cost_per_unit field
# ─────────────────────────────────────────────────────────────────────────────

class TestCostPerUnit:
    """cost_per_unit is generic — not tied to carats or any specific unit."""

    def test_cif_item_cost_per_unit_ct(self) -> None:
        item = CIFItem(
            external_id="i:1",
            name="Ruby",
            status="available",
            weight=Decimal("1.5"),
            weight_unit="ct",
            cost_per_unit=Decimal("200.00"),
        )
        assert item.cost_per_unit == Decimal("200.00")

    def test_cif_item_cost_per_unit_kg(self) -> None:
        item = CIFItem(
            external_id="i:1",
            name="Gold bar",
            status="available",
            weight=Decimal("1.0"),
            weight_unit="kg",
            cost_per_unit=Decimal("50000.00"),
        )
        assert item.cost_per_unit == Decimal("50000.00")

    def test_cif_item_cost_per_unit_none_by_default(self) -> None:
        item = CIFItem(external_id="i:1", name="Stone", status="available")
        assert item.cost_per_unit is None

    def test_cif_item_cost_per_unit_without_weight(self) -> None:
        """cost_per_unit is independent — no validation coupling to weight."""
        item = CIFItem(
            external_id="i:1",
            name="Thing",
            status="available",
            cost_per_unit=Decimal("99.99"),
        )
        assert item.cost_per_unit == Decimal("99.99")
        assert item.weight is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. sell_by field on CIFItem
# ─────────────────────────────────────────────────────────────────────────────

class TestSellBy:
    """sell_by distinguishes 'piece' (fixed price per item) vs 'weight' (price per gram/ct)."""

    @pytest.mark.parametrize("sell_by", ["piece", "weight"])
    def test_cif_item_sell_by_valid_values(self, sell_by: str) -> None:
        item = CIFItem(
            external_id="i:1",
            name="Stone",
            status="available",
            sell_by=sell_by,
        )
        assert item.sell_by == sell_by

    def test_cif_item_sell_by_none_by_default(self) -> None:
        item = CIFItem(external_id="i:1", name="Stone", status="available")
        assert item.sell_by is None

    def test_cif_item_sell_by_weight_requires_no_weight(self) -> None:
        """sell_by='weight' does NOT require weight to be set (it's just metadata)."""
        item = CIFItem(
            external_id="i:1",
            name="Bulk lot",
            status="available",
            sell_by="weight",
        )
        assert item.sell_by == "weight"
        assert item.weight is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Importer payload serialisation
# ─────────────────────────────────────────────────────────────────────────────

class TestImporterPayloadSerialisation:
    """Verify the BundleImporter builds correct HTTP payloads for items and line items."""

    def _run_import(self, manifest: CIFImportManifest) -> list[dict]:
        """Run importer with patched _post_batch; return all calls' payloads."""
        from unittest.mock import AsyncMock, patch
        captured: list[dict] = []

        async def _fake_post(client, endpoint, records):
            captured.extend(records)
            return {"created": len(records), "skipped": 0, "errors": []}

        importer = BundleImporter(api_base="http://localhost", token="tok")
        with patch.object(importer, "_post_batch", new=AsyncMock(side_effect=_fake_post)):
            _run_coroutine(importer.run(manifest))
        return captured

    def _make_manifest(self, items=None, documents=None) -> CIFImportManifest:
        return CIFImportManifest(
            source="test",
            exported_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            bundle=CIFImportBundle(
                items=items or [],
                documents=documents or [],
            ),
        )

    def test_item_payload_includes_weight_and_unit(self) -> None:
        manifest = self._make_manifest(items=[
            CIFItem(
                external_id="i:1",
                name="Sapphire",
                status="available",
                weight=Decimal("2.35"),
                weight_unit="ct",
            )
        ])
        payloads = self._run_import(manifest)
        item_payload = next(p for p in payloads if p.get("event_type") == "item.snapshot")
        assert item_payload["data"]["weight"] == "2.35"
        assert item_payload["data"]["weight_unit"] == "ct"

    def test_item_payload_includes_cost_per_unit(self) -> None:
        manifest = self._make_manifest(items=[
            CIFItem(
                external_id="i:1",
                name="Diamond",
                status="available",
                weight=Decimal("1.0"),
                weight_unit="ct",
                cost_per_unit=Decimal("5000.00"),
            )
        ])
        payloads = self._run_import(manifest)
        item_payload = next(p for p in payloads if p.get("event_type") == "item.snapshot")
        assert item_payload["data"]["cost_per_unit"] == "5000.00"

    def test_item_payload_weight_none_serialised_as_none(self) -> None:
        manifest = self._make_manifest(items=[
            CIFItem(external_id="i:1", name="Stone", status="available")
        ])
        payloads = self._run_import(manifest)
        item_payload = next(p for p in payloads if p.get("event_type") == "item.snapshot")
        assert item_payload["data"]["weight"] is None
        assert item_payload["data"]["weight_unit"] is None
        assert item_payload["data"]["cost_per_unit"] is None

    def test_item_payload_sell_by_included(self) -> None:
        manifest = self._make_manifest(items=[
            CIFItem(
                external_id="i:1",
                name="Stone",
                status="available",
                sell_by="piece",
            )
        ])
        payloads = self._run_import(manifest)
        item_payload = next(p for p in payloads if p.get("event_type") == "item.snapshot")
        assert item_payload["data"]["sell_by"] == "piece"

    def test_item_payload_no_legacy_ct_fields(self) -> None:
        """HTTP payload must NOT contain weight_ct or cost_per_ct."""
        manifest = self._make_manifest(items=[
            CIFItem(
                external_id="i:1",
                name="Stone",
                status="available",
                weight=Decimal("1.5"),
                weight_unit="ct",
                cost_per_unit=Decimal("300.00"),
            )
        ])
        payloads = self._run_import(manifest)
        item_payload = next(p for p in payloads if p.get("event_type") == "item.snapshot")
        assert "weight_ct" not in item_payload["data"]
        assert "cost_per_ct" not in item_payload["data"]

    def test_line_item_payload_weight_unit(self) -> None:
        from celerp.importers.schema import CIFDocument, CIFLineItem
        li = CIFLineItem(
            item_external_id="i:1",
            quantity=Decimal("1"),
            weight=Decimal("1.80"),
            weight_unit="ct",
            unit_price=Decimal("4000"),
            total_price=Decimal("4000"),
            cost_basis=Decimal("1200"),
        )
        doc = CIFDocument(
            external_id="d:1",
            doc_type="invoice",
            status="paid",
            total=Decimal("4000"),
            amount_paid=Decimal("4000"),
            amount_outstanding=Decimal("0"),
            line_items=[li],
        )
        manifest = self._make_manifest(documents=[doc])
        payloads = self._run_import(manifest)
        doc_payload = next(p for p in payloads if p.get("event_type") == "doc.created")
        li_data = doc_payload["data"]["line_items"][0]
        assert li_data["weight"] == "1.80"
        assert li_data["weight_unit"] == "ct"
        assert li_data["cost_basis"] == "1200"

    def test_line_item_payload_no_weight_ct_field(self) -> None:
        from celerp.importers.schema import CIFDocument, CIFLineItem
        li = CIFLineItem(
            item_external_id="i:1",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            total_price=Decimal("100"),
        )
        doc = CIFDocument(
            external_id="d:1",
            doc_type="invoice",
            status="paid",
            total=Decimal("100"),
            amount_paid=Decimal("100"),
            amount_outstanding=Decimal("0"),
            line_items=[li],
        )
        manifest = self._make_manifest(documents=[doc])
        payloads = self._run_import(manifest)
        doc_payload = next(p for p in payloads if p.get("event_type") == "doc.created")
        li_data = doc_payload["data"]["line_items"][0]
        assert "weight_ct" not in li_data
        assert li_data["weight"] is None
        assert li_data["weight_unit"] is None

    def test_item_payload_kg_weight_unit(self) -> None:
        """weight_unit is passed through verbatim — not restricted to 'ct'."""
        manifest = self._make_manifest(items=[
            CIFItem(
                external_id="i:1",
                name="Grain lot",
                status="available",
                weight=Decimal("500.0"),
                weight_unit="kg",
                cost_per_unit=Decimal("12.50"),
            )
        ])
        payloads = self._run_import(manifest)
        item_payload = next(p for p in payloads if p.get("event_type") == "item.snapshot")
        assert item_payload["data"]["weight_unit"] == "kg"
        assert item_payload["data"]["cost_per_unit"] == "12.50"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Round-trip: CIFItem → JSON → re-parse
# ─────────────────────────────────────────────────────────────────────────────

class TestRoundTrip:
    """Full round-trip through manifest serialisation and re-parsing."""

    def test_weight_and_unit_survive_json_roundtrip(self) -> None:
        manifest = CIFImportManifest(
            source="roundtrip",
            exported_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            bundle=CIFImportBundle(
                items=[
                    CIFItem(
                        external_id="i:1",
                        name="Emerald",
                        status="available",
                        weight=Decimal("3.14"),
                        weight_unit="ct",
                        cost_per_unit=Decimal("1500.00"),
                        sell_by="piece",
                    )
                ]
            ),
        )
        raw = json.loads(manifest.model_dump_json())
        restored = CIFImportManifest.model_validate(raw)
        item = restored.bundle.items[0]
        assert item.weight == Decimal("3.14")
        assert item.weight_unit == "ct"
        assert item.cost_per_unit == Decimal("1500.00")
        assert item.sell_by == "piece"

    def test_line_item_weight_unit_survives_roundtrip(self) -> None:
        from celerp.importers.schema import CIFDocument, CIFLineItem
        li = CIFLineItem(
            item_external_id="i:1",
            quantity=Decimal("2"),
            weight=Decimal("4.20"),
            weight_unit="g",
            unit_price=Decimal("200"),
            total_price=Decimal("400"),
        )
        doc = CIFDocument(
            external_id="d:1",
            doc_type="invoice",
            status="paid",
            total=Decimal("400"),
            amount_paid=Decimal("400"),
            amount_outstanding=Decimal("0"),
            line_items=[li],
        )
        manifest = CIFImportManifest(
            source="rt",
            exported_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            bundle=CIFImportBundle(documents=[doc]),
        )
        raw = json.loads(manifest.model_dump_json())
        restored = CIFImportManifest.model_validate(raw)
        rli = restored.bundle.documents[0].line_items[0]
        assert rli.weight == Decimal("4.20")
        assert rli.weight_unit == "g"

    def test_null_weight_unit_survives_roundtrip(self) -> None:
        item = CIFItem(external_id="i:1", name="Thing", status="available")
        raw = json.loads(item.model_dump_json())
        restored = CIFItem.model_validate(raw)
        assert restored.weight is None
        assert restored.weight_unit is None
        assert restored.cost_per_unit is None
        assert restored.sell_by is None


# ─────────────────────────────────────────────────────────────────────────────
# 7. gemcloud_adapter helpers (importable functions, no JSONL files needed)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def adapter():
    """Import gemcloud_adapter.py from the migration directory."""
    import importlib.util, sys
    from pathlib import Path
    adapter_path = Path(__file__).resolve().parents[1] / "context" / "migration" / "gemcloud_adapter.py"
    if not adapter_path.exists():
        pytest.skip("gemcloud_adapter.py not in repo — skipping adapter tests")
    spec = importlib.util.spec_from_file_location("gemcloud_adapter", str(adapter_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGemCloudAdapterWeightFields:
    """adapt_item should map GemCloud weight to weight/weight_unit='ct', not weight_ct."""

    def _gc_item_event(self, **overrides) -> dict:
        data = {
            "id": "999",
            "product_title": "Test Stone",
            "status": "available",
            "weight": None,
            "cost_per_carat": None,
            "total_cost": None,
            "total_selling_price": None,
            "retail_price": None,
            "calibrated": "",
        }
        data.update(overrides)
        return {"data": data, "idempotency_key": "key:999"}

    def test_adapt_item_weight_ct_maps_to_weight_plus_unit(self, adapter) -> None:
        event = self._gc_item_event(weight="2.50ct")
        item = adapter.adapt_item(event)
        assert item.weight == Decimal("2.50")
        assert item.weight_unit == "ct"

    def test_adapt_item_no_weight_gives_none_unit(self, adapter) -> None:
        event = self._gc_item_event(weight=None)
        item = adapter.adapt_item(event)
        assert item.weight is None
        assert item.weight_unit is None

    def test_adapt_item_cost_per_carat_maps_to_cost_per_unit(self, adapter) -> None:
        event = self._gc_item_event(weight="1.0", cost_per_carat="$300.00")
        item = adapter.adapt_item(event)
        assert item.cost_per_unit == Decimal("300.00")

    def test_adapt_item_no_cost_per_carat_gives_none(self, adapter) -> None:
        event = self._gc_item_event(cost_per_carat=None)
        item = adapter.adapt_item(event)
        assert item.cost_per_unit is None

    def test_adapt_item_no_weight_ct_field_on_result(self, adapter) -> None:
        event = self._gc_item_event(weight="1.5")
        item = adapter.adapt_item(event)
        assert not hasattr(item, "weight_ct"), "weight_ct must not exist on CIFItem"
        assert not hasattr(item, "cost_per_ct"), "cost_per_ct must not exist on CIFItem"

    def test_adapt_item_attributes_contain_stone_fields_not_weight(self, adapter) -> None:
        """Stone-type fields go into attributes; weight stays top-level."""
        event = self._gc_item_event(
            weight="3.0",
            **{"stone_types": {"name": "Ruby"}, "stone_type_colors": {"name": "Red"}},
        )
        item = adapter.adapt_item(event)
        assert item.weight == Decimal("3.0")
        assert item.weight_unit == "ct"
        assert item.attributes.get("stone_type") == "Ruby"
        # weight is NOT in attributes
        assert "weight" not in item.attributes
        assert "weight_ct" not in item.attributes

    def test_adapt_line_item_weight_maps_correctly(self, adapter) -> None:
        """adapt_invoice produces CIFLineItems with weight/weight_unit, not weight_ct."""
        invoice_event = {
            "data": {
                "id": "inv:1",
                "customer_id": "42",
                "status": "Paid",
                "is_void": 0,
                "is_payment_recieved": "1",
                "converted_price": "$5000.00",
                "payment_remaining": "$0.00",
                "stone_order_invoice_products": [
                    {
                        "stone_detail_id": "100",
                        "weight": "2.15ct",
                        "unit_price": "$5000.00",
                        "total_selling_price": "$5000.00",
                        "stone": {"total_cost": "$2000.00"},
                    }
                ],
            },
            "idempotency_key": "key:inv:1",
        }
        doc = adapter.adapt_invoice(invoice_event)
        assert len(doc.line_items) == 1
        li = doc.line_items[0]
        assert li.weight == Decimal("2.15")
        assert li.weight_unit == "ct"
        assert not hasattr(li, "weight_ct"), "weight_ct must not exist on CIFLineItem"


# ─────────────────────────────────────────────────────────────────────────────
# 8. ItemCreate router model — weight_unit and sell_by are universal
# ─────────────────────────────────────────────────────────────────────────────

class TestItemCreateModel:
    """ItemCreate in routers/items.py: sell_by required, no weight/weight_unit top-level."""

    def test_item_create_has_sell_by_field(self) -> None:
        from celerp_inventory.routes import ItemCreate
        assert "sell_by" in ItemCreate.model_fields

    def test_item_create_has_attributes_field(self) -> None:
        from celerp_inventory.routes import ItemCreate
        assert "attributes" in ItemCreate.model_fields

    def test_item_create_no_weight_top_level(self) -> None:
        """weight and weight_unit removed from ItemCreate (Phase 2 refactor)."""
        from celerp_inventory.routes import ItemCreate
        assert "weight" not in ItemCreate.model_fields
        assert "weight_unit" not in ItemCreate.model_fields

    def test_item_create_no_stone_fields(self) -> None:
        """Stone-specific fields must NOT be top-level on ItemCreate (L-075)."""
        from celerp_inventory.routes import ItemCreate
        for forbidden in ("stone_type", "stone_color", "stone_shape", "stone_treatment",
                          "stone_origin", "weight_ct", "color_grade", "clarity_grade",
                          "cut_grade", "cost_per_ct"):
            assert forbidden not in ItemCreate.model_fields, \
                f"Field '{forbidden}' is industry-specific and must not be top-level"

    def test_item_create_sell_by_required(self) -> None:
        """sell_by is now required (no default)."""
        import pydantic
        from celerp_inventory.routes import ItemCreate
        with pytest.raises(pydantic.ValidationError):
            ItemCreate(sku="S001", name="Widget")

    def test_item_create_with_sell_by(self) -> None:
        from celerp_inventory.routes import ItemCreate
        item = ItemCreate(sku="S001", name="Widget", sell_by="piece")
        assert item.sell_by == "piece"

    def test_item_create_stone_data_in_attributes(self) -> None:
        """Stone metadata should be stored in attributes dict, not top-level."""
        from celerp_inventory.routes import ItemCreate
        item = ItemCreate(
            sku="S001",
            name="Ruby",
            sell_by="carat",
            attributes={
                "stone_type": "ruby",
                "color_grade": "AAA",
                "clarity_grade": "VS1",
            },
        )
        assert item.attributes["stone_type"] == "ruby"
        assert item.attributes["color_grade"] == "AAA"


# ─────────────────────────────────────────────────────────────────────────────
# 9. created_at / updated_at fields
# ─────────────────────────────────────────────────────────────────────────────

class TestTimestampFields:
    """created_at and updated_at are universal fields preserved through the full pipeline."""

    def test_cif_item_has_updated_at_field(self) -> None:
        assert "updated_at" in CIFItem.model_fields

    def test_cif_item_updated_at_none_by_default(self) -> None:
        item = CIFItem(external_id="i:1", name="Stone", status="available")
        assert item.updated_at is None

    def test_cif_item_updated_at_set(self) -> None:
        from datetime import datetime, timezone
        dt = datetime(2026, 2, 19, 15, 59, 22, tzinfo=timezone.utc)
        item = CIFItem(
            external_id="i:1",
            name="Stone",
            status="available",
            created_at=datetime(2019, 11, 14, tzinfo=timezone.utc),
            updated_at=dt,
        )
        assert item.updated_at == dt

    def test_cif_item_timestamps_survive_roundtrip(self) -> None:
        from datetime import datetime, timezone
        item = CIFItem(
            external_id="i:1",
            name="Stone",
            status="available",
            created_at=datetime(2019, 11, 14, tzinfo=timezone.utc),
            updated_at=datetime(2026, 2, 19, 15, 59, 22, tzinfo=timezone.utc),
        )
        raw = json.loads(item.model_dump_json())
        restored = CIFItem.model_validate(raw)
        assert restored.created_at == item.created_at
        assert restored.updated_at == item.updated_at

    def test_item_snapshot_schema_has_timestamp_fields(self) -> None:
        from celerp.events.schemas import ItemSnapshot
        assert "created_at" in ItemSnapshot.model_fields
        assert "updated_at" in ItemSnapshot.model_fields

    def test_importer_payload_includes_updated_at(self) -> None:
        from unittest.mock import AsyncMock, patch
        from datetime import datetime, timezone

        manifest = CIFImportManifest(
            source="test",
            exported_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            bundle=CIFImportBundle(items=[
                CIFItem(
                    external_id="i:1",
                    name="Stone",
                    status="available",
                    created_at=datetime(2019, 11, 14, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 2, 19, 15, 59, 22, tzinfo=timezone.utc),
                )
            ]),
        )
        captured: list[dict] = []

        async def _fake_post(client, endpoint, records):
            captured.extend(records)
            return {"created": len(records), "skipped": 0, "errors": []}

        importer = BundleImporter(api_base="http://localhost", token="tok")
        with patch.object(importer, "_post_batch", new=AsyncMock(side_effect=_fake_post)):
            _run_coroutine(importer.run(manifest))

        payload = next(p for p in captured if p.get("event_type") == "item.snapshot")
        assert payload["data"]["updated_at"] == "2026-02-19T15:59:22+00:00"
        assert payload["data"]["created_at"] == "2019-11-14T00:00:00+00:00"

    def test_importer_payload_updated_at_none_when_absent(self) -> None:
        from unittest.mock import AsyncMock, patch
        from datetime import datetime, timezone

        manifest = CIFImportManifest(
            source="test",
            exported_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            bundle=CIFImportBundle(items=[
                CIFItem(external_id="i:1", name="Stone", status="available")
            ]),
        )
        captured: list[dict] = []

        async def _fake_post(client, endpoint, records):
            captured.extend(records)
            return {"created": len(records), "skipped": 0, "errors": []}

        importer = BundleImporter(api_base="http://localhost", token="tok")
        with patch.object(importer, "_post_batch", new=AsyncMock(side_effect=_fake_post)):
            _run_coroutine(importer.run(manifest))

        payload = next(p for p in captured if p.get("event_type") == "item.snapshot")
        assert payload["data"]["updated_at"] is None
        assert payload["data"]["created_at"] is None

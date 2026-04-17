# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Unit tests for celerp.importers.schema and celerp.importers.importer (non-HTTP logic)."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from celerp.importers.schema import (
    CIF_VERSION,
    CIFBatch,
    CIFContact,
    CIFDocument,
    CIFEntityType,
    CIFImportBundle,
    CIFImportManifest,
    CIFItem,
    CIFLineItem,
    CIFMemo,
)
from celerp.importers.importer import (
    MAX_BATCH_SIZE,
    BundleImporter,
    EntityStats,
    ImportResult,
    load_manifest,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _minimal_manifest() -> dict:
    return {
        "cif_version": CIF_VERSION,
        "source": "test",
        "exported_at": "2026-01-01T00:00:00",
        "bundle": {
            "items": [],
            "contacts": [],
            "documents": [],
            "memos": [],
        },
        "stats": {},
    }


def _make_item() -> CIFItem:
    return CIFItem(
        external_id="item:001",
        name="Test Stone",
        status="available",
    )


def _make_contact() -> CIFContact:
    return CIFContact(external_id="c:001", name="Alice")


def _make_line_item() -> CIFLineItem:
    return CIFLineItem(
        item_external_id="item:001",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        total_price=Decimal("100.00"),
    )


def _make_document() -> CIFDocument:
    return CIFDocument(
        external_id="doc:001",
        doc_type="invoice",
        status="paid",
        total=Decimal("100.00"),
        amount_paid=Decimal("100.00"),
        amount_outstanding=Decimal("0.00"),
        line_items=[_make_line_item()],
    )


def _make_memo() -> CIFMemo:
    return CIFMemo(
        external_id="memo:001",
        status="out",
        total=Decimal("50.00"),
    )


# ── CIFRecord ─────────────────────────────────────────────────────────────────


def test_cif_record_valid() -> None:
    from celerp.importers.schema import CIFRecord
    record = CIFRecord(
        entity_id="item:001",
        entity_type=CIFEntityType.ITEM,
        event_type="item.snapshot",
        data={"name": "Stone"},
        source="import:test",
        idempotency_key="test:item:001",
    )
    assert record.entity_id == "item:001"
    assert record.event_type == "item.snapshot"


def test_cif_record_unknown_event_type() -> None:
    from celerp.importers.schema import CIFRecord
    with pytest.raises(Exception, match="Unknown event_type"):
        CIFRecord(
            entity_id="item:001",
            entity_type=CIFEntityType.ITEM,
            event_type="item.does_not_exist_xyzzy",
            data={},
            source="import:test",
            idempotency_key="key",
        )


def test_cif_record_entity_type_mismatch() -> None:
    from celerp.importers.schema import CIFRecord
    # CONTACT entity with item.* event should raise
    with pytest.raises(Exception):
        CIFRecord(
            entity_id="c:001",
            entity_type=CIFEntityType.CONTACT,
            event_type="item.snapshot",
            data={},
            source="import:test",
            idempotency_key="key",
        )




def test_entity_type_values() -> None:
    assert CIFEntityType.ITEM == "item"
    assert CIFEntityType.CONTACT == "contact"
    assert CIFEntityType.INVOICE == "invoice"
    assert CIFEntityType.MEMO == "memo"


# ── CIFItem ───────────────────────────────────────────────────────────────────


def test_cif_item_minimal() -> None:
    item = _make_item()
    assert item.external_id == "item:001"
    assert item.name == "Test Stone"
    assert item.status == "available"
    assert item.metadata == {}


def test_cif_item_full() -> None:
    item = CIFItem(
        external_id="item:002",
        sku="SKU-001",
        name="Ruby",
        description="Red ruby",
        weight=Decimal("1.5"),
        weight_unit="ct",
        cost_per_unit=Decimal("200.00"),
        total_cost=Decimal("300.00"),
        wholesale_price=Decimal("400.00"),
        retail_price=Decimal("500.00"),
        status="sold",
        attributes={
            "stone_type": "ruby",
            "stone_color": "red",
            "stone_shape": "oval",
            "stone_treatment": "heat",
            "stone_origin": "Burma",
        },
        category="loose",
        parent_external_id="item:000",
        barcode="1234567890",
        source_ref="REF-001",
        created_at=datetime(2026, 1, 1),
        metadata={"extra": "data"},
    )
    assert item.sku == "SKU-001"
    assert item.weight == Decimal("1.5")
    assert item.weight_unit == "ct"
    assert item.metadata == {"extra": "data"}


# ── CIFContact ───────────────────────────────────────────────────────────────


def test_cif_contact_minimal() -> None:
    c = _make_contact()
    assert c.external_id == "c:001"
    assert c.name == "Alice"
    assert c.email is None


def test_cif_contact_full() -> None:
    c = CIFContact(
        external_id="c:002",
        name="Bob",
        email="bob@example.com",
        phone="+1234",
        address="123 St",
        metadata={"vip": True},
    )
    assert c.email == "bob@example.com"
    assert c.metadata == {"vip": True}


# ── CIFLineItem ───────────────────────────────────────────────────────────────


def test_cif_line_item_with_optional_fields() -> None:
    li = CIFLineItem(
        item_external_id="item:001",
        quantity=Decimal("2"),
        weight=Decimal("3.0"),
        weight_unit="ct",
        unit_price=Decimal("50.00"),
        total_price=Decimal("100.00"),
        cost_basis=Decimal("40.00"),
    )
    assert li.weight == Decimal("3.0")
    assert li.weight_unit == "ct"
    assert li.cost_basis == Decimal("40.00")


# ── CIFDocument ───────────────────────────────────────────────────────────────


def test_cif_document_minimal() -> None:
    doc = _make_document()
    assert doc.doc_type == "invoice"
    assert doc.status == "paid"
    assert len(doc.line_items) == 1


def test_cif_document_with_dates() -> None:
    from datetime import date
    doc = CIFDocument(
        external_id="doc:002",
        doc_type="purchase_order",
        status="draft",
        total=Decimal("200.00"),
        amount_paid=Decimal("0.00"),
        amount_outstanding=Decimal("200.00"),
        payment_due_date=date(2026, 3, 31),
        created_at=datetime(2026, 1, 15),
    )
    assert doc.payment_due_date == date(2026, 3, 31)
    assert doc.contact_external_id is None


# ── CIFMemo ───────────────────────────────────────────────────────────────────


def test_cif_memo_minimal() -> None:
    memo = _make_memo()
    assert memo.external_id == "memo:001"
    assert memo.status == "out"
    assert memo.total == Decimal("50.00")


# ── CIFBatch ─────────────────────────────────────────────────────────────────


def test_cif_batch_defaults() -> None:
    batch = CIFBatch(source="test", source_system="gemcloud")
    assert batch.cif_version == CIF_VERSION
    assert batch.record_count == 0
    assert batch.notes is None


# ── CIFImportBundle ───────────────────────────────────────────────────────────


def test_cif_import_bundle_empty() -> None:
    bundle = CIFImportBundle()
    assert bundle.items == []
    assert bundle.contacts == []
    assert bundle.documents == []
    assert bundle.memos == []


def test_cif_import_bundle_populated() -> None:
    bundle = CIFImportBundle(
        items=[_make_item()],
        contacts=[_make_contact()],
        documents=[_make_document()],
        memos=[_make_memo()],
    )
    assert len(bundle.items) == 1
    assert len(bundle.contacts) == 1
    assert len(bundle.documents) == 1
    assert len(bundle.memos) == 1


# ── CIFImportManifest ─────────────────────────────────────────────────────────


def test_manifest_from_dict() -> None:
    manifest = CIFImportManifest.model_validate(_minimal_manifest())
    assert manifest.cif_version == CIF_VERSION
    assert manifest.source == "test"
    assert isinstance(manifest.bundle, CIFImportBundle)


def test_manifest_with_bundle() -> None:
    data = _minimal_manifest()
    data["bundle"]["items"] = [
        {
            "external_id": "item:001",
            "name": "Stone",
            "status": "available",
        }
    ]
    manifest = CIFImportManifest.model_validate(data)
    assert len(manifest.bundle.items) == 1
    assert manifest.bundle.items[0].name == "Stone"


# ── EntityStats ───────────────────────────────────────────────────────────────


def test_entity_stats_per_sec() -> None:
    stats = EntityStats(label="Items", count=100, elapsed=2.0)
    assert stats.per_sec == 50.0


def test_entity_stats_per_sec_zero_elapsed() -> None:
    stats = EntityStats(label="Items", count=100, elapsed=0.0)
    assert stats.per_sec == 0.0


def test_entity_stats_fmt() -> None:
    stats = EntityStats(label="Items", count=100, elapsed=2.0)
    text = stats.fmt()
    assert "Items" in text
    assert "100" in text


# ── ImportResult ──────────────────────────────────────────────────────────────


def test_import_result_defaults() -> None:
    result = ImportResult()
    assert result.total == 0
    assert result.created == 0
    assert result.failed == 0
    assert result.errors == []


def test_import_result_record_error() -> None:
    result = ImportResult()
    result.record_error("item:001", "not found")
    assert result.failed == 1
    assert result.errors[0] == {"entity_id": "item:001", "reason": "not found"}


def test_import_result_summary_no_errors() -> None:
    result = ImportResult(total=10, created=8, skipped=2, failed=0)
    summary = result.summary()
    assert "Total:    10" in summary
    assert "Created:  8" in summary
    assert "Skipped:  2" in summary
    assert "Failed:   0" in summary


def test_import_result_summary_with_errors() -> None:
    result = ImportResult(total=5, created=3, skipped=0, failed=2)
    for i in range(12):
        result.record_error(f"item:{i}", f"err {i}")
    summary = result.summary()
    assert "Errors (first 10):" in summary
    assert "and 2 more" in summary


# ── BundleImporter constructor ────────────────────────────────────────────────


def test_bundle_importer_caps_batch_size() -> None:
    importer = BundleImporter(
        api_base="http://localhost:8000",
        token="tok",
        batch_size=9999,
    )
    assert importer.batch_size == MAX_BATCH_SIZE


def test_bundle_importer_respects_small_batch_size() -> None:
    importer = BundleImporter(
        api_base="http://localhost:8000",
        token="tok",
        batch_size=10,
    )
    assert importer.batch_size == 10


def test_bundle_importer_strips_trailing_slash() -> None:
    importer = BundleImporter(api_base="http://localhost:8000/", token="tok")
    assert importer.api_base == "http://localhost:8000"


# ── dry_run ───────────────────────────────────────────────────────────────────


def test_dry_run_returns_empty_result(capsys) -> None:
    importer = BundleImporter(api_base="http://localhost:8000", token="tok", dry_run=True)
    manifest = CIFImportManifest.model_validate({
        **_minimal_manifest(),
        "bundle": {
            "items": [{"external_id": "i:1", "name": "Stone", "status": "available"}],
            "contacts": [],
            "documents": [],
            "memos": [],
        },
        "stats": {"items": 1},
    })
    result = asyncio.run(importer.run(manifest))
    captured = capsys.readouterr()
    assert result.total == 0
    assert result.created == 0
    assert "Dry Run" in captured.out
    assert "1" in captured.out  # item count


# ── load_manifest ─────────────────────────────────────────────────────────────


def test_load_manifest_valid(tmp_path) -> None:
    f = tmp_path / "manifest.json"
    f.write_text(json.dumps(_minimal_manifest()), encoding="utf-8")
    manifest = load_manifest(f)
    assert manifest.source == "test"


def test_load_manifest_invalid_json(tmp_path) -> None:
    f = tmp_path / "bad.json"
    f.write_text("{not valid json}", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_manifest(f)


def test_load_manifest_invalid_schema(tmp_path) -> None:
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"source": "x"}), encoding="utf-8")  # missing required fields
    with pytest.raises(SystemExit):
        load_manifest(f)


# ── BundleImporter.run with mocked HTTP ───────────────────────────────────────


@pytest.mark.asyncio
async def test_run_calls_batch_endpoints() -> None:
    manifest = CIFImportManifest.model_validate({
        **_minimal_manifest(),
        "bundle": {
            "items": [{"external_id": "i:1", "name": "Stone", "status": "available"}],
            "contacts": [{"external_id": "c:1", "name": "Alice"}],
            "documents": [],
            "memos": [],
        },
    })

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"created": 1, "skipped": 0, "errors": []})

    importer = BundleImporter(api_base="http://localhost:8000", token="tok")

    with patch.object(importer, "_post_batch", new=AsyncMock(return_value={"created": 1, "skipped": 0, "errors": []})):
        result = await importer.run(manifest)

    assert result.created == 2  # 1 item + 1 contact
    assert result.failed == 0


@pytest.mark.asyncio
async def test_run_handles_batch_error() -> None:
    manifest = CIFImportManifest.model_validate({
        **_minimal_manifest(),
        "bundle": {
            "items": [{"external_id": "i:1", "name": "Stone", "status": "available"}],
            "contacts": [],
            "documents": [],
            "memos": [],
        },
    })

    importer = BundleImporter(api_base="http://localhost:8000", token="tok")

    with patch.object(importer, "_post_batch", new=AsyncMock(side_effect=Exception("timeout"))):
        result = await importer.run(manifest)

    # record_error increments failed by 1, then failed += len(chunk) adds 1 more
    assert result.failed == 2
    assert "timeout" in result.errors[0]["reason"]

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Bills that bring in no stock, and bills whose goods arrived before the books were imported,
are not open receipts: the list does not count them as stock to receive and their lines do not
read Not Received."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

from celerp.importers.schema import CIFDocument, CIFLineItem
from ui.i18n import t


def _not_stocked(doc: dict) -> bool:
    from celerp_docs.routes import _doc_flag_rules
    return _doc_flag_rules("2026-11-02")["not_stocked"](doc)


def test_a_bill_with_no_stock_line_is_not_stock_to_receive():
    rent = {"doc_type": "bill", "status": "awaiting_payment",
            "line_items": [{"description": "Rent", "quantity": 1, "unit_price": 900}]}
    assert not _not_stocked(rent)


def test_a_stock_bill_with_nothing_received_is_stock_to_receive():
    stock = {"doc_type": "bill", "status": "awaiting_payment",
             "line_items": [{"item_id": "item:1", "sku": "W-1", "quantity": 3}]}
    assert _not_stocked(stock)
    assert not _not_stocked(stock | {"received_items": ["item:2"]})
    assert not _not_stocked(stock | {"status": "draft"})


def test_a_bill_received_before_import_is_not_stock_to_receive():
    bill = {"doc_type": "bill", "status": "awaiting_payment", "received_before_import": True,
            "line_items": [{"item_id": "item:1", "sku": "W-1", "quantity": 3}]}
    assert not _not_stocked(bill)


def _record(status: str = "awaiting_payment", item: str | None = "item-1", metadata: dict | None = None):
    from celerp_docs.migration_sink import _doc_record

    record = CIFDocument(
        source_system="odoo", source_type="Bill", source_external_id="bill-1", doc_type="bill",
        status=status, ref="BILL-1", issue_date="2024-03-04", total=Decimal("30.00"),
        amount_paid=Decimal("0"), amount_outstanding=Decimal("30.00"), metadata=metadata or {},
        line_items=[CIFLineItem(item_external_id=item, account_external_id="5000", description="Widget", quantity=Decimal("3"),
                                unit_price=Decimal("10.00"), total_price=Decimal("30.00"))],
    )
    context = SimpleNamespace(run_id=uuid.uuid4(), company_id=uuid.uuid4(),
                              idempotency_key=lambda r, op: f"k:{op}")
    out = _doc_record(context, record, "USD", {}, {"item-1": "item:1"}, {}, {"5000": "5000"}, {}, {})
    assert not isinstance(out, str), out
    return out.data


def test_an_issued_stock_bill_with_no_source_receipts_was_received_before_import():
    assert _record()["received_before_import"] is True


def test_only_issued_stock_bills_without_receipts_are_marked_received_before_import():
    assert "received_before_import" not in _record(status="draft")
    assert "received_before_import" not in _record(status="void")
    assert "received_before_import" not in _record(item=None)
    receipts = [{"source": "r-1", "date": "2024-03-05", "lines": []}]
    assert "received_before_import" not in _record(metadata={"receipts": receipts})


def test_a_line_received_before_import_reads_so():
    from ui.routes.documents import _vendor_line_badge

    line = {"item_id": "item:1", "quantity": 3}
    assert str(t("documents.status_received_before_import")) in str(_vendor_line_badge(line, "awaiting_payment", before_import=True))
    assert str(t("documents.status_not_received")) in str(_vendor_line_badge(line, "awaiting_payment"))

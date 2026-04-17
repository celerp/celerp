# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Unit tests for celerp/services/pdf.py.

Validates that generate_pdf returns valid PDF bytes and that all
code paths (empty line items, outstanding balance, notes, minimal doc)
are exercised for coverage.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def _pdf(doc: dict, company: dict | None = None) -> bytes:
    from celerp_docs.pdf import generate_document_pdf
    return generate_document_pdf(doc, company)


def _is_pdf(data: bytes) -> bool:
    return data[:4] == b"%PDF"


class TestGeneratePdf:
    def test_returns_pdf_bytes(self):
        data = _pdf({"doc_type": "invoice", "ref_id": "INV-001"})
        assert _is_pdf(data)
        assert len(data) > 1000

    def test_minimal_doc(self):
        """Empty dict must not raise."""
        data = _pdf({})
        assert _is_pdf(data)

    def test_with_line_items(self):
        doc = {
            "doc_type": "invoice",
            "ref_id": "INV-100",
            "line_items": [
                {"name": "Blue Sapphire", "quantity": 2, "unit_price": 1200.0, "line_total": 2400.0, "tax_rate": 7},
                {"name": "Gold Ring", "quantity": 1, "unit_price": 500.0, "line_total": 500.0},
            ],
            "subtotal": 2900.0,
            "tax_amount": 168.0,
            "total_amount": 3068.0,
        }
        data = _pdf(doc)
        assert _is_pdf(data)

    def test_with_outstanding_balance(self):
        """Outstanding > 0 triggers an extra row in totals table."""
        doc = {
            "doc_type": "invoice",
            "ref_id": "INV-200",
            "total_amount": 500.0,
            "outstanding_balance": 500.0,
        }
        data = _pdf(doc)
        assert _is_pdf(data)

    def test_empty_line_items_list(self):
        """Empty line_items list should render 'No line items.' row without crashing."""
        doc = {"doc_type": "invoice", "ref_id": "INV-EMPTY", "line_items": []}
        data = _pdf(doc)
        assert _is_pdf(data)

    def test_with_notes(self):
        doc = {
            "doc_type": "invoice",
            "ref_id": "INV-NOTES",
            "notes": "Net 30 days. Thank you for your business.",
        }
        data = _pdf(doc)
        assert _is_pdf(data)

    def test_with_payment_terms_fallback(self):
        """notes field is absent; payment_terms should be used."""
        doc = {
            "doc_type": "invoice",
            "ref_id": "INV-PT",
            "payment_terms": "Net 60",
        }
        data = _pdf(doc)
        assert _is_pdf(data)

    def test_doc_type_memo(self):
        doc = {"doc_type": "memo", "ref_id": "MEMO-001", "total_amount": 100.0}
        data = _pdf(doc)
        assert _is_pdf(data)

    def test_doc_type_purchase_order(self):
        doc = {"doc_type": "purchase_order", "ref_id": "PO-001"}
        data = _pdf(doc)
        assert _is_pdf(data)

    def test_company_info_in_doc(self):
        """Company dict passed separately should not crash rendering."""
        doc = {
            "doc_type": "invoice",
            "ref_id": "INV-CO",
            "contact_name": "Alice Smith",
            "contact_address": "456 Sukhumvit, Bangkok",
        }
        company = {
            "name": "Demo Company Ltd",
            "address": "123 Silom Road, Bangkok, TH 10500",
        }
        data = _pdf(doc, company)
        assert _is_pdf(data)

    def test_zero_totals_do_not_crash(self):
        """Zero/missing totals render as ฿0.00 without crashing."""
        doc = {
            "doc_type": "invoice",
            "ref_id": "INV-ZERO",
            "subtotal": 0,
            "tax_amount": None,
            "total_amount": 0,
        }
        data = _pdf(doc)
        assert _is_pdf(data)

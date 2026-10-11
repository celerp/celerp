# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Aging includes only unused, issued credit notes as current offsets."""
from decimal import Decimal

from celerp_reports.routes import _aging_amount


def test_unapplied_credit_is_current_negative_aging():
    credit = {"doc_type": "credit_note", "status": "final", "amount_outstanding": 25, "total": 50}
    assert _aging_amount(credit, ("invoice",), "credit_note") == (Decimal("-25"), True)
    paid = {**credit, "amount_outstanding": 0, "status": "paid"}
    assert _aging_amount(paid, ("invoice",), "credit_note") is None
    draft = {**credit, "status": "draft"}
    assert _aging_amount(draft, ("invoice",), "credit_note") is None
    assert _aging_amount({**credit, "status": "void"}, ("invoice",), "credit_note") is None


def test_aged_invoice_and_debit_note_use_one_balance_rule():
    charge = {"doc_type": "invoice", "status": "partial", "amount_outstanding": 10, "total": 70}
    assert _aging_amount(charge, ("invoice",), "credit_note") == (Decimal("10"), False)
    debit_note = {"doc_type": "debit_note", "status": "final", "amount_outstanding": 20}
    assert _aging_amount(debit_note, ("purchase_order", "bill"), "debit_note") == (Decimal("-20"), True)
    assert _aging_amount({**debit_note, "status": "paid"}, ("purchase_order", "bill"), "debit_note") is None

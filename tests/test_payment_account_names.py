# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A document's payments name the account each one settled from by code and name, never a bare code."""

from __future__ import annotations

from fasthtml.common import to_xml


def test_a_payment_reads_its_account_by_code_and_name():
    from ui.routes.documents import _payment_section

    doc = {"entity_id": "doc:1", "doc_type": "invoice", "status": "partial", "currency": "USD", "total": 200,
           "amount_paid": 6, "amount_outstanding": 194,
           "payments": [{"index": 0, "payment_date": "2024-04-12", "method": "withholding",
                         "bank_account": "115000", "amount": 6}]}
    chart = [{"code": "115000", "name": "Tax Withheld by Customers"}, {"code": "101000", "name": "Bank"}]
    html = to_xml(_payment_section(doc, chart_accounts=chart))
    assert "115000 Tax Withheld by Customers" in html
    # An account the chart no longer holds still shows its code.
    assert ">115000<" in to_xml(_payment_section(doc))

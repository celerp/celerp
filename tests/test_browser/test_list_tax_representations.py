# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: a quotation saved from its editor charges each line as its invoice will.

The list editor saves every line with its taxes and, beside them, the sum of their rates as
``tax_rate``. A line holding compound taxes is charged by its taxes (5% then 10% on the price with
VAT is 15.50 on 100, not a flat 15.00); a line stored with only a rate is sent as one tax at that
rate and keeps it. The invoice converted from the list charges the same.
"""
from __future__ import annotations

import pytest

from .test_doc_live_totals import _held_saves, _open, _PENDING, _shown, _stored, _type_price, _wait_for
from .test_from_items_tax_e2e import _COMPOUND_AFTER_VAT, _demo_company

pytestmark = pytest.mark.browser

_VAT = {"code": "VAT 5%", "rate": 5.0, "amount": 0, "order": 0, "is_compound": False, "label": "VAT 5%"}
_LEVY = {"code": "Levy 10%", "rate": 10.0, "amount": 0, "order": 1, "is_compound": True, "label": "Levy 10%"}


def _line(item: dict, price: float, **tax) -> dict:
    return {"item_id": item["id"], "sku": item["sku"], "name": item.get("name") or item["sku"],
            "quantity": 1, "unit_price": price, **tax}


def test_one_save_with_compound_taxes_and_a_rate_only_line_charges_what_the_invoice_charges(
        page, api_server, ui_server, api):
    with _demo_company(api_server, ui_server, api, "GB", _COMPOUND_AFTER_VAT) as (company, ui, token, items):
        r = company.post("/lists", json={"list_type": "quotation", "line_items": [
            _line(items[0], 50.0, taxes=[_VAT, _LEVY]), _line(items[1], 50.0, tax_rate=7.0)]})
        assert r.status_code == 200, r.text
        list_id = r.json()["id"]
        ctx, tab = _open(page, ui_server, token, list_id, "lists")
        try:
            held = _held_saves(tab, list_id, "lists")
            _type_price(tab, "100")
            _wait_for(tab, held)
            # The one request carries both forms on every line: the compound taxes with their
            # summed rate beside them, and the rate-only line's rate as one tax beside that rate.
            sent = held[0].request.post_data_json["line_items"]
            assert [(t["code"], t["is_compound"]) for t in sent[0]["taxes"]] == [
                ("VAT 5%", False), ("Levy 10%", True)]
            assert sent[0]["tax_rate"] == 15
            assert ([(t["rate"], t["is_compound"]) for t in sent[1]["taxes"]], sent[1]["tax_rate"]) == (
                [(7, False)], 7)

            held[0].continue_()
            tab.wait_for_selector(_PENDING, state="detached", timeout=8000)
            stored = _stored(company, list_id, "lists")
            # 100: VAT 5.00 + levy 10.50; 50 at 7%: 3.50.
            assert stored == {"taxes": 19.0, "subtotal": 150.0, "total": 169.0}
            assert _shown(tab) == stored
        finally:
            ctx.close()

        r = company.post(f"/lists/{list_id}/finalize", json={})
        assert r.status_code == 200, r.text
        r = company.post(f"/lists/{list_id}/convert", json={"target_type": "invoice"})
        assert r.status_code == 200, r.text
        assert _stored(company, r.json()["target_doc_id"]) == stored

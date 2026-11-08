# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bulk table's selection posts as one field, whatever the number of rows.

Generate all due lists one row per agreement with a bill due. The rows ticked when Generate due
is clicked go to the server as one ``item`` field holding them as a JSON list, so a page of any
size stays under the form parser's field cap; an unticked row is not in it.
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def _agreement(api) -> str:
    r = api.post("/docs", json={
        "doc_type": "subscription_invoice", "frequency": "monthly", "start_date": "2026-01-01",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
    })
    assert r.status_code in {200, 201}, r.text
    eid = r.json()["id"]
    assert api.post(f"/subscriptions/{eid}/activate").status_code == 200
    return eid


def test_generate_due_posts_the_ticked_rows_as_one_field(page, ui_server, fresh_company):
    eids = {_agreement(fresh_company) for _ in range(3)}
    page.goto(f"{ui_server}/subscriptions?direction=sales", wait_until="domcontentloaded")
    page.locator("button[hx-get^='/subscriptions/due']").click()
    boxes = page.locator("#sub-due-panel tbody .bulk-select")
    expect(boxes).to_have_count(3, timeout=8000)
    left_out = boxes.first.get_attribute("value")
    boxes.first.uncheck()
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/subscriptions/generate-due")) as sent:
        page.locator("#sub-due-panel button[type=submit]").click()
    fields = re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n--', sent.value.post_data, re.S)
    chosen = json.loads(dict(fields)["item"])
    assert [k for k, _ in fields].count("item") == 1
    assert left_out not in chosen and {c.split("|")[0] for c in chosen} == eids - {left_out.split("|")[0]}

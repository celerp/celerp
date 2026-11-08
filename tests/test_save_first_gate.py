# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A page that edits lines holds one save-first gate (ui/routes/documents.py): every htmx request
that changes something waits for the page's pending line edits to save. So every state-changing
control on a draft agreement's page and a draft document's page must be an htmx request (hx-post,
hx-put, hx-patch, hx-delete) the gate sees; a control that posts by itself from an inline handler,
or a form that posts without htmx, would go around it."""
from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup

_VERBS = ("hx-post", "hx-put", "hx-patch", "hx-delete")
_GATE = "window._celerpSaveFirstBound"
_SENDS = re.compile(r"\bfetch\(|celerpFetch\(|XMLHttpRequest|\.submit\(|htmx\.ajax\(\s*['\"](?!GET)", re.I)


async def _draft_agreement(api) -> str:
    r = await api.post("/docs", json={
        "doc_type": "subscription_invoice", "frequency": "monthly",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0, "line_total": 100.0}],
    })
    assert r.status_code in {200, 201}, r.text
    eid = r.json()["id"]
    r = await api.patch(f"/subscriptions/{eid}/schedule", json={"anchor_date": "2027-03-01", "frequency": "monthly"})
    assert r.status_code == 200, r.text
    r = await api.post(f"/subscriptions/{eid}/periods/1/skip", json={"reason": "holiday"})
    assert r.status_code == 200, r.text
    return f"/subscriptions/{eid}"


async def _draft_invoice(api) -> str:
    r = await api.post("/docs", json={
        "doc_type": "invoice", "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100.0}],
    })
    assert r.status_code in {200, 201}, r.text
    return f"/docs/{r.json()['id']}"


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_draft_agreement, _draft_invoice], ids=["agreement", "document"])
async def test_every_state_changing_control_on_a_draft_page_goes_through_the_gate(owner_ui, make):
    url = await make(owner_ui.api)
    r = await owner_ui.get(url)
    assert r.status_code == 200, r.text
    page = BeautifulSoup(r.text, "html.parser")
    assert page.find(id="line-body") is not None and r.text.count(_GATE) == 2, url

    main = page.find("main")
    doors = main.find_all(lambda n: any(n.has_attr(v) for v in _VERBS))
    assert doors, url
    for form in main.find_all("form"):
        assert (form.get("method") or "get").lower() == "get" or any(form.has_attr(v) for v in _VERBS), form
    for node in main.find_all(True):
        for attr, js in node.attrs.items():
            if attr.startswith("on"):
                assert not _SENDS.search(str(js)), (attr, node)

# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End to end: an invoice made from demo items in a new company is taxed at the country's
default rate once, never at every sales tax of the regime stacked together.

Real API and UI servers on Postgres: the company takes its tax regime from its default
location's country, the demo items are seeded the way setup seeds them, and the invoice is
made through the inventory "Send to > New invoice" route.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import _API_LIMITS
from .test_demo_items_link import _seed_demo_items

pytestmark = pytest.mark.browser


@pytest.mark.parametrize(("country", "factor"), [("GB", 1.20), ("IN", 1.18)])
def test_from_items_invoice_total_is_subtotal_plus_default_rate(api_server, ui_server, api, country, factor):
    r = api.post("/companies", json={"name": f"Tax {country} {uuid.uuid4().hex[:6]}"})
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]
    with httpx.Client(base_url=api_server, headers={"Authorization": f"Bearer {token}"}, timeout=10,
                      limits=_API_LIMITS) as company, \
            httpx.Client(base_url=ui_server, cookies={"celerp_token": token}, timeout=30) as ui:
        default = next(loc for loc in company.get("/companies/me/locations").json()["items"] if loc["is_default"])
        r = company.patch(f"/companies/me/locations/{default['id']}",
                          json={"address": {**(default.get("address") or {}), "country": country}})
        assert r.status_code == 200, r.text
        _seed_demo_items(company)
        items = company.get("/items", params={"filter": "demo", "limit": 500}).json()["items"]
        assert items, "no demo items were seeded"

        r = ui.post("/docs/from-items/new", data={"selected": [i["id"] for i in items]})
        assert r.status_code == 204, r.text
        doc_id = r.headers["HX-Redirect"].rsplit("/", 1)[-1]
        doc = company.get(f"/docs/{doc_id}").json()

    subtotal, total = float(doc["subtotal"]), float(doc["total"])
    assert subtotal > 0, doc
    assert abs(total - round(subtotal * factor, 2)) < 0.01 * len(doc["line_items"]), (subtotal, total, doc.get("doc_taxes"))

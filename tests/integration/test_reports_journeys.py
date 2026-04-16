# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_reports_sales_purchases_expiring_smoke(journey_api):
    sales = await journey_api.get("/reports/sales")
    assert sales.status_code == 200, sales.text
    assert "lines" in sales.json()

    purchases = await journey_api.get("/reports/purchases")
    assert purchases.status_code == 200, purchases.text
    assert "lines" in purchases.json()

    expiring = await journey_api.get("/reports/expiring", params={"days": 30})
    assert expiring.status_code == 200, expiring.text
    assert "items" in expiring.json() or "lines" in expiring.json() or "rows" in expiring.json()

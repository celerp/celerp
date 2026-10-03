# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A module's pricing_action shows on the Pricing rows it asks for, and following it
opens the module's page with that row's item, price list and field."""
import os
import uuid
from pathlib import Path

import pytest
from starlette.responses import PlainTextResponse
from starlette.routing import Route

pytestmark = pytest.mark.browser

_SHOTS = os.environ.get("PRICING_ACTION_SCREENSHOTS")


def _shot(page, name: str) -> None:
    """Save a screenshot when a directory is configured for them."""
    if _SHOTS:
        page.screenshot(path=str(Path(_SHOTS) / f"{name}.png"), full_page=True)


async def _module_page(request):
    q = request.query_params
    return PlainTextResponse(
        f"item={request.path_params['entity_id']} list={q['list']} field={q['field']}")


@pytest.fixture()
def quoter_module():
    """A module contributing one sell-row action and the page it opens."""
    from celerp.modules import slots
    from ui.app import app as ui_app
    saved_slots = slots.all_slots()
    saved_routes = list(ui_app.router.routes)
    ui_app.router.routes.insert(0, Route("/quoter-demo/{entity_id}", _module_page))
    slots.register("pricing_action", {
        "label": "Quote",
        "href_template": "/quoter-demo/{entity_id}?list={price_list}&field={field_name}",
        "show_on": ["sell", "manual"],
        "_module": "celerp-quoter-demo",
    })
    try:
        yield
    finally:
        slots.clear()
        for name, items in saved_slots.items():
            for item in items:
                slots.register(name, item)
        ui_app.router.routes[:] = saved_routes


def test_pricing_row_action_opens_the_module_page(page, ui_server, fresh_company, quoter_module):
    # The demo module is not in any company's enabled list, so the action shows only on a
    # company that has never narrowed its modules. The shared session company may have had
    # a preset applied by an earlier test on the same worker; this test owns its company.
    api = fresh_company
    assert "enabled_modules" not in (api.get("/companies/me").json()["settings"] or {})
    r = api.post("/items", json={"sku": f"PA-{uuid.uuid4().hex[:6]}", "name": "Quote me",
                                 "sell_by": "piece", "quantity": 2, "retail_price": 40})
    assert r.status_code in {200, 201}, r.text
    eid = r.json()["id"]

    page.goto(f"{ui_server}/inventory/{eid}?tab=pricing", wait_until="domcontentloaded")
    retail = page.locator("tr", has=page.locator("td.detail-label", has_text="Retail")).first
    retail.get_by_role("link", name="Quote").wait_for()
    assert page.locator("th", has_text="Actions").count() == 1  # the sell card only
    cost_rows = page.locator(".detail-card", has=page.locator("h3", has_text="Cost"))
    assert cost_rows.get_by_role("link", name="Quote").count() == 0
    _shot(page, "pricing-action-row")

    retail.get_by_role("link", name="Quote").click()
    page.wait_for_url("**/quoter-demo/**")
    assert page.locator("body").inner_text().strip() == (
        f"item={eid} list=Retail field=retail_price")

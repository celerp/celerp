# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: the column setup a page saves lands in the order the user made the changes.
A change, a reset and the move of an older browser copy all go through one queue per page, so
an earlier answer that arrives late, or a request whose answer is lost on the way, never puts
back a setup the user has since changed or reset. The browser copy is cleared part by part,
only for the parts the server kept."""
from __future__ import annotations

import uuid

import pytest

from .conftest import htmx_idle, saved_column_layout

pytestmark = pytest.mark.browser

_PREFS = "**/column-prefs"


@pytest.fixture
def inventory_key(page, ui_server, api):
    api.post("/items", json={"sku": f"CQ-{uuid.uuid4().hex[:8]}", "name": "Queue Item",
                             "quantity": 1, "sell_by": "piece"}).raise_for_status()
    page.goto(f"{ui_server}/inventory", wait_until="load")
    page.wait_for_selector("#data-table thead th[data-key]", state="attached", timeout=10000)
    htmx_idle(page)
    return page.locator("#data-table thead th[data-key]").first.get_attribute("data-key")


def _deliver(route, drop=False):
    """Send a held request on to the server; its answer reaches the page unless ``drop``."""
    answer = route.fetch()
    route.abort() if drop else route.fulfill(response=answer)


def _held_until(page, held, n):
    page.wait_for_timeout(300)  # let the page send what it is going to send
    assert len(held) == n, f"{len(held)} column setup requests were in flight at once, expected {n}"


def _release_newest_first(page, held, total, drop_first=False):
    """Answer the held requests newest first, as a slow network can, until ``total`` have been
    answered. A request the queue sends only after an earlier answer is held and answered too."""
    done = 0
    while done < total:
        page.wait_for_timeout(200)
        assert held, f"only {done} of {total} column setup requests were sent"
        route = held.pop()
        _deliver(route, drop=drop_first and done == 0)
        done += 1


def test_an_earlier_save_answered_late_does_not_overwrite_a_newer_one(page, inventory_key, api):
    held = []
    page.route(_PREFS, lambda route: held.append(route))
    page.evaluate("k => { var c = celerpColumns('inventory'); c.set('vis', {[k]: false});"
                  " c.set('vis', {[k]: true}); }", inventory_key)
    _held_until(page, held, 1)
    _release_newest_first(page, held, 2)
    layout = saved_column_layout(api, "inventory", lambda l: (l.get("vis") or {}).get(inventory_key) is True)
    assert layout.get("vis") == {inventory_key: True}


def test_a_save_whose_answer_is_lost_still_lets_the_newer_one_win(page, inventory_key, api):
    held = []
    page.route(_PREFS, lambda route: held.append(route))
    page.evaluate("k => { var c = celerpColumns('inventory'); c.set('vis', {[k]: false});"
                  " c.set('vis', {[k]: true}); }", inventory_key)
    _held_until(page, held, 1)
    # The first save reaches the server but its answer is dropped on the way back.
    _release_newest_first(page, held, 2, drop_first=True)
    layout = saved_column_layout(api, "inventory", lambda l: (l.get("vis") or {}).get(inventory_key) is True)
    assert layout.get("vis") == {inventory_key: True}


def test_a_pending_save_cannot_undo_a_reset(page, inventory_key, api):
    held = []
    page.route(_PREFS, lambda route: held.append(route))
    page.evaluate("k => { celerpColumns('inventory').set('vis', {[k]: false});"
                  " celerpResetColumns(['inventory']); }", inventory_key)
    _held_until(page, held, 1)
    _release_newest_first(page, held, 2)
    page.wait_for_load_state("load")
    assert saved_column_layout(api, "inventory", lambda l: not l) == {}


def _browser_copy(page, values):
    page.evaluate("v => Object.keys(v).forEach(k => localStorage.setItem(k, v[k]))", values)


def test_a_browser_copy_with_one_bad_value_moves_its_good_values(page, ui_server, inventory_key, api):
    keys = {"celerp_cols_inventory": f'{{"{inventory_key}": false, "": true, "x": "yes"}}',
            "celerp_col_widths_inventory": f'{{"{inventory_key}": "120px", "other_col": "{"9" * 30}px"}}'}
    _browser_copy(page, keys)
    page.reload(wait_until="load")
    layout = saved_column_layout(api, "inventory", lambda l: "vis" in l and "widths" in l)
    assert layout == {"vis": {inventory_key: False}, "widths": {inventory_key: "120px"}}
    assert page.evaluate("ks => ks.map(k => localStorage.getItem(k))", list(keys)) == [None, None]


def test_a_refused_part_of_the_browser_copy_stays_for_the_next_load(page, ui_server, inventory_key, api):
    keys = {"celerp_cols_inventory": f'{{"{inventory_key}": false}}',
            "celerp_col_widths_inventory": f'{{"{inventory_key}": "120px"}}'}
    _browser_copy(page, keys)

    def refuse_widths(route):
        if "widths" in (route.request.post_data or ""):
            route.fulfill(status=422, json={"detail": "refused"})
        else:
            route.continue_()
    page.route(_PREFS, refuse_widths)
    page.reload(wait_until="load")
    saved_column_layout(api, "inventory", lambda l: "vis" in l)
    page.wait_for_timeout(300)
    stored = page.evaluate("ks => ks.map(k => localStorage.getItem(k))", list(keys))
    assert stored == [None, keys["celerp_col_widths_inventory"]]

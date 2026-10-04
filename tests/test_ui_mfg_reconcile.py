# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Reconciling a production run from the page its notification links to.

The page says why the run needs reconciling, asks for the value of each component still in
it and the account that value comes off, and sends them. A refusal keeps what was entered
and says why in the user's language; success says the run can carry on. The run's row on its
product's Manufacturing tab links to the same page.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml

from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui import i18n
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

RUN = {"id": "mfg:abc12345", "output_item_id": "item:p", "expected_outputs": [{"item_id": "item:p", "sku": "CAKE"}],
       "wip_unresolved": "books disagree"}
NEEDS = {"reason": "books disagree",
         "components": [{"item_id": "item:flour", "quantity": 2.5, "sku": "FLOUR", "name": "Flour"}]}
POSTING = {"roles": [{"role": "retained_earnings", "code": "3200", "name": "Retained earnings"},
                     {"role": "work_in_progress", "code": "1130", "name": "Work in progress"}],
           "older_stock": {"lots": [], "candidates": [{"code": "1120", "name": "Inventory", "account_type": "asset"}]}}
LOCKED = {"message": "The books are locked for 2026-01-31, so this run cannot be reconciled until that period is open.",
          "message_key": "mfg.period_locked", "params": {"day": "2026-01-31"}}


@pytest.fixture(autouse=True)
def _lang():
    yield
    i18n.set_lang("en")


def _reading(posting=None, needs=None):
    return (patch("ui.api_client.get_mfg_order", new=AsyncMock(return_value=RUN)),
            patch("ui.api_client.mfg_reconcile_needs", new=AsyncMock(return_value=needs or NEEDS)),
            patch("ui.api_client.get_posting_accounts",
                  new=AsyncMock(**({"side_effect": posting} if isinstance(posting, Exception)
                                   else {"return_value": posting or POSTING}))))


async def _page(ui_client, posting=None, needs=None, lang="en"):
    a, b, c = _reading(posting, needs)
    with a, b, c:
        return await ui_client.get(f"/manufacturing/runs/{RUN['id']}/reconcile",
                                   cookies={**_authed(), "celerp_lang": lang})


async def _send(ui_client, values, account, reconcile, lang="en"):
    body = "&".join([*(f"item_id={i}&value={v}" for i, v in values), f"account={account}", "idempotency_key=k1"])
    a, b, c = _reading()
    with a, b, c, patch("ui.api_client.reconcile_mfg_order", new=reconcile):
        return await ui_client.post(f"/manufacturing/runs/{RUN['id']}/reconcile", content=body.encode(),
                                    headers={"content-type": "application/x-www-form-urlencoded"},
                                    cookies={**_authed(), "celerp_lang": lang})


async def test_the_page_says_why_and_asks_for_each_value_and_the_account(ui_client):
    r = await _page(ui_client)

    assert r.status_code == 200, r.text
    assert "the books hold a different amount than its history shows" in r.text
    assert "FLOUR Flour" in r.text and 'name="value"' in r.text and 'value="item:flour"' in r.text
    assert '<td class="cell--number">2.5</td>' in r.text  # how much of it the run holds
    assert 'value="1120"' in r.text and 'value="3200"' in r.text and 'value="1130"' not in r.text
    assert 'name="idempotency_key"' in r.text
    assert 'href="/inventory/item:p?tab=manufacturing"' in r.text  # the way back to the run's product


async def test_with_accounting_off_the_page_asks_only_for_values(ui_client):
    r = await _page(ui_client, posting=APIError(403, "Accounting is not running"))

    assert r.status_code == 200, r.text
    assert 'name="value"' in r.text and 'name="account"' not in r.text


async def test_a_run_that_holds_nothing_or_needs_nothing_says_so(ui_client):
    assert "This run holds no materials." in (await _page(ui_client, needs={"reason": "books disagree",
                                                                           "components": []})).text
    r = await _page(ui_client, needs={"reason": None, "components": []})
    assert "This run does not need reconciling." in r.text and "<form" not in r.text


async def test_sending_records_the_values_and_says_the_run_can_carry_on(ui_client):
    reconcile = AsyncMock(return_value={"reconciled": "30.00"})

    r = await _send(ui_client, [("item:flour", "30")], "1120", reconcile)

    assert r.status_code == 200, r.text
    reconcile.assert_awaited_once()
    assert reconcile.await_args.args[1:] == (RUN["id"], {"components": [{"item_id": "item:flour", "value": 30.0}],
                                                         "account": "1120"})
    assert reconcile.await_args.kwargs == {"idempotency_key": "k1"}
    assert "Reconciled. The run can issue, return, receive and complete again." in r.text


async def test_a_refusal_keeps_what_was_entered_and_says_why_in_the_users_language(ui_client):
    reconcile = AsyncMock(side_effect=APIError(422, LOCKED["message"], LOCKED))

    r = await _send(ui_client, [("item:flour", "30")], "1120", reconcile, lang="th")

    assert r.status_code == 200, r.text
    assert "บัญชีถูกล็อกสำหรับวันที่ 2026-01-31" in r.text
    assert 'value="30"' in r.text and 'value="k1"' in r.text


async def test_a_value_that_is_not_a_number_is_sent_as_missing_so_the_refusal_names_it(ui_client):
    missing = {"message": "Give the value of every component still in this run: item:flour has none.",
               "message_key": "mfg.reconcile_missing", "params": {"items": "item:flour"}}
    reconcile = AsyncMock(side_effect=APIError(422, missing["message"], missing))

    r = await _send(ui_client, [("item:flour", "abc")], "1120", reconcile)

    assert reconcile.await_args.args[2]["components"] == []
    assert missing["message"] in r.text


async def test_the_runs_row_on_its_product_links_to_reconciling_it():
    from ui.routes.inventory import _production_block

    html = to_xml(_production_block("item:p", {"id": "item:p"}, {"runs": [{**RUN, "status": "in_progress"},
                                                                          {"id": "mfg:fine", "status": "planned"}]},
                                    "$"))
    assert html.count("Needs reconciling") == 1
    assert f'href="/manufacturing/runs/{RUN["id"]}/reconcile"' in html


async def test_output_already_received_is_listed_so_the_value_given_is_the_whole_issue(ui_client):
    needs = {**NEEDS, "reason": "received before tracking",
             "received": [{"lot_item_id": "item:lot1", "sku": "CAKE", "quantity": 1.0, "value": "100.00"}]}

    r = await _page(ui_client, needs=needs)

    assert r.status_code == 200, r.text
    assert "Output this run already received: CAKE (1)." in r.text


UNLOTTED = {"reason": "received before tracking", "unlotted": 1.0, "components": [], "received": []}


async def test_a_receipt_that_made_no_stock_is_offered_for_discarding(ui_client):
    r = await _page(ui_client, needs=UNLOTTED)

    assert r.status_code == 200, r.text
    assert "recorded 1 as received without making any stock" in r.text
    assert f'hx-post="/manufacturing/runs/{RUN["id"]}/repair-output"' in r.text
    assert "Discard" in r.text
    assert "repair-output" not in (await _page(ui_client)).text  # nothing to discard, nothing offered


async def test_discarding_says_what_comes_next(ui_client):
    repair = AsyncMock(return_value={"discarded": 1.0, "output_item_id": None})
    a, b, c = _reading(needs=UNLOTTED)
    with a, b, c, patch("ui.api_client.repair_mfg_output", new=repair):
        r = await ui_client.post(f"/manufacturing/runs/{RUN['id']}/repair-output", content=b"idempotency_key=k2",
                                 headers={"content-type": "application/x-www-form-urlencoded"}, cookies=_authed())

    assert r.status_code == 200, r.text
    assert repair.await_args.args[1:] == (RUN["id"],) and repair.await_args.kwargs == {"idempotency_key": "k2"}
    assert "Discarded." in r.text


async def test_a_refused_discard_says_why_and_keeps_the_offer(ui_client):
    refused = {"message": "This run is cancelled, so it cannot be repaired.", "message_key": "mfg.run_closed",
               "params": {"status": "cancelled"}}
    a, b, c = _reading(needs=UNLOTTED)
    with a, b, c, patch("ui.api_client.repair_mfg_output",
                        new=AsyncMock(side_effect=APIError(409, refused["message"], refused))):
        r = await ui_client.post(f"/manufacturing/runs/{RUN['id']}/repair-output", content=b"idempotency_key=k2",
                                 headers={"content-type": "application/x-www-form-urlencoded"}, cookies=_authed())

    assert r.status_code == 200, r.text
    assert refused["message"] in r.text and "repair-output" in r.text

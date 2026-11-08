# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A stock line action does only what the confirm the user answered said.

Someone else changes the lines while the confirm is open. The action sent on Confirm changes
nothing, and the page asks the fresh question, saying the lines changed; answered, the action
does what that question said, and its notice names any shipped line it took back."""
from __future__ import annotations

import pytest

from .conftest import answer_confirms
from .test_item_less_lines_browser import _GIA, _NOT_STOCK, _invoice, _line_ids, _persistent_toast, _status, _stone, _stone_line

pytestmark = pytest.mark.browser

_CHANGED = "The lines changed while this confirm was open, so nothing was changed. This is what the action does now: "


def _ask_and_change_once(page, change):
    """Answers every question, running ``change`` (someone else's action) while the first is open."""
    asked: list[str] = []

    def answer(question: str) -> bool:
        if not asked:
            change()
        asked.append(question)
        return True
    answer_confirms(page, answer)
    return asked


def _act(api, doc_id: str, path: str, line_ids: list[str], **extra) -> None:
    r = api.post(f"/docs/{doc_id}/{path}", json={"line_ids": line_ids, **extra})
    assert r.status_code in (200, 201), r.text


def test_set_as_shipped_ships_no_line_its_confirm_left_until_asked_again(page, ui_server, api):
    """Red statement: the confirm said line 1 would be left as it is; it was taken back
    meanwhile, and Confirm shipped it too, with a notice that said nothing of it."""
    sold, free = _stone(api), _stone(api)
    doc_id = _invoice(api, [_stone_line(sold), _stone_line(free), dict(_GIA)])
    sold_id = _line_ids(api, f"/docs/{doc_id}")[0]
    _act(api, doc_id, "fulfill-lines", [sold_id])
    asked = _ask_and_change_once(page, lambda: _act(api, doc_id, "set-available", [sold_id]))
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    page.locator("#li-bulk-select").select_option(value="li-fulfill")
    page.locator("#li-bulk-fulfill-btn").click()
    notice = _persistent_toast(page, "GIA Certificate")
    assert asked == [
        f'Set 1 line as shipped? Line 1 "{sold["sku"]}" has already shipped, so it will be left as it is. '
        f'Line 3 "GIA Certificate" {_NOT_STOCK}',
        _CHANGED + f'Set 2 lines as shipped? Line 3 "GIA Certificate" {_NOT_STOCK}']
    assert notice.startswith("2 lines set as shipped.")
    assert _status(api, sold) == "sold" and _status(api, free) == "sold"


def test_set_as_reserved_reverses_no_sale_its_confirm_did_not_warn_of(page, ui_server, api):
    """Red statement: a line shipped while the confirm "Set 2 lines as reserved?" was open;
    Confirm took its goods back and reversed the sale, and neither question nor notice said so."""
    first, second = _stone(api), _stone(api)
    doc_id = _invoice(api, [_stone_line(first), _stone_line(second), dict(_GIA)])
    first_id = _line_ids(api, f"/docs/{doc_id}")[0]
    asked = _ask_and_change_once(page, lambda: _act(api, doc_id, "fulfill-lines", [first_id]))
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.check("#li-select-all")
    page.locator("#li-bulk-select").select_option(value="li-reserve")
    page.locator("#li-bulk-reserve-btn").click()
    notice = _persistent_toast(page, "came back into stock")
    taken = f'Line 1 "{first["sku"]}"'
    assert asked == [
        f'Set 2 lines as reserved? Line 3 "GIA Certificate" {_NOT_STOCK}',
        _CHANGED + f'Set 2 lines as reserved? Line 3 "GIA Certificate" {_NOT_STOCK} '
        f'{taken} has already shipped: this takes its goods back into stock and reverses its shipment.']
    assert notice.startswith("2 lines set as reserved.")
    assert f"{taken} had already shipped, so its goods came back into stock and its shipment was reversed." in notice
    assert _status(api, first) == "reserved" and _status(api, second) == "reserved"

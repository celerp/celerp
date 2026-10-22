# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What the accountant sees of older stock placed automatically.

The person selling sees one plain notice that existing inventory is ready. The
accountant's Older stock tab shows, per inventory account, the lots on it and what
placements transferred, recognized as opening stock and left unresolved; each placement
with its classification and original journal entries; every correction with who made it;
and pages the lots, placements, moves and corrections so a large migration stays usable.
Placements are corrected in bulk from the same table.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from celerp.models.company import User
from celerp.models.ledger import LedgerEntry
from sqlalchemy import select
from test_older_stock_correction import _correct, _short_placement
from test_older_stock_placement import (
    _PLACED,
    _VIEW,
    _cookies,
    _move,
    _notices,
    _older_company,
    _tab,
    _view,
    ui_client,  # noqa: F401  (fixture)
)
from test_posting_roles_rollout import _startup

pytestmark = pytest.mark.asyncio


async def test_the_seller_is_told_existing_inventory_is_ready(session, client, auth):
    await _older_company(session, client, auth, [10.0, 20.0, 30.0])
    await _startup(session)

    (notice,) = await _notices(session, auth, _PLACED)

    assert notice.title == "Existing inventory is ready"
    assert notice.body == ("Celerp assigned inventory accounts to 3 older stock items. "
                           "Sales and shipments can continue normally.")
    assert notice.i18n["params"] == {"count": 3}


async def test_lots_placements_and_moves_are_paged_with_their_totals(session, client, auth):
    lots = await _older_company(session, client, auth, [10.0, 20.0, 30.0])
    await _startup(session)
    for lot in lots[:2]:
        assert (await _move(client, auth, [lot], "1130-P")).status_code == 200

    first = await _view(client, auth, limit=2)
    rest = await _view(client, auth, limit=2, lots_offset=2, moves_offset=1, placements_offset=1)

    assert first["totals"] == {"lots": 3, "placements": 1, "moves": 2, "corrections": 0}
    assert first["limit"] == 2
    assert len(first["lots"]) == 2 and len(rest["lots"]) == 1
    assert {lot["item_id"] for lot in first["lots"] + rest["lots"]} == set(lots)
    assert len(first["placements"]) == 1 and rest["placements"] == []
    assert len(first["moves"]) == 2 and len(rest["moves"]) == 1
    assert rest["moves"][0]["move_id"] == first["moves"][1]["move_id"]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"lots_offset": -1},
                                    {"placements_offset": "x"}])
async def test_a_page_outside_the_allowed_range_is_refused(client, auth, params):
    r = await client.get("/accounting/older-stock", headers=auth["headers"], params=params)

    assert r.status_code == 422, r.text


async def test_the_summary_shows_each_account_with_its_lots_and_differences(session, client, auth):
    await _short_placement(session, client, auth)

    view = await _view(client, auth)

    assert view["summary"] == [{"account": "1130-OB", "lots": 1, "value": 30.0, "transferred": 0.0,
                                "recognized": 10.0, "unresolved": 0.0}]


async def test_corrections_are_listed_newest_first_with_who_made_them(session, client, auth):
    _, placement = await _short_placement(session, client, auth)
    pid = placement["pid"]
    assert (await _correct(client, auth, "unresolve", pid)).status_code == 200
    assert (await _correct(client, auth, "recognize", pid)).status_code == 200
    (name,) = (await session.execute(select(User.name).join(LedgerEntry, LedgerEntry.actor_id == User.id).where(
        LedgerEntry.company_id == auth["company_id"], LedgerEntry.event_type == "sys.older_stock.corrected")
        .distinct())).scalars().all()

    view = await _view(client, auth)

    assert [(c["kind"], c["amount"], c["journal"]) for c in view["corrections"]] == [
        ("recognize", 10.0, f"je:auto:older-stock-placed:{pid}:correction:2"),
        ("unresolve", -10.0, f"je:auto:older-stock-placed:{pid}:correction:1")]
    assert {c["placement"] for c in view["corrections"]} == {pid}
    assert {c["actor_name"] for c in view["corrections"]} == {name}
    assert view["totals"]["corrections"] == 2


# --- Settings > Accounting > Older stock -----------------------------------------------


_PAGED = {**_VIEW,
          "summary": [{"account": "1130-OB", "lots": 2, "value": 1264.5, "transferred": 0.0, "recognized": 10.0,
                       "unresolved": -4.25}],
          "corrections": [{"placement": "p1", "day": "2026-10-15", "kind": "unresolve", "amount": -10.0,
                           "journal": "je:auto:older-stock-placed:p1:correction:1", "actor_name": "Owner A"}],
          "totals": {"lots": 120, "placements": 1, "moves": 1, "corrections": 1}, "limit": 50}


async def test_the_tab_shows_the_accountant_view_with_pagers_and_bulk_correction(ui_client):
    body, get = await _tab(ui_client, _PAGED, query="&account=1130-OB&lots_offset=50&limit=50")

    assert get.await_args.args[1] == {"account": "1130-OB", "lots_offset": "50", "limit": "50"}
    assert 'id="older-stock-summary"' in body and "-$4.25" in body and "$1,264.50" in body
    assert 'class="pagination"' in body and "lots_offset=100" in body and "account=1130-OB" in body
    assert "51-100 of 120" in body
    assert 'id="bulkbar-older-stock-placements"' in body
    assert "/settings/accounting/older-stock/corrections?" in body and "kind=recognize" in body
    assert "kind=unresolve" in body and 'value="p1"' in body
    assert "Opening stock" in body and "je:auto:older-stock-placed:p1:opening" in body
    assert 'id="older-stock-corrections"' in body and "Owner A" in body
    assert "je:auto:older-stock-placed:p1:correction:1" in body


_RETAINED = {"code": "3200", "name": "Retained Earnings", "account_type": "equity", "code_generated": False}
_SURPLUS = {"pid": "p2", "day": "2026-10-15", "count": 1, "account": "1130-P", "classification": "unresolved",
            "transferred": 0.0, "recognized": 0.0, "unresolved": 30000.0, "journal": [], "corrections": []}


async def test_a_surplus_shows_as_a_plain_line_with_a_write_off_that_names_its_account(ui_client):
    written_off = {**_SURPLUS, "pid": "p3", "recognized": -500.0, "unresolved": 0.0}
    view = {**_PAGED, "placements": [_SURPLUS, written_off], "retained": _RETAINED,
            "corrections": [{"placement": "p3", "day": "2026-10-15", "kind": "write_off", "amount": -500.0,
                             "journal": "je:auto:older-stock-placed:p3:correction:1", "actor_name": "Owner A"}]}

    body, _ = await _tab(ui_client, view)

    assert "$30,000.00 more on the books than the stock" in body
    assert ">Write off<" in body and "Undo write-off" in body
    assert ("Write off $30,000.00 against 3200 Retained Earnings as a prior-period correction? "
            "You can undo it.") in body
    assert "Written off against retained earnings" in body
    assert "Book against retained earnings" not in body.split('id="older-stock-placements"')[1].split("</table>")[0]


async def test_the_tab_ignores_a_page_it_cannot_read(ui_client):
    _, get = await _tab(ui_client, _PAGED, query="&lots_offset=-3&limit=abc&moves_offset=2")

    assert get.await_args.args[1] == {"moves_offset": "2"}


async def test_the_bulk_correction_posts_the_ticked_placements(ui_client):
    correct = AsyncMock(return_value={**_PAGED, "result": {"corrected": 1}})
    ui_client.cookies.update(_cookies())
    with patch("ui.api_client.correct_older_stock", new=correct), \
         patch("ui.api_client.get_older_stock", new=AsyncMock(return_value=_PAGED)):
        r = await ui_client.post("/settings/accounting/older-stock/corrections?kind=recognize&lots_offset=50", data={"selected": ["p1", "p1", "p2"]})

    assert r.status_code == 200
    assert correct.await_args.args[1:] == (["p1", "p2"], "recognize")
    assert "Correction posted." in r.content.decode()

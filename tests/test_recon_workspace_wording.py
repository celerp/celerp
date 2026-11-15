"""The bank page answers every button: Post ticked with nothing ticked says so instead of
sitting greyed out, and the open statement's difference is spelled out."""
from __future__ import annotations

from html import escape

import pytest

import bank_statement_helpers as B
from test_helpers import ui_get, ui_request
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def test_post_ticked_with_nothing_ticked_says_why(client):
    co = await B.company(client)
    bank = await B.bank(client, co["h"], name="Wording Bank")
    await B.statement(client, co, bank["id"], [("2026-09-03", "Coffee", "-12.00")],
                      opening="0.00", closing="-12.00", start="2026-09-01", end="2026-09-30")
    page = await ui_get(co["h"], f"/accounting/reconcile/bank/{bank['id']}")
    button = page.text.split(f'hx-post="/accounting/reconcile/bank/{bank["id"]}/post-ticked"', 1)[1].split(">", 1)[0]
    assert "disabled" not in button
    assert escape(t("bank.difference", date="", amount=""), quote=False).split("{")[0].split(":")[0] in page.text
    r = await ui_request(co["h"], "POST", f"/accounting/reconcile/bank/{bank['id']}/post-ticked",
                         data={"idempotency_key": B.key()})
    assert escape(t("bank.nothing_ticked"), quote=False) in r.text

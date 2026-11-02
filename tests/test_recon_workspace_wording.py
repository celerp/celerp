"""The reconciliation workspace answers every button: Confirm Suggestions with nothing suggested
says so instead of sitting greyed out, and the difference is spelled out."""
from __future__ import annotations

from html import escape

import pytest

from test_helpers import ui_get, ui_request
from test_reconciliation_history import OPENING, admin, bank, start
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def test_confirm_suggestions_with_none_suggested_says_why(client):
    _, h = await admin(client)
    b = await bank(client, h)
    sid = (await start(client, h, b["id"], "2026-09-30", OPENING)).json()["id"]
    page = await ui_get(h, f"/accounting/reconcile/{sid}")
    button = page.text.split(f'hx-post="/accounting/reconcile/{sid}/bulk-confirm"', 1)[1].split(">", 1)[0]
    assert "disabled" not in button
    assert escape(t("recon.difference", amount=""), quote=False).split("{")[0] in page.text
    r = await ui_request(h, "POST", f"/accounting/reconcile/{sid}/bulk-confirm")
    assert escape(t("recon.no_suggestions_to_confirm"), quote=False) in r.text

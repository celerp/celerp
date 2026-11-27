# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The dashboard banner for the tax codes upgrade notice (S32)."""
from __future__ import annotations

from fasthtml.common import to_xml
from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)

from ui.routes.dashboard import _tax_upgrade_banner

NOTICE = {"country_missing": True, "codes_without_category": 3, "unmapped_roles": ["tax_withheld_payable"],
          "bills_awaiting_correction": 1, "unmatched_item_tax_codes": 0}


def test_banner_links_each_outcome_left_to_check():
    html = to_xml(_tax_upgrade_banner({"upgrade_notice": NOTICE}, "admin"))
    for href in ("/settings/general?tab=company", "/settings/sales?tab=taxes",
                 "/settings/accounting?tab=posting-accounts", "/settings/taxes/withholding-corrections"):
        assert f'href="{href}"' in html
    assert 'href="/inventory"' not in html  # a zero count is not listed
    assert "3 tax codes have no category" in html and "1 tax accounts are not mapped" in html
    assert 'hx-post="/settings/taxes/upgrade-notice/dismiss"' in html


def test_banner_hidden_when_dismissed_empty_or_below_admin():
    zero = {k: (False if v is True else [] if isinstance(v, list) else 0) for k, v in NOTICE.items()}
    assert _tax_upgrade_banner({"upgrade_notice": NOTICE, "upgrade_notice_dismissed": True}, "admin") == ""
    assert _tax_upgrade_banner({"upgrade_notice": zero}, "admin") == ""
    assert _tax_upgrade_banner({}, "admin") == ""
    assert _tax_upgrade_banner({"upgrade_notice": NOTICE}, "operator") == ""


async def test_dismiss_closes_the_banner_or_shows_the_refusal_in_its_place(ui_client):
    from unittest.mock import AsyncMock, patch

    from ui.api_client import APIError

    url = "/settings/taxes/upgrade-notice/dismiss"
    dismiss = AsyncMock(return_value={"ok": True})
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.dismiss_tax_upgrade_notice", new=dismiss):
        closed = await ui_client.post(url)
        with patch("ui.api_client.dismiss_tax_upgrade_notice", new=AsyncMock(side_effect=APIError(403, "Admins only"))):
            refused = await ui_client.post(url)
    dismiss.assert_awaited_once()
    assert closed.status_code == 200 and closed.text == ""
    assert 'id="tax-upgrade-banner"' in refused.text and "Admins only" in refused.text

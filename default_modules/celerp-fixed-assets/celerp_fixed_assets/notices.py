# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The standing notices fixed assets tells a company: each one holds while its condition
does and is removed when the condition clears (notification_service.notify_standing)."""
from __future__ import annotations

CATEGORY = "fixed_assets"
RUNS_URL = "/fixed-assets/runs"
CORRECTIONS_URL = "/fixed-assets/corrections"

# Title (stored in English, as every notice is) and message key of each notice.
READY = ("Depreciation ready to post", "fixed_assets.notice.ready")
PAUSED = ("Automatic depreciation posting paused", "fixed_assets.notice.paused")
NO_USER = ("Automatic depreciation posting needs a user", "fixed_assets.notice.no_user")
ATTENTION = ("Assets held out of depreciation", "fixed_assets.notice.attention")
CORRECTIONS = ("Corrections waiting", "fixed_assets.notice.corrections")
REVIEWS = ("Impairment reviews due", "fixed_assets.notice.reviews")


async def tell(session, company_id, notice: tuple[str, str], body: str, *, action_url: str = RUNS_URL,
               **params) -> None:
    from celerp.notifications import service

    title, key = notice
    await service.notify_standing(session, company_id, CATEGORY, title, body, action_url=action_url,
                                  i18n={"title": f"{key}.title", "body": f"{key}.body", "params": params})


async def clear(session, company_id, *notices: tuple[str, str]) -> None:
    from celerp.notifications import service

    for title, _ in notices:
        await service.clear_standing(session, company_id, CATEGORY, title)

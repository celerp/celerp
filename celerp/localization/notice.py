# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Tell every company, at each startup, when this installation lacks country tax rates.

One standing bell notice per company, kept current in place: it names the missing packs (or
the unreadable lock) and the next step, and is removed once every pack loads again. The notice
keeps the country codes and is read with their names in the reader's language.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.localization import lock_codes, missing
from celerp.notifications.service import clear_every_company, notify_every_company
from celerp.tax_regimes import country_names
from ui.i18n import t

_CATEGORY = "settings"
_TITLE = "l10n.pack_missing.title"


async def sync_pack_notice(session: AsyncSession) -> None:
    """Raise, update or clear the missing-pack notice for the current pack state. Caller commits."""
    title = t(_TITLE, "en")
    if lock_codes() is None:
        body, params = "l10n.pack_lock_missing.body", {}
    elif codes := missing():
        body, params = "l10n.pack_missing.body", {"countries": list(codes)}
    else:
        await clear_every_company(session, _CATEGORY, title)
        return
    english = {"countries": country_names(codes, "en")} if params else {}
    await notify_every_company(session, _CATEGORY, title, t(body, "en", **english),
                               i18n={"title": _TITLE, "body": body, "params": params})

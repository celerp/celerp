# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Tell companies when this installation lacks country tax rates.

One standing bell notice per company, kept current in place: it names the missing packs (or
the unreadable lock) and the next step, and is removed once every pack loads again. Every
company is brought up to date at each startup; a company created later is told when it is
provisioned. The notice keeps the country codes and is read with their names in the reader's
language.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.localization import lock_codes, missing
from celerp.notifications import service as notification_service
from celerp.tax_regimes import country_names
from ui.i18n import t

_CATEGORY = "settings"
_TITLE = "l10n.pack_missing.title"


async def sync_pack_notice(session: AsyncSession, company_id: uuid.UUID | None = None) -> None:
    """Raise, update or clear the missing-pack notice for the current pack state, for
    ``company_id`` alone or, without it, for every company. Caller commits."""
    title = t(_TITLE, "en")
    if lock_codes() is None:
        body, params = "l10n.pack_lock_missing.body", {}
    elif codes := missing():
        body, params = "l10n.pack_missing.body", {"countries": list(codes)}
    elif company_id is None:
        await notification_service.clear_every_company(session, _CATEGORY, title)
        return
    else:
        await notification_service.clear_standing(session, company_id, _CATEGORY, title)
        return
    english = {"countries": country_names(codes, "en")} if params else {}
    text, i18n = t(body, "en", **english), {"title": _TITLE, "body": body, "params": params}
    if company_id is None:
        await notification_service.notify_every_company(session, _CATEGORY, title, text, i18n=i18n)
    else:
        await notification_service.notify_standing(session, company_id, _CATEGORY, title, text, i18n=i18n)

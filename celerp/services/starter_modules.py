# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The modules that keep registration's starter records.

Registration gives a new company starter items (Inventory) and its own contact (Contacts),
and a record is written only through the module that applies it, so both modules are
enabled before the first company registers. An older release wrote those records with no
module enabled; the first start of this release on such an install enables the two modules
for it and tells each company holding the records. This happens once: a module the owner
turns off afterwards stays off.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

STARTER_MODULES = ("celerp-inventory", "celerp-contacts")
STARTER_MODULES_KEY = "starter_modules_enabled"
NOTICE_TITLE = "Inventory and Contacts were turned on"
_NOTICE_BODY = ("This company's starter items and its own contact, created when it was registered, "
                "are kept by Inventory and Contacts, so both were turned on. They are listed on the "
                "Modules page.")
# The keys registration's starter records are written under (demo.seed_demo_items,
# demo.seed_self_contacts, and the two self-contacts of releases before those were one).
_STARTER_RECORD_KEYS = ("demo:item:%", "reg:contact:%")


def with_starter_modules(company_settings: dict | None) -> dict:
    """Company settings with the starter modules recorded as enabled."""
    from celerp.modules.registry import get_enabled, set_enabled

    settings = company_settings or {}
    return set_enabled(settings, get_enabled(settings) | set(STARTER_MODULES))


async def enable_starter_modules(session: AsyncSession) -> None:
    """Once per install, before the modules load: enable the starter modules when no company
    is registered yet or a company holds starter records, and tell each such company when
    that turned them on. The caller commits."""
    from celerp.config import set_enabled_modules
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.company import Company
    from celerp.models.ledger import LedgerEntry
    from celerp.notifications.service import create as notify
    from celerp.services.company_lock import locked_company

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, STARTER_MODULES_KEY)) == "done":
        return
    holders = list((await session.execute(select(LedgerEntry.company_id).where(
        or_(*(LedgerEntry.idempotency_key.like(k) for k in _STARTER_RECORD_KEYS))).distinct())).scalars())
    registered = (await session.execute(select(Company.id).limit(1))).first() is not None
    if holders or not registered:
        turned_on = await asyncio.to_thread(set_enabled_modules, list(STARTER_MODULES))
        for company_id in holders:
            company = await locked_company(session, company_id)
            company.settings = with_starter_modules(company.settings)
            if turned_on:
                await notify(session, company_id, "system", NOTICE_TITLE, _NOTICE_BODY,
                             action_url="/modules", priority="high")
    await conn.run_sync(lambda c: set_meta(c, STARTER_MODULES_KEY, "done"))

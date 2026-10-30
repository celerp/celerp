# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The modules that keep registration's starter records.

Registration gives a new company starter items (Inventory) and its own contact (Contacts),
and a record is written only through the module that applies it, so both modules are
enabled for the install before the first company registers. An older release wrote those
records with no module enabled; the first start of this release on such an install enables
the two modules for it and tells each company holding the records. This happens once: a module the owner
turns off afterwards stays off.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

STARTER_MODULES = ("celerp-inventory", "celerp-contacts")
STARTER_MODULES_KEY = "starter_modules_enabled"
# The marker while the companies' change has committed and the config file is still to be
# confirmed: a start that finds it adds whichever starter module the file lacks.
_FILE_PENDING = "file_pending"
NOTICE_TITLE = "Inventory and Contacts were turned on"
_NOTICE_BODY = ("This company's starter items and its own contact, created when it was registered, "
                "are kept by Inventory and Contacts, so both were turned on. They are listed on the "
                "Modules page.")
# The keys registration's starter records are written under (demo.seed_demo_items,
# demo.seed_self_contacts, and the two self-contacts of releases before those were one).
_STARTER_RECORD_PREFIXES = ("demo:item:", "reg:contact:")


async def enable_starter_modules(session: AsyncSession) -> None:
    """Once per install, before the modules load: turn the starter modules on when no company
    is registered yet or a company holds starter records, and tell each such company when
    that turned them on. A company that lists no modules of its own uses the installation's
    list, so the modules are added there; a company with its own list has them added to it.
    It all happens in one transaction under the module-state boundary, so no other module
    change lands in between: the installation's list is read, the config file is written, and
    only then do the companies' lists, the notices and the marker commit. A write that fails
    leaves everything as it was and the next start tries again; a commit that fails puts the
    file back as it was read. A commit can report a failure after it landed, so when the file
    was written the marker says so until a second commit confirms the first: a start that
    finds it puts the starter modules back in the file."""
    import functools

    from celerp.config import read_config, replace_enabled_modules
    from celerp.migrations._data_reconcile import get_meta, set_meta
    from celerp.models.company import Company
    from celerp.models.ledger import LedgerEntry
    from celerp.modules.boundary import take_company_locks
    from celerp.modules.registry import (
        commit_with_load_set, company_modules, enable_for_company, hold_module_state, uses_own_list,
    )
    from celerp.notifications.service import create as notify
    from celerp.services.company_lock import locked_company

    conn = await session.connection()
    if await conn.run_sync(lambda c: get_meta(c, STARTER_MODULES_KEY)) == "done":
        return
    await hold_module_state(session)
    marker = await conn.run_sync(lambda c: get_meta(c, STARTER_MODULES_KEY))
    if marker == "done":
        await session.commit()
        return
    if marker == _FILE_PENDING:
        installed = list(read_config().get("modules", {}).get("enabled") or [])
        missing = [m for m in STARTER_MODULES if m not in installed]
        if missing:
            await asyncio.to_thread(replace_enabled_modules, installed + missing)
        await conn.run_sync(lambda c: set_meta(c, STARTER_MODULES_KEY, "done"))
        await session.commit()
        return
    holder_ids = list((await session.execute(select(LedgerEntry.company_id).where(
        or_(*(LedgerEntry.idempotency_key.startswith(k, autoescape=True) for k in _STARTER_RECORD_PREFIXES))).distinct())).scalars())
    holders = [c for c in await take_company_locks(
        session, *(functools.partial(locked_company, session, cid) for cid in sorted(holder_ids, key=str)))
        if c is not None]
    registered = (await session.execute(select(Company.id).limit(1))).first() is not None
    installed = list(read_config().get("modules", {}).get("enabled") or [])
    add_to_installation = not registered or any(not uses_own_list(c.settings) for c in holders)
    turned_on = ([c for c in holders if not uses_own_list(c.settings)]
                 if add_to_installation and not set(STARTER_MODULES) <= set(installed) else [])
    own_lists = [c for c in holders if uses_own_list(c.settings)
                 and not set(STARTER_MODULES) <= company_modules(c.settings)]
    for company in own_lists:
        settings = company.settings
        for name in STARTER_MODULES:
            settings, _added = enable_for_company(settings, name)
        company.settings = settings
    for company in turned_on + own_lists:
        await notify(session, company.id, "system", NOTICE_TITLE, _NOTICE_BODY,
                     action_url="/modules", priority="high",
                     i18n={"title": "notice.starter_modules_on.title", "body": "notice.starter_modules_on.body"})
    await conn.run_sync(lambda c: set_meta(c, STARTER_MODULES_KEY, _FILE_PENDING if add_to_installation else "done"))
    if add_to_installation:
        await asyncio.to_thread(replace_enabled_modules, installed + list(STARTER_MODULES))
    try:
        await session.commit()
    except BaseException:
        if add_to_installation:
            await asyncio.to_thread(replace_enabled_modules, installed)
        raise
    if add_to_installation:
        await (await session.connection()).run_sync(lambda c: set_meta(c, STARTER_MODULES_KEY, "done"))
    if own_lists:
        await commit_with_load_set(session)
    else:
        await session.commit()

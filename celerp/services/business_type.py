# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The single operation that changes a company's business type (settings["vertical"]).

Setting a type is additive and safe to repeat:
  * the type's modules are added to the installation config and company settings,
    never removed;
  * missing category schemas are added, existing ones are left alone;
  * preset settings, payment terms and T&C move to the new type only while they
    still hold the system value;
  * demo items are swapped only when the type actually changes, and only
    demo-sourced rows are touched.

Module config lives in a file, company state in the database, so the two cannot
commit together. The config step runs first and is idempotent, and the restart
need is derived from what is actually running, so a retry after a failure in
between still reports that a restart is required.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import set_enabled_modules
from celerp.models.company import Company
from celerp.modules.loader import is_running
from celerp.modules.registry import enable as enable_in_settings
from celerp.services.demo import reconcile_vertical_defaults, replace_demo_items
from celerp.services.vertical_presets import (
    installed_preset_modules,
    load_preset,
    merge_missing_preset_categories,
    reconcile_preset_settings,
)


class UnknownBusinessType(ValueError):
    """The requested business type is not one of the offered presets."""


async def set_business_type(
    session: AsyncSession,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    vertical: str,
) -> dict:
    target = load_preset(vertical)
    if target is None:
        raise UnknownBusinessType(vertical)
    modules = installed_preset_modules(target)
    await asyncio.to_thread(set_enabled_modules, modules)

    company = (await session.execute(
        select(Company).where(Company.id == company_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    settings = dict(company.settings or {})
    previous_vertical = settings.get("vertical")
    previous_preset = load_preset(previous_vertical, allow_hidden=True) if previous_vertical else None

    for name in modules:
        settings = enable_in_settings(settings, name)
    settings, _categories = merge_missing_preset_categories(settings, target)
    settings = reconcile_preset_settings(settings, previous_preset, target)
    settings = reconcile_vertical_defaults(settings, previous_vertical, vertical)
    settings["vertical"] = vertical
    company.settings = settings

    changed = previous_vertical != vertical
    if changed:
        await replace_demo_items(session, company_id, actor_id, vertical)
    await session.commit()
    return {
        "vertical": vertical,
        "changed": changed,
        "modules": modules,
        "restart_required": any(not is_running(name) for name in modules),
    }

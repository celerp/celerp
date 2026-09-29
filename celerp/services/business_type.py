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
    demo items the user has not edited or used are replaced.

The result carries a summary of every change made on the user's behalf so the
caller can show it.

Module config lives in a file, company state in the database, so the two cannot
commit together. The config step runs first and is idempotent, and the restart
need is derived from what is actually running (and what a restart could load),
so a retry after a failure in between still reports that a restart is required.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import set_enabled_modules
from celerp.models.company import Company
from celerp.modules.loader import is_running, module_label, restart_would_load
from celerp.modules.registry import enable as enable_in_settings, get_enabled
from celerp.services.demo import reconcile_vertical_defaults, replace_demo_items
from celerp.services.vertical_presets import (
    installed_preset_modules,
    load_preset,
    merge_missing_preset_categories,
    reconcile_preset_settings,
)


class UnknownBusinessType(ValueError):
    """The requested business type is not one of the offered presets."""


def _changed_keys(before: dict, after: dict) -> list[str]:
    return sorted(k for k in {*before, *after} if before.get(k) != after.get(k))


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
    before = dict(company.settings or {})
    previous_vertical = before.get("vertical")
    previous_preset = load_preset(previous_vertical, allow_hidden=True) if previous_vertical else None

    with_modules = before
    for name in modules:
        with_modules = enable_in_settings(with_modules, name)
    with_categories, _categories = merge_missing_preset_categories(with_modules, target)
    with_presets = reconcile_preset_settings(with_categories, previous_preset, target)
    with_defaults = reconcile_vertical_defaults(with_presets, previous_vertical, vertical)
    company.settings = {**with_defaults, "vertical": vertical}

    changed = previous_vertical != vertical
    demo = {"replaced": 0, "kept": 0}
    if changed:
        demo = await replace_demo_items(session, company_id, actor_id, vertical)
    await session.commit()

    old_schemas = before.get("category_schemas") or {}
    labels = with_categories.get("category_display_names") or {}
    enabled_before = get_enabled(before)
    return {
        "vertical": vertical,
        "changed": changed,
        "modules": modules,
        "restart_required": any(not is_running(n) and restart_would_load(n) for n in modules),
        "changes": {
            "categories_added": [
                labels.get(k, k) for k in with_categories["category_schemas"] if k not in old_schemas
            ],
            "modules_enabled": [module_label(n) for n in modules if n not in enabled_before],
            "settings_updated": _changed_keys(with_categories, with_presets),
            "defaults_updated": _changed_keys(with_presets, with_defaults),
            "demo_items_replaced": demo["replaced"],
            "demo_items_kept": demo["kept"],
        },
    }

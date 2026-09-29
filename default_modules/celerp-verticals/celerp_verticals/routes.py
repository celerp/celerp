# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-verticals API routes.

The preset and category library is read and merged by the core
celerp.services.vertical_presets service; these routes are the additive
Inventory > Categories operations on top of it. Applying a preset here never
changes the company's business type.

Endpoints:
  GET  /companies/verticals/categories          list all categories in the library
  GET  /companies/verticals/categories/{name}   single category definition
  GET  /companies/verticals/presets             list the offered presets
  POST /companies/me/apply-preset               add a preset's missing categories and modules
  POST /companies/me/apply-category             apply a single category schema
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import set_enabled_modules
from celerp.db import get_session
from celerp.models.company import Company
from celerp.modules.registry import enable as enable_in_settings
from celerp.services.auth import get_current_company_id, get_current_user
from celerp.services.permissions import require_permission
from celerp.services.vertical_presets import (
    installed_preset_modules,
    list_categories as _list_categories,
    list_presets as _list_presets,
    load_category,
    load_preset,
    merge_missing_preset_categories,
    seed_category_units,
)


async def _company(session: AsyncSession, company_id) -> Company:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _build_router() -> APIRouter:
    router = APIRouter()
    read_deps = [Depends(get_current_user)]
    write_deps = [Depends(get_current_user), require_permission("manage_company_settings")]

    @router.get("/verticals/categories", dependencies=read_deps)
    async def list_categories() -> list[dict]:
        return [
            {
                "name": c["name"],
                "display_name": c["display_name"],
                "vertical_tags": c.get("vertical_tags", []),
                "default_sell_by": c.get("default_sell_by"),
                "default_purchase_unit": c.get("default_purchase_unit"),
                "default_weight_unit": c.get("default_weight_unit"),
            }
            for c in _list_categories()
        ]

    @router.get("/verticals/categories/{name}", dependencies=read_deps)
    async def get_category(name: str) -> dict:
        cat = load_category(name)
        if cat is None:
            raise HTTPException(status_code=404, detail=f"Category '{name}' not found")
        return cat

    @router.get("/verticals/presets", dependencies=read_deps)
    async def list_presets() -> list[dict]:
        return [
            {"name": p["name"], "display_name": p["display_name"], "categories": p.get("categories", [])}
            for p in _list_presets()
        ]

    @router.post("/me/apply-preset", dependencies=write_deps)
    async def apply_preset(
        vertical: str,
        company_id=Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        preset = load_preset(vertical)
        if preset is None:
            raise HTTPException(status_code=404, detail=f"Preset '{vertical}' not found")
        modules = installed_preset_modules(preset)

        company = await _company(session, company_id)
        settings, categories = merge_missing_preset_categories(dict(company.settings or {}), preset)
        for name in modules:
            settings = enable_in_settings(settings, name)
        # Additive: a setting the company already has is never overwritten.
        extra = {k: v for k, v in (preset.get("company_settings") or {}).items() if k not in settings}
        settings.update(extra)
        company.settings = settings
        await session.commit()

        # Write to config file so the next restart picks up the module list
        await asyncio.to_thread(set_enabled_modules, modules)

        return {"applied": vertical, "categories": len(categories), "modules": modules, "company_settings": extra}

    @router.post("/me/apply-category", dependencies=write_deps)
    async def apply_category(
        name: str,
        company_id=Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        cat = load_category(name)
        if cat is None:
            raise HTTPException(status_code=404, detail=f"Category '{name}' not found")
        company = await _company(session, company_id)
        settings = dict(company.settings or {})
        settings["category_schemas"] = {**(settings.get("category_schemas") or {}), cat["name"]: cat["fields"]}
        settings["category_display_names"] = {
            **(settings.get("category_display_names") or {}), cat["name"]: cat["display_name"],
        }
        seed_category_units(settings, cat)
        company.settings = settings
        await session.commit()
        return {"applied": name, "display_name": cat["display_name"]}

    return router


def setup_api_routes(app: FastAPI) -> None:
    app.include_router(_build_router(), prefix="/companies", tags=["verticals"])

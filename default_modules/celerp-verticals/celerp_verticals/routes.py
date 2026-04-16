# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""celerp-verticals API routes.

Endpoints:
  GET  /companies/verticals/categories          list all categories in the library
  GET  /companies/verticals/categories/{name}   single category definition
  GET  /companies/verticals/presets             list all presets
  POST /companies/me/apply-preset               apply a vertical preset (seeds category schemas)
  POST /companies/me/apply-category             apply a single category schema
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.company import Company
from celerp.services.auth import get_current_company_id, get_current_user, require_admin

_PRESETS_DIR = Path(__file__).parent / "presets"
_CATEGORIES_DIR = Path(__file__).parent / "categories"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _all_categories() -> dict[str, dict]:
    """Return {name: category_dict} for all categories on disk."""
    result: dict[str, dict] = {}
    if _CATEGORIES_DIR.exists():
        for p in sorted(_CATEGORIES_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text())
                result[data["name"]] = data
            except Exception:
                pass
    return result


def _load_category(name: str) -> dict:
    path = _CATEGORIES_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Category '{name}' not found")
    return json.loads(path.read_text())


def _load_preset(name: str) -> dict:
    path = _PRESETS_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Preset '{name}' not found")
    return json.loads(path.read_text())


def _all_presets() -> list[dict]:
    result = []
    if _PRESETS_DIR.exists():
        for p in sorted(_PRESETS_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text())
                result.append({
                    "name": data["name"],
                    "display_name": data["display_name"],
                    "categories": data.get("categories", []),
                })
            except Exception:
                pass
    return result


# ---------------------------------------------------------------------------
# Default units seed (kept in sync with celerp_inventory/routes.py)
# ---------------------------------------------------------------------------

_DEFAULT_UNITS: list[dict] = [
    {"name": "piece", "label": "Piece", "decimals": 0},
    {"name": "carat", "label": "Carat (ct)", "decimals": 2},
    {"name": "gram", "label": "Gram (g)", "decimals": 2},
    {"name": "kg", "label": "Kilogram (kg)", "decimals": 3},
    {"name": "oz", "label": "Ounce (oz)", "decimals": 2},
    {"name": "liter", "label": "Liter (L)", "decimals": 2},
    {"name": "meter", "label": "Meter (m)", "decimals": 2},
]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _apply_category_schema(
    session: AsyncSession,
    company_id,
    category_name: str,
    fields: list[dict],
) -> None:
    """Idempotently write a category field schema into company settings."""
    import uuid as _uuid
    cid = _uuid.UUID(str(company_id)) if isinstance(company_id, str) else company_id
    company = await session.get(Company, cid)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    settings = dict(company.settings or {})
    cat_schemas = dict(settings.get("category_schemas") or {})
    cat_schemas[category_name] = fields
    settings["category_schemas"] = cat_schemas
    company.settings = settings


def _ensure_unit_seeded(settings: dict, unit_name: str) -> None:
    """Add unit_name to company units from the default seed if not already present.

    Mutates settings in place. No-op if company already has custom units that include it,
    or if the unit is not in the default seed.
    """
    seed_by_name = {u["name"]: u for u in _DEFAULT_UNITS}
    if unit_name not in seed_by_name:
        return  # Unknown unit - nothing to seed
    current_units: list[dict] = list(settings.get("units") or _DEFAULT_UNITS)
    if not any(u["name"] == unit_name for u in current_units):
        current_units.append(seed_by_name[unit_name])
        settings["units"] = current_units


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def _build_router() -> APIRouter:
    router = APIRouter()
    read_deps = [Depends(get_current_user)]
    write_deps = [Depends(get_current_user), Depends(require_admin)]

    @router.get("/verticals/categories", dependencies=read_deps)
    async def list_categories() -> list[dict]:
        cats = _all_categories()
        return [
            {
                "name": c["name"],
                "display_name": c["display_name"],
                "vertical_tags": c.get("vertical_tags", []),
                "default_sell_by": c.get("default_sell_by"),
            }
            for c in cats.values()
        ]

    @router.get("/verticals/categories/{name}", dependencies=read_deps)
    async def get_category(name: str) -> dict:
        return _load_category(name)

    @router.get("/verticals/presets", dependencies=read_deps)
    async def list_presets() -> list[dict]:
        return _all_presets()

    @router.post("/me/apply-preset", dependencies=write_deps)
    async def apply_preset(
        vertical: str,
        company_id=Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        from celerp.modules.registry import enable as _registry_enable
        from celerp.config import set_enabled_modules
        import uuid as _uuid

        preset = _load_preset(vertical)
        cats = _all_categories()
        preset_modules: list[str] = preset.get("modules") or []

        # Apply category schemas + seed required units
        applied: list[str] = []
        for cat_name in (preset.get("categories") or []):
            cat = cats.get(cat_name)
            if cat is None:
                continue
            await _apply_category_schema(session, company_id, cat["display_name"], cat["fields"])
            applied.append(cat_name)

        # Enable declared modules in DB (company settings) + config file (survives restart)
        cid = _uuid.UUID(str(company_id)) if isinstance(company_id, str) else company_id
        company = await session.get(Company, cid)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        settings = dict(company.settings or {})
        for mod_name in preset_modules:
            settings = _registry_enable(settings, mod_name)

        # Seed units required by any applied category
        for cat_name in applied:
            cat = cats.get(cat_name)
            if cat:
                dsb = cat.get("default_sell_by")
                if dsb:
                    _ensure_unit_seeded(settings, dsb)

        # Apply any preset-level company settings (e.g. inventory_method)
        extra = preset.get("company_settings") or {}
        settings.update(extra)
        company.settings = settings

        await session.commit()

        # Write to config file so the next restart picks up the module list
        set_enabled_modules(preset_modules)

        return {"applied": vertical, "categories": len(applied), "modules": preset_modules, "company_settings": extra}

    @router.post("/me/apply-category", dependencies=write_deps)
    async def apply_category(
        name: str,
        company_id=Depends(get_current_company_id),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        import uuid as _uuid
        cat = _load_category(name)
        await _apply_category_schema(session, company_id, cat["display_name"], cat["fields"])

        # Seed the default_sell_by unit if needed
        cid = _uuid.UUID(str(company_id)) if isinstance(company_id, str) else company_id
        company = await session.get(Company, cid)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        settings = dict(company.settings or {})
        dsb = cat.get("default_sell_by")
        if dsb:
            _ensure_unit_seeded(settings, dsb)
        company.settings = settings

        await session.commit()
        return {"applied": name, "display_name": cat["display_name"]}

    return router


def setup_api_routes(app: FastAPI) -> None:
    app.include_router(_build_router(), prefix="/companies", tags=["verticals"])

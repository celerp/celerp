# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.company import Company, Location, User
from celerp.models.accounting import UserCompany
from celerp.services.auth import create_access_token, create_refresh_token, get_current_company_id, get_current_user, get_current_role, hash_password, require_admin, ROLE_LEVELS
from celerp.tax_regimes import get_regime, TAX_REGIMES

router = APIRouter(dependencies=[Depends(get_current_user)])

logger = logging.getLogger(__name__)

_DEFAULT_TAX_NAMES = {t["name"] for t in TAX_REGIMES["_default"]["taxes"]}


async def _maybe_apply_regime(session: AsyncSession, company_id, address: dict | None) -> None:
    """Re-seed taxes and currency from the country in address if:
    - address has a non-empty 'country' key
    - company taxes are still at the generic _default (not yet customised)

    Safe to call multiple times — no-op if already customised.
    """
    if not address:
        return
    country = str(address.get("country") or "").strip()
    if not country:
        return

    company = await session.get(Company, company_id)
    if company is None:
        return

    current_taxes = company.settings.get("taxes") or []
    current_names = {t.get("name") for t in current_taxes}

    # Only re-seed if taxes are empty or still match the generic _default set
    if current_taxes and not current_names.issubset(_DEFAULT_TAX_NAMES | {""}):
        return  # user has customised — don't overwrite

    regime = get_regime(country)
    settings = dict(company.settings)
    settings["taxes"] = regime["taxes"]
    settings["currency"] = regime["currency"]
    company.settings = settings


class CompanyPatch(BaseModel):
    name: str | None = None
    settings: dict = Field(default_factory=dict)


class LocationCreate(BaseModel):
    name: str
    type: str
    address: dict | None = None
    is_default: bool = False


class LocationPatch(BaseModel):
    name: str | None = None
    address: dict | None = None
    type: str | None = None
    is_default: bool | None = None


class UserCreate(BaseModel):
    email: str
    name: str
    role: str = "user"
    password: str


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None  # if set, re-hashes


class ItemSchemaField(BaseModel):
    key: str
    label: str
    type: str  # text|number|money|select|date|boolean|weight|status|image
    editable: bool = True
    required: bool = False
    options: list[str] = Field(default_factory=list)
    visible_to_roles: list[str] = Field(default_factory=list)  # empty = all roles
    position: float = 0
    show_in_table: bool = True  # False = hidden in list view by default


class ItemSchemaPatch(BaseModel):
    fields: list[ItemSchemaField]


class CategorySchemaPatch(BaseModel):
    fields: list[ItemSchemaField]

    @model_validator(mode="after")
    def _unique_keys(self) -> "CategorySchemaPatch":
        keys = [f.key for f in self.fields]
        seen: set[str] = set()
        dupes = [k for k in keys if k in seen or seen.add(k)]  # type: ignore[func-returns-value]
        if dupes:
            raise ValueError(f"Duplicate field keys: {', '.join(sorted(set(dupes)))}")
        return self


class ColumnPrefsPatch(BaseModel):
    # key = category name or "__all__"; value = list of visible column keys
    prefs: dict[str, list[str]]


class TaxRate(BaseModel):
    name: str
    rate: float  # percentage, e.g. 7.0
    tax_type: str = "both"  # sales|purchase|both
    is_default: bool = False
    description: str = ""
    is_compound: bool = False
    default_order: int = 0


class TaxRatesPatch(BaseModel):
    taxes: list[TaxRate]


class PaymentTermsPatch(BaseModel):
    terms: list[dict]


class ContactTagsPatch(BaseModel):
    tags: list[dict]  # Each: {name: str, color: str|None, category: str|None}


class ContactDefaultsPatch(BaseModel):
    defaults: dict


class TermsConditionsPatch(BaseModel):
    templates: list[dict]


class SettingsImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class SettingsBatchImportRequest(BaseModel):
    records: list[SettingsImportRecord]


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]


class UnitRecord(BaseModel):
    name: str
    label: str
    decimals: int
    unit_type: str = "quantity"  # "weight" | "pieces" | "quantity"


class UnitsPatch(BaseModel):
    units: list[UnitRecord]


class CompanyCreate(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Company profile
# ---------------------------------------------------------------------------

@router.post("")
async def create_company(
    payload: CompanyCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a new company linked to the current user. Returns JWT scoped to new company."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", payload.name.strip().lower()).strip("-") or str(uuid.uuid4())
    company = Company(id=uuid.uuid4(), name=payload.name, slug=slug, settings={})
    link = UserCompany(id=uuid.uuid4(), user_id=user.id, company_id=company.id, role="owner")
    session.add(company)
    await session.flush()  # persist company row before FK reference in user_companies
    session.add(link)
    await session.flush()
    # Fire module lifecycle hooks (e.g. celerp-accounting seeds chart of accounts)
    from celerp.modules.slots import fire_lifecycle
    await fire_lifecycle("on_company_created", session=session, company_id=company.id)
    # Seed the company's single self-contact (typed `both`, mirrors registration flow)
    from celerp.services.demo import seed_self_contacts
    await seed_self_contacts(
        session,
        company_id=company.id,
        actor_id=user.id,
        person_name=user.name,
        company_name=payload.name,
        email=user.email,
    )
    # Seed a default "Head Office" location (mirrors registration flow)
    head_office = Location(
        id=uuid.uuid4(),
        company_id=company.id,
        name="Head Office",
        type="office",
        address=None,
        is_default=True,
    )
    session.add(head_office)
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.error("create_company failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=f"Could not create company: {e}") from e
    from celerp.services.session_tracker import register_token as _reg_token, get_nonce as _get_nonce
    from celerp.config import settings as _cfg
    from datetime import datetime, timedelta, timezone as _tz
    snonce = await _get_nonce(session, str(user.id))
    access_token, token_jti = create_access_token(str(user.id), str(company.id), "owner", user.email, snonce=snonce)
    # Cap at 24h to match create_access_token's internal cap so DB expiry = JWT exp
    capped_minutes = min(int(_cfg.access_token_expire_minutes), 24 * 60)
    expiry = datetime.now(_tz.utc) + timedelta(minutes=capped_minutes)
    await _reg_token(session, token_jti, str(user.id), expiry)
    return {
        "access_token": access_token,
        "refresh_token": create_refresh_token(str(user.id), str(company.id), "owner", user.email),
    }


@router.get("/me")
async def me(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        logger.warning("GET /companies/me: company_id %s not found in DB", company_id)
        raise HTTPException(status_code=404, detail="Not found")
    return {"id": str(company.id), "name": company.name, "slug": company.slug, "settings": company.settings}


@router.patch("/me")
async def patch_me(payload: CompanyPatch, company_id=Depends(get_current_company_id), _=Depends(require_admin), session: AsyncSession = Depends(get_session)) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    if payload.name is not None:
        company.name = payload.name.strip()
    # Merge (PATCH semantics): a partial settings payload must not wipe other
    # keys. Replacing wholesale erased e.g. the numbering `sequences`, currency,
    # and category_schemas whenever a caller sent only one field (the UI happens
    # to pre-merge, but partial callers — and a name-only patch — must be safe).
    if payload.settings:
        company.settings = {**(company.settings or {}), **payload.settings}
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

@router.get("/import/template", response_class=PlainTextResponse, include_in_schema=False)
async def import_settings_template():
    return PlainTextResponse(
        "entity_id,event_type,idempotency_key\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=settings.csv"},
    )


@router.post("/import/batch", response_model=BatchImportResult)
async def batch_import_settings(
    body: SettingsBatchImportRequest,
    company_id=Depends(get_current_company_id),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    from sqlalchemy import select as _select
    from celerp.models.ledger import LedgerEntry

    keys = [r.idempotency_key for r in body.records]
    existing_keys = set((await session.execute(
        _select(LedgerEntry.idempotency_key).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.idempotency_key.in_(keys),
        )
    )).scalars().all())

    created = skipped = 0
    errors: list[str] = []
    for rec in body.records:
        if rec.idempotency_key in existing_keys:
            skipped += 1
            continue
        try:
            # Settings are stored on company; represent imports as sys.* events on company
            await emit_event(
                session,
                company_id=company_id,
                entity_id=str(company_id),
                entity_type="company",
                event_type=rec.event_type,
                data=rec.data,
                actor_id=user.id,
                location_id=None,
                source=rec.source,
                idempotency_key=rec.idempotency_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
            existing_keys.add(rec.idempotency_key)
            created += 1
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: {exc}")

    await session.commit()
    return BatchImportResult(created=created, skipped=skipped, errors=errors)


@router.post("/me/locations")
async def create_location(payload: LocationCreate, company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    loc = Location(
        id=uuid.uuid4(),
        company_id=company_id,
        name=payload.name,
        type=payload.type,
        address=payload.address,
        is_default=payload.is_default,
    )
    session.add(loc)
    if payload.is_default:
        await _maybe_apply_regime(session, company_id, payload.address)
    await session.commit()
    return {"id": str(loc.id)}


class LocationBatchImportRequest(BaseModel):
    records: list[dict]


@router.post("/me/locations/import/batch")
async def import_locations_batch(
    payload: LocationBatchImportRequest,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    existing = {
        r.name
        for r in (await session.execute(select(Location).where(Location.company_id == company_id))).scalars().all()
    }
    created = skipped = failed = 0
    first = True
    for rec in payload.records:
        name = (rec.get("name") or "").strip()
        loc_type = (rec.get("type") or "warehouse").strip()
        if not name:
            failed += 1
            continue
        if name in existing:
            skipped += 1
            continue
        session.add(Location(
            id=uuid.uuid4(),
            company_id=company_id,
            name=name,
            type=loc_type,
            is_default=first,
        ))
        existing.add(name)
        created += 1
        first = False
    await session.commit()
    return {"created": created, "skipped": skipped, "failed": failed}


@router.get("/me/locations")
async def list_locations(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(Location).where(Location.company_id == company_id))).scalars().all()
    items = [{"id": str(r.id), "name": r.name, "type": r.type, "address": r.address, "is_default": r.is_default} for r in rows]
    return {"items": items, "total": len(items)}


@router.patch("/me/locations/{location_id}")
async def patch_location(location_id: str, payload: LocationPatch, company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    try:
        loc_uuid = uuid.UUID(location_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid location id")
    loc = await session.get(Location, loc_uuid)
    if loc is None or loc.company_id != company_id:
        raise HTTPException(status_code=404, detail="Location not found")
    if payload.name is not None:
        loc.name = payload.name
    if payload.address is not None:
        loc.address = payload.address
    if payload.type is not None:
        loc.type = payload.type
    if payload.is_default is not None:
        loc.is_default = payload.is_default
        if payload.is_default:
            # Clear default on all other locations for this company
            others = (await session.execute(
                select(Location).where(Location.company_id == company_id, Location.id != loc_uuid)
            )).scalars().all()
            for other in others:
                other.is_default = False
    # Re-seed regime if this is (or is becoming) the default location with a country
    effective_default = payload.is_default if payload.is_default is not None else loc.is_default
    effective_address = payload.address if payload.address is not None else loc.address
    if effective_default:
        await _maybe_apply_regime(session, company_id, effective_address)
    await session.commit()
    return {"id": str(loc.id), "name": loc.name, "type": loc.type, "address": loc.address, "is_default": loc.is_default}


@router.delete("/me/locations/{location_id}")
async def delete_location(
    location_id: str,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.projections import Projection
    from sqlalchemy import func as _func
    try:
        loc_uuid = uuid.UUID(location_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid location id")
    loc = await session.get(Location, loc_uuid)
    if loc is None or loc.company_id != company_id:
        raise HTTPException(status_code=404, detail="Location not found")
    if loc.is_default:
        raise HTTPException(status_code=409, detail="Cannot delete the default location.")
    item_count = (await session.execute(
        select(_func.count()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
            Projection.location_id == loc_uuid,
        )
    )).scalar_one()
    if item_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete location: {item_count} item(s) still assigned here. Reassign them first.",
        )
    await session.delete(loc)
    await session.commit()
    return {"ok": True}



@router.get("/me/users")
async def list_users(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    from celerp.models.accounting import UserCompany
    rows = (
        await session.execute(
            select(User, UserCompany.role, UserCompany.is_active).join(UserCompany, UserCompany.user_id == User.id).where(
                UserCompany.company_id == company_id
            )
        )
    ).all()
    items = [{"id": str(u.id), "email": u.email, "name": u.name, "role": role, "is_active": uc_active} for u, role, uc_active in rows]
    return {"items": items, "total": len(items)}


@router.post("/me/users")
async def create_user(
    payload: UserCreate,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.accounting import UserCompany

    if payload.role not in ROLE_LEVELS:
        raise HTTPException(400, f"Invalid role. Must be one of: {', '.join(sorted(ROLE_LEVELS, key=ROLE_LEVELS.get))}")

    # Check if user with this email already exists globally; if so, just link them.
    existing_user = (await session.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if existing_user:
        # Verify not already a member of this company
        existing_link = (await session.execute(
            select(UserCompany).where(UserCompany.user_id == existing_user.id, UserCompany.company_id == company_id)
        )).scalar_one_or_none()
        if existing_link:
            raise HTTPException(status_code=400, detail="User already a member of this company")
        link = UserCompany(id=uuid.uuid4(), user_id=existing_user.id, company_id=company_id, role=payload.role)
        session.add(link)
        try:
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error("create_user link failed: %s", e, exc_info=True)
            raise HTTPException(status_code=400, detail=f"User creation failed: {e}") from e
        return {"id": str(existing_user.id)}

    user = User(
        id=uuid.uuid4(),
        email=payload.email,
        name=payload.name,
        auth_hash=hash_password(payload.password),
        is_active=True,
    )
    session.add(user)
    try:
        await session.flush()  # persist user first so FK on user_companies is satisfied
        link = UserCompany(id=uuid.uuid4(), user_id=user.id, company_id=company_id, role=payload.role)
        session.add(link)
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.error("create_user failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=f"User creation failed: {e}") from e
    return {"id": str(user.id)}


@router.patch("/me/users/{user_id}")
async def patch_user(
    user_id: uuid.UUID,
    payload: UserPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.accounting import UserCompany
    from sqlalchemy import func as _func

    user = await session.get(User, user_id)
    link = (await session.execute(
        select(UserCompany).where(UserCompany.user_id == user_id, UserCompany.company_id == company_id)
    )).scalar_one_or_none()
    if not user or not link:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.name is not None:
        user.name = payload.name
    if payload.role is not None:
        if payload.role not in ROLE_LEVELS:
            raise HTTPException(400, f"Invalid role. Must be one of: {', '.join(sorted(ROLE_LEVELS, key=ROLE_LEVELS.get))}")
        # Guard: cannot demote the last active owner
        old_level = ROLE_LEVELS.get(link.role, 0)
        new_level = ROLE_LEVELS.get(payload.role, 0)
        if old_level >= ROLE_LEVELS["owner"] and new_level < ROLE_LEVELS["owner"]:
            owner_count = (
                await session.execute(
                    select(_func.count()).where(
                        UserCompany.company_id == company_id,
                        UserCompany.role == "owner",
                        UserCompany.is_active.is_(True),
                    )
                )
            ).scalar()
            if owner_count <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last owner. Assign another owner first.")
        link.role = payload.role
    if payload.is_active is not None:
        link.is_active = payload.is_active
    if payload.password is not None:
        user.auth_hash = hash_password(payload.password)
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Item schema configuration
# ---------------------------------------------------------------------------

from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA  # noqa: F401 re-export
from celerp.services.field_schema import get_effective_field_schema  # noqa: F401 re-export


_COST_SCHEMA_KEYS: frozenset[str] = frozenset({"cost_price", "cost_price_total"})


@router.get("/me/item-schema")
async def get_item_schema(company_id=Depends(get_current_company_id), role: str = Depends(get_current_role), session: AsyncSession = Depends(get_session)) -> list[dict]:
    schema = await get_effective_field_schema(session, company_id)
    if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["manager"]:
        schema = [f for f in schema if f.get("key") not in _COST_SCHEMA_KEYS]
    return schema


@router.patch("/me/item-schema")
async def patch_item_schema(
    payload: ItemSchemaPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["item_schema"] = [f.model_dump() for f in payload.fields]
    company.settings = settings
    await session.commit()
    return {"ok": True, "field_count": len(payload.fields)}


# ---------------------------------------------------------------------------
# Category schema - per-category attribute column definitions
# ---------------------------------------------------------------------------

@router.get("/me/category-schema/{category}")
async def get_category_schema(category: str, company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    cat_schemas: dict = company.settings.get("category_schemas") or {}
    saved = cat_schemas.get(category)
    if saved is not None:
        return saved
    # Fall back to module-contributed defaults (category_schema slot)
    from celerp.modules.slots import get as get_slot
    for contrib in get_slot("category_schema"):
        if contrib.get("category") == category:
            return contrib.get("fields") or []
    return []


@router.patch("/me/category-schema/{category}")
async def patch_category_schema(
    category: str,
    payload: CategorySchemaPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    cat_schemas = dict(settings.get("category_schemas") or {})
    cat_schemas[category] = [f.model_dump() for f in payload.fields]
    settings["category_schemas"] = cat_schemas
    company.settings = settings
    await session.commit()
    return {"ok": True, "category": category, "field_count": len(payload.fields)}


@router.get("/me/company-category-schemas")
async def get_company_category_schemas(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    """Return only company-level category schemas (no module defaults). Used to determine which categories the user explicitly applied."""
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(company.settings.get("category_schemas") or {})


@router.get("/me/category-display-names")
async def get_category_display_names(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    """Return display names keyed by category slug."""
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(company.settings.get("category_display_names") or {})


@router.get("/me/category-schemas")
async def get_all_category_schemas(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    """Return all category schemas keyed by category name.

    Merges module-contributed defaults (category_schema slot) with company overrides.
    Company overrides take precedence.
    """
    from celerp.modules.slots import get as get_slot
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    # Start with module defaults
    merged: dict = {}
    for contrib in get_slot("category_schema"):
        cat = contrib.get("category")
        if cat and cat not in merged:
            merged[cat] = contrib.get("fields") or []
    # Company overrides win
    merged.update(company.settings.get("category_schemas") or {})
    return merged


@router.post("/me/category-schemas/merge")
async def merge_category_schemas(
    payload: dict,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Auto-merge attribute keys discovered during import into category schemas.

    payload: {"schemas": {"CategoryName": [{"key": ..., "label": ..., "type": ..., "options": [...]}]}}

    For each category:
    - Appends new keys not already present in the stored category schema.
    - Never overwrites existing keys (user customisations preserved).
    Returns counts of new fields added per category.
    """
    incoming: dict[str, list[dict]] = payload.get("schemas") or {}
    if not incoming:
        raise HTTPException(status_code=422, detail="schemas required")

    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")

    settings = dict(company.settings)
    cat_schemas: dict[str, list[dict]] = dict(settings.get("category_schemas") or {})
    added: dict[str, int] = {}

    for cat, new_fields in incoming.items():
        existing = cat_schemas.get(cat) or []
        existing_keys = {f["key"] for f in existing}
        max_pos = max((f.get("position", 0) for f in existing), default=-1)
        appended = []
        for nf in new_fields:
            if nf["key"] not in existing_keys:
                max_pos += 1
                appended.append({**nf, "position": max_pos, "editable": True, "required": False, "visible_to_roles": [], "show_in_table": True})
                existing_keys.add(nf["key"])
        if appended:
            cat_schemas[cat] = existing + appended
            added[cat] = len(appended)

    if added:
        settings["category_schemas"] = cat_schemas
        company.settings = settings
        await session.commit()

    return {"ok": True, "added": added}


# ---------------------------------------------------------------------------
# Category CRUD
# ---------------------------------------------------------------------------

def _slugify_category(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@router.post("/me/categories")
async def create_category(
    payload: dict,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    key = _slugify_category(name)
    if not key:
        raise HTTPException(status_code=422, detail="name produces an empty key")
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings or {})
    cat_schemas = dict(settings.get("category_schemas") or {})
    if key in cat_schemas:
        raise HTTPException(status_code=409, detail=f"Category '{key}' already exists")
    cat_schemas[key] = []
    settings["category_schemas"] = cat_schemas
    display_names = dict(settings.get("category_display_names") or {})
    display_names[key] = name
    settings["category_display_names"] = display_names
    company.settings = settings
    await session.commit()
    return {"ok": True, "key": key}


@router.patch("/me/categories/{category_key}")
async def rename_category(
    category_key: str,
    payload: dict,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.projections import Projection
    new_name = str(payload.get("name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=422, detail="name is required")
    new_key = _slugify_category(new_name)
    if not new_key:
        raise HTTPException(status_code=422, detail="name produces an empty key")
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings or {})
    cat_schemas = dict(settings.get("category_schemas") or {})
    if category_key not in cat_schemas:
        raise HTTPException(status_code=404, detail=f"Category '{category_key}' not found")
    if new_key == category_key:
        return {"ok": True, "items_updated": 0}
    if new_key in cat_schemas:
        raise HTTPException(status_code=409, detail=f"Category '{new_key}' already exists")
    # Rename schema key
    cat_schemas[new_key] = cat_schemas.pop(category_key)
    settings["category_schemas"] = cat_schemas
    display_names = dict(settings.get("category_display_names") or {})
    display_names[new_key] = new_name
    display_names.pop(category_key, None)
    settings["category_display_names"] = display_names
    company.settings = settings
    # Bulk-update item projections
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    updated = 0
    for row in rows:
        if str(row.state.get("category") or "") == category_key:
            new_state = dict(row.state)
            new_state["category"] = new_key
            row.state = new_state
            updated += 1
    await session.commit()
    return {"ok": True, "items_updated": updated}


@router.delete("/me/categories/{category_key}")
async def delete_category(
    category_key: str,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.projections import Projection
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings or {})
    cat_schemas = dict(settings.get("category_schemas") or {})
    if category_key not in cat_schemas:
        raise HTTPException(status_code=404, detail=f"Category '{category_key}' not found")
    # Count items referencing this category
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    item_count = sum(1 for r in rows if str(r.state.get("category") or "") == category_key)
    if item_count > 0:
        raise HTTPException(
            status_code=409,
            detail={"detail": f"Cannot delete: {item_count} item(s) use this category.", "item_count": item_count},
        )
    cat_schemas.pop(category_key)
    settings["category_schemas"] = cat_schemas
    display_names = dict(settings.get("category_display_names") or {})
    display_names.pop(category_key, None)
    settings["category_display_names"] = display_names
    company.settings = settings
    await session.commit()
    return {"ok": True}




@router.get("/me/column-prefs")
async def get_column_prefs(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    """Return column visibility prefs keyed by category or '__all__'."""
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company.settings.get("column_prefs") or {}


@router.patch("/me/column-prefs")
async def patch_column_prefs(
    payload: ColumnPrefsPatch,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Merge column visibility prefs. Any user (not admin-only) can save their view prefs."""
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    prefs = dict(settings.get("column_prefs") or {})
    prefs.update(payload.prefs)
    settings["column_prefs"] = prefs
    company.settings = settings
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tax rates
# ---------------------------------------------------------------------------

DEFAULT_TAX_RATES: list[dict] = [
    {"name": "VAT 7%", "rate": 7.0, "tax_type": "both", "is_default": True,
     "description": "Standard VAT rate", "is_compound": False, "default_order": 0},
    {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
     "description": "Tax-exempt", "is_compound": False, "default_order": 0},
]


@router.get("/me/taxes")
async def get_taxes(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company.settings.get("taxes") or DEFAULT_TAX_RATES


@router.patch("/me/taxes")
async def patch_taxes(
    payload: TaxRatesPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["taxes"] = [t.model_dump() for t in payload.taxes]
    company.settings = settings
    await session.commit()
    return {"ok": True}


@router.post("/me/taxes/import/batch", response_model=BatchImportResult)
async def import_taxes_batch(
    payload: SettingsBatchImportRequest,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch import tax rates into company.settings.taxes.

    Deterministic behavior:
    - Key: name
    - If name exists (case-insensitive): skipped
    - Else: created

    NOTE: This remains the legacy settings-import format (records are raw dicts).
    """
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")

    res = BatchImportResult(created=0, skipped=0, errors=[])

    settings = dict(company.settings)
    taxes = list(settings.get("taxes") or DEFAULT_TAX_RATES)
    existing_names = {str(t.get("name", "")).strip().lower() for t in taxes if t.get("name")}

    records = [rec.data for rec in (payload.records or [])]
    for i, r in enumerate(records):
        name = str(r.get("name", "") or "").strip()
        rate_raw = r.get("rate", None)
        tax_type = str(r.get("tax_type", "both") or "both").strip() or "both"
        is_default_raw = r.get("is_default", False)
        description = str(r.get("description", "") or "").strip()

        if not name:
            res.errors.append(f"row:{i}: Missing name")
            continue

        key = name.lower()
        if key in existing_names:
            res.skipped += 1
            continue

        try:
            rate = float(rate_raw)
        except Exception:
            res.errors.append(f"{name}: Invalid rate")
            continue

        if tax_type not in {"sales", "purchase", "both"}:
            res.errors.append(f"{name}: Invalid tax_type: {tax_type}")
            continue

        is_default = bool(is_default_raw)
        if not isinstance(is_default_raw, bool):
            is_default = str(is_default_raw).strip().lower() in {"true", "1", "yes"}

        taxes.append({
            "name": name,
            "rate": rate,
            "tax_type": tax_type,
            "is_default": is_default,
            "description": description,
        })
        existing_names.add(key)
        res.created += 1

    settings["taxes"] = taxes
    company.settings = settings
    await session.commit()
    return res


# ---------------------------------------------------------------------------
# Payment terms
# ---------------------------------------------------------------------------

DEFAULT_PAYMENT_TERMS: list[dict] = [
    {"name": "Pay in Advance", "days": 0, "description": "Full payment before delivery"},
    {"name": "Cash on Delivery", "days": 0, "description": "Payment on receipt of goods"},
    {"name": "Deposit (50%)", "days": 0, "description": "50% deposit upfront, balance on delivery"},
    {"name": "Net 7", "days": 7, "description": "Due within 7 days"},
    {"name": "Net 15", "days": 15, "description": "Due within 15 days"},
    {"name": "Net 30", "days": 30, "description": "Due within 30 days"},
    {"name": "Net 60", "days": 60, "description": "Due within 60 days"},
    {"name": "Net 90", "days": 90, "description": "Due within 90 days"},
]


@router.get("/me/payment-terms")
async def get_payment_terms(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company.settings.get("payment_terms") or DEFAULT_PAYMENT_TERMS


@router.patch("/me/payment-terms")
async def patch_payment_terms(
    payload: PaymentTermsPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["payment_terms"] = payload.terms
    company.settings = settings
    await session.commit()
    return {"ok": True}


@router.post("/me/payment-terms/import/batch", response_model=BatchImportResult)
async def import_payment_terms_batch(
    payload: SettingsBatchImportRequest,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch import payment terms into company.settings.payment_terms.

    Deterministic behavior:
    - Key: name
    - If name exists (case-insensitive): skipped
    - Else: created
    """
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")

    res = BatchImportResult(created=0, skipped=0, errors=[])

    settings = dict(company.settings)
    terms = list(settings.get("payment_terms") or DEFAULT_PAYMENT_TERMS)
    existing_names = {str(t.get("name", "")).strip().lower() for t in terms if t.get("name")}

    records = [rec.data for rec in (payload.records or [])]
    for i, r in enumerate(records):
        name = str(r.get("name", "") or "").strip()
        days_raw = r.get("days", None)
        description = str(r.get("description", "") or "").strip()

        if not name:
            res.errors.append(f"row:{i}: Missing name")
            continue

        key = name.lower()
        if key in existing_names:
            res.skipped += 1
            continue

        try:
            days = int(days_raw)
        except Exception:
            res.errors.append(f"{name}: Invalid days")
            continue

        terms.append({"name": name, "days": days, "description": description})
        existing_names.add(key)
        res.created += 1

    settings["payment_terms"] = terms
    company.settings = settings
    await session.commit()
    return res


# ---------------------------------------------------------------------------
# Contact tags vocabulary
# ---------------------------------------------------------------------------


@router.get("/me/contact-tags")
async def get_contact_tags(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company.settings.get("contact_tags") or []


@router.patch("/me/contact-tags")
async def patch_contact_tags(
    payload: ContactTagsPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["contact_tags"] = payload.tags
    company.settings = settings
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Contact defaults
# ---------------------------------------------------------------------------


@router.get("/me/contact-defaults")
async def get_contact_defaults(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company.settings.get("contact_defaults") or {}


@router.patch("/me/contact-defaults")
async def patch_contact_defaults(
    payload: ContactDefaultsPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["contact_defaults"] = payload.defaults
    company.settings = settings
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Terms & Conditions templates
# ---------------------------------------------------------------------------

DEFAULT_TERMS_CONDITIONS: list[dict] = [
    {"name": "Standard Sales Terms", "text": "Goods remain property of the seller until paid in full.", "doc_types": ["invoice", "receipt", "credit_note"], "default_for": ["invoice", "receipt", "credit_note"]},
    {"name": "Standard Consignment Out Terms", "text": "Consigned goods remain property of the consignor until sold or returned.", "doc_types": ["memo"], "default_for": ["memo"]},
    {"name": "Standard Purchase Terms", "text": "Goods must conform to agreed specifications.", "doc_types": ["purchase_order", "bill"], "default_for": ["purchase_order", "bill"]},
    {"name": "Standard Consignment In Terms", "text": "Consigned goods remain property of the consignor. Unsold goods may be returned per agreed schedule.", "doc_types": ["consignment_in"], "default_for": ["consignment_in"]},
]


@router.get("/me/terms-conditions")
async def get_terms_conditions(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    templates = company.settings.get("terms_conditions") or DEFAULT_TERMS_CONDITIONS
    # Migrate old is_default boolean to default_for list
    migrated = False
    for t in templates:
        if "default_for" not in t:
            t["default_for"] = list(t.get("doc_types") or []) if t.pop("is_default", False) else []
            migrated = True
        t.pop("is_default", None)
    if migrated and company.settings.get("terms_conditions"):
        settings = dict(company.settings)
        settings["terms_conditions"] = templates
        company.settings = settings
        await session.commit()
    return templates


@router.patch("/me/terms-conditions")
async def patch_terms_conditions(
    payload: TermsConditionsPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["terms_conditions"] = [t for t in payload.templates]
    company.settings = settings
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Purchasing taxes & payment terms (independent copies, seeded from sales)
# ---------------------------------------------------------------------------

async def _seed_purchasing_key(
    session: AsyncSession, company: Company, key: str, sales_key: str, default: list[dict],
) -> list[dict]:
    """Return purchasing data; on first access, copy from sales data and persist."""
    existing = company.settings.get(key)
    if existing is not None:
        return existing
    import copy
    source = company.settings.get(sales_key) or default
    seeded = copy.deepcopy(source)
    settings = dict(company.settings)
    settings[key] = seeded
    company.settings = settings
    await session.commit()
    return seeded


@router.get("/me/purchasing-taxes")
async def get_purchasing_taxes(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return await _seed_purchasing_key(session, company, "purchasing_taxes", "taxes", DEFAULT_TAX_RATES)


@router.patch("/me/purchasing-taxes")
async def patch_purchasing_taxes(
    payload: TaxRatesPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["purchasing_taxes"] = [t.model_dump() for t in payload.taxes]
    company.settings = settings
    await session.commit()
    return {"ok": True}


@router.post("/me/purchasing-taxes/import/batch", response_model=BatchImportResult)
async def import_purchasing_taxes_batch(
    payload: SettingsBatchImportRequest,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    res = BatchImportResult(created=0, skipped=0, errors=[])
    taxes = list(await _seed_purchasing_key(session, company, "purchasing_taxes", "taxes", DEFAULT_TAX_RATES))
    existing_names = {str(t.get("name", "")).strip().lower() for t in taxes if t.get("name")}
    for i, r in enumerate(rec.data for rec in (payload.records or [])):
        name = str(r.get("name", "") or "").strip()
        if not name:
            res.errors.append(f"row:{i}: Missing name")
            continue
        if name.lower() in existing_names:
            res.skipped += 1
            continue
        try:
            rate = float(r.get("rate", None))
        except Exception:
            res.errors.append(f"{name}: Invalid rate")
            continue
        tax_type = str(r.get("tax_type", "both") or "both").strip() or "both"
        if tax_type not in {"sales", "purchase", "both"}:
            res.errors.append(f"{name}: Invalid tax_type: {tax_type}")
            continue
        is_default_raw = r.get("is_default", False)
        is_default = bool(is_default_raw) if isinstance(is_default_raw, bool) else str(is_default_raw).strip().lower() in {"true", "1", "yes"}
        taxes.append({"name": name, "rate": rate, "tax_type": tax_type, "is_default": is_default, "description": str(r.get("description", "") or "").strip()})
        existing_names.add(name.lower())
        res.created += 1
    settings = dict(company.settings)
    settings["purchasing_taxes"] = taxes
    company.settings = settings
    await session.commit()
    return res


@router.get("/me/purchasing-payment-terms")
async def get_purchasing_payment_terms(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return await _seed_purchasing_key(session, company, "purchasing_payment_terms", "payment_terms", DEFAULT_PAYMENT_TERMS)


@router.patch("/me/purchasing-payment-terms")
async def patch_purchasing_payment_terms(
    payload: PaymentTermsPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["purchasing_payment_terms"] = payload.terms
    company.settings = settings
    await session.commit()
    return {"ok": True}


@router.post("/me/purchasing-payment-terms/import/batch", response_model=BatchImportResult)
async def import_purchasing_payment_terms_batch(
    payload: SettingsBatchImportRequest,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    res = BatchImportResult(created=0, skipped=0, errors=[])
    terms = list(await _seed_purchasing_key(session, company, "purchasing_payment_terms", "payment_terms", DEFAULT_PAYMENT_TERMS))
    existing_names = {str(t.get("name", "")).strip().lower() for t in terms if t.get("name")}
    for i, r in enumerate(rec.data for rec in (payload.records or [])):
        name = str(r.get("name", "") or "").strip()
        if not name:
            res.errors.append(f"row:{i}: Missing name")
            continue
        if name.lower() in existing_names:
            res.skipped += 1
            continue
        try:
            days = int(r.get("days", None))
        except Exception:
            res.errors.append(f"{name}: Invalid days")
            continue
        terms.append({"name": name, "days": days, "description": str(r.get("description", "") or "").strip()})
        existing_names.add(name.lower())
        res.created += 1
    settings = dict(company.settings)
    settings["purchasing_payment_terms"] = terms
    company.settings = settings
    await session.commit()
    return res


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

import re as _re

_DEFAULT_UNITS: list[dict] = [
    {"name": "piece",  "label": "Piece",         "decimals": 0, "unit_type": "pieces"},
    {"name": "carat",  "label": "Carat (ct)",     "decimals": 2, "unit_type": "weight"},
    {"name": "gram",   "label": "Gram (g)",        "decimals": 2, "unit_type": "weight"},
    {"name": "kg",     "label": "Kilogram (kg)",   "decimals": 3, "unit_type": "weight"},
    {"name": "oz",     "label": "Ounce (oz)",      "decimals": 2, "unit_type": "weight"},
    {"name": "liter",  "label": "Liter (L)",       "decimals": 2, "unit_type": "quantity"},
    {"name": "meter",  "label": "Meter (m)",       "decimals": 2, "unit_type": "quantity"},
]

_UNIT_NAME_RE = _re.compile(r"^[a-z0-9_]+$")


_VALID_UNIT_TYPES: frozenset[str] = frozenset({"weight", "pieces", "quantity"})


def _validate_units(units: list[UnitRecord]) -> None:
    seen: set[str] = set()
    for u in units:
        if not _UNIT_NAME_RE.match(u.name):
            raise HTTPException(status_code=422, detail=f"Unit name '{u.name}' must be lowercase alphanumeric + underscore")
        if not (0 <= u.decimals <= 6):
            raise HTTPException(status_code=422, detail=f"Unit '{u.name}' decimals must be 0–6")
        if u.unit_type not in _VALID_UNIT_TYPES:
            raise HTTPException(status_code=422, detail=f"Unit '{u.name}' type must be one of: {', '.join(sorted(_VALID_UNIT_TYPES))}")
        if u.name in seen:
            raise HTTPException(status_code=422, detail=f"Duplicate unit name: '{u.name}'")
        seen.add(u.name)


@router.get("/me/units")
async def get_units(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company.settings.get("units") or _DEFAULT_UNITS


@router.put("/me/units")
async def put_units(
    payload: UnitsPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    _validate_units(payload.units)
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["units"] = [u.model_dump() for u in payload.units]
    company.settings = settings
    await session.commit()
    return settings["units"]


# ---------------------------------------------------------------------------
# Cloud Relay toggle
# ---------------------------------------------------------------------------

class RelayEnablePayload(BaseModel):
    gateway_token: str = Field(..., description="GATEWAY_TOKEN issued by celerp.com subscription.")
    instance_id: str = Field("", description="Optional stable instance identifier.")


@router.post("/me/relay/enable", dependencies=[Depends(require_admin)])
async def enable_relay(
    payload: RelayEnablePayload,
    _company_id=Depends(get_current_company_id),
) -> dict:
    """Activate the Cloud Relay for this instance.

    Stores the gateway token in runtime settings and starts the WS connection
    immediately (no restart required). Admin-only.
    """
    import asyncio
    from celerp.config import settings as _cfg
    from celerp.gateway import client as _gw

    _cfg.gateway_token = payload.gateway_token
    if payload.instance_id:
        _cfg.gateway_instance_id = payload.instance_id

    if _gw.get_client() is None:
        import uuid as _uuid
        instance_id = _cfg.gateway_instance_id or str(_uuid.uuid4())
        gw = _gw.GatewayClient(
            gateway_token=payload.gateway_token,
            instance_id=instance_id,
            gateway_url=_cfg.gateway_url,
        )
        _gw.set_client(gw)
        asyncio.create_task(gw.run())

    return {"ok": True, "message": "Cloud Relay activated."}


@router.post("/me/relay/disable", dependencies=[Depends(require_admin)])
async def disable_relay(_company_id=Depends(get_current_company_id)) -> dict:
    """Deactivate the Cloud Relay. Stops cloudflared and closes the WS connection. Admin-only."""
    from celerp.config import settings as _cfg
    from celerp.gateway import client as _gw
    from celerp.gateway.state import set_session_token

    client = _gw.get_client()
    if client:
        client.stop()
        _gw.set_client(None)
    _cfg.gateway_token = ""
    set_session_token("")
    return {"ok": True, "message": "Cloud Relay deactivated."}


# ── Module management ──────────────────────────────────────────────────────────

@router.get("/me/modules")
async def list_modules(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List all installed modules with their enabled state.

    Returns installed modules from the module directory, annotated with whether
    each is currently enabled in company settings. Loaded (runtime) modules are
    also flagged as running=True.
    """
    import asyncio
    import os
    from pathlib import Path
    from celerp.modules.loader import loaded_modules, read_manifest_metadata
    from celerp.modules.registry import get_enabled

    company = await session.get(Company, company_id)
    settings_dict: dict = company.settings or {} if company else {}
    enabled_names = get_enabled(settings_dict)
    loaded_by_name: dict[str, dict] = {m["name"]: m for m in loaded_modules()}
    module_dir_raw = os.environ.get("MODULE_DIR", "")

    def _scan_modules() -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        for d_str in module_dir_raw.split(","):
            d_str = d_str.strip()
            if not d_str:
                continue
            d = Path(d_str)
            if not d.exists():
                continue
            for pkg_path in sorted(d.iterdir()):
                if not pkg_path.is_dir() or not (pkg_path / "__init__.py").exists():
                    continue
                pkg_name = pkg_path.name
                if pkg_name in seen:
                    continue
                seen.add(pkg_name)
                loaded = loaded_by_name.get(pkg_name)
                manifest_source = loaded or read_manifest_metadata(pkg_path)
                results.append({
                    "name": pkg_name,
                    "label": manifest_source.get("display_name") or manifest_source.get("label") or pkg_name,
                    "version": manifest_source.get("version", "unknown"),
                    "description": manifest_source.get("description", ""),
                    "author": manifest_source.get("author", ""),
                    "depends_on": list(manifest_source.get("depends_on") or []),
                    "enabled": pkg_name in enabled_names,
                    "running": pkg_name in loaded_by_name,
                })
        return results

    return await asyncio.to_thread(_scan_modules)


@router.post("/me/modules/{module_name}/enable", dependencies=[Depends(require_admin)])
async def enable_module(
    module_name: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Enable a module. Requires admin. A restart is required for changes to take effect."""
    from celerp.modules.registry import enable, get_enabled
    from celerp.config import set_enabled_modules

    company = await session.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    company.settings = enable(company.settings or {}, module_name)
    await session.commit()
    set_enabled_modules([module_name])
    enabled_list = sorted(get_enabled(company.settings))
    return {"ok": True, "name": module_name, "enabled": True, "restart_required": True, "enabled_modules": enabled_list}


@router.post("/me/modules/{module_name}/disable", dependencies=[Depends(require_admin)])
async def disable_module(
    module_name: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Disable a module. Requires admin. A restart is required for changes to take effect."""
    from celerp.modules.registry import disable, get_enabled
    from celerp.config import read_config, write_config

    company = await session.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    company.settings = disable(company.settings or {}, module_name)
    await session.commit()
    # Remove from config file so the next restart honours the disable
    cfg = read_config()
    if cfg:
        enabled = cfg.get("modules", {}).get("enabled", [])
        cfg.setdefault("modules", {})["enabled"] = [m for m in enabled if m != module_name]
        write_config(cfg)
    enabled_list = sorted(get_enabled(company.settings))
    return {"ok": True, "name": module_name, "enabled": False, "restart_required": True, "enabled_modules": enabled_list}


@router.delete("/me", dependencies=[Depends(require_admin)])
async def deactivate_company(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Soft-delete the current company. Sets is_active=False. Admin only.

    Does not delete any data. All records (ledger, documents, users) are preserved.
    Use POST /me/reactivate to restore.
    """
    import time as _time
    import re as _re2
    company = await session.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    company.is_active = False
    # Free the slug so the user can re-create a company with the same name later.
    # Strip any previous deactivated suffix first (idempotent), then append new one.
    base_slug = _re2.sub(r"-deactivated-\d+$", "", company.slug)
    company.slug = f"{base_slug}-deactivated-{int(_time.time())}"
    await session.commit()
    return {"ok": True, "company_id": str(company_id), "is_active": False}


@router.post("/me/reactivate", dependencies=[Depends(require_admin)])
async def reactivate_company(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reactivate a previously deactivated company. Admin only."""
    import re as _re2
    company = await session.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    company.is_active = True
    # Restore slug to its original form (strip deactivated suffix).
    company.slug = _re2.sub(r"-deactivated-\d+$", "", company.slug)
    await session.commit()
    return {"ok": True, "company_id": str(company_id), "is_active": True}


# ---------------------------------------------------------------------------
# Price lists
# ---------------------------------------------------------------------------

DEFAULT_PRICE_LISTS: list[dict] = [
    {"name": "Retail", "description": "Standard retail price"},
    {"name": "Wholesale", "description": "Wholesale / trade price"},
]
DEFAULT_PRICE_LIST_NAME: str = "Retail"


class PriceListsPatch(BaseModel):
    price_lists: list[dict]


class DefaultPriceListPatch(BaseModel):
    name: str


_COST_PL_NAMES: frozenset[str] = frozenset({"cost", "cost price", "landed", "landed cost"})


@router.get("/me/price-lists")
async def get_price_lists(
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    existing = company.settings.get("price_lists")
    if existing is not None:
        price_lists = existing
    else:
        # Lazy seed defaults on first access
        import copy
        seeded = copy.deepcopy(DEFAULT_PRICE_LISTS)
        settings = dict(company.settings)
        settings["price_lists"] = seeded
        if "default_price_list" not in settings:
            settings["default_price_list"] = DEFAULT_PRICE_LIST_NAME
        company.settings = settings
        await session.commit()
        price_lists = seeded
    if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["manager"]:
        price_lists = [pl for pl in price_lists if pl.get("name", "").lower() not in _COST_PL_NAMES]
    return price_lists


@router.patch("/me/price-lists")
async def patch_price_lists(
    payload: PriceListsPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["price_lists"] = payload.price_lists
    company.settings = settings
    await session.commit()
    return {"ok": True}


@router.get("/me/default-price-list")
async def get_default_price_list(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> str:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company.settings.get("default_price_list") or DEFAULT_PRICE_LIST_NAME


@router.patch("/me/default-price-list")
async def patch_default_price_list(
    payload: DefaultPriceListPatch,
    company_id=Depends(get_current_company_id),
    _=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["default_price_list"] = payload.name
    company.settings = settings
    await session.commit()
    return {"ok": True}


@router.post("/me/demo/reseed", dependencies=[Depends(require_admin)])
async def reseed_demo_items(
    company_id=Depends(get_current_company_id),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    vertical: str | None = None,
) -> dict:
    """Re-seed demo items using the company's current vertical setting.

    Wipes all existing demo items (ledger + projections) before seeding so that
    a vertical change replaces, rather than appends to, the previous demo set.

    `vertical` query param overrides the DB lookup (used by setup wizard to avoid
    a race between the settings PATCH and this call).
    """
    import sqlalchemy as _sa
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.services.demo import seed_demo_items

    # Collect demo item entity_ids so we can delete their projections too
    demo_entity_ids_result = await session.execute(
        _sa.select(LedgerEntry.entity_id).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_type == "item",
            LedgerEntry.source == "demo",
        ).distinct()
    )
    demo_entity_ids = [row[0] for row in demo_entity_ids_result]

    if demo_entity_ids:
        await session.execute(
            _sa.delete(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_id.in_(demo_entity_ids),
            )
        )
        await session.execute(
            _sa.delete(LedgerEntry).where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.entity_type == "item",
                LedgerEntry.source == "demo",
            )
        )

    # vertical param takes precedence; fall back to company settings
    if not vertical:
        company = await session.get(Company, company_id)
        vertical = (company.settings or {}).get("vertical") if company else None
    # Look up the default location so demo items land in it
    from sqlalchemy import select as _select
    from celerp.models.company import Location as _Location
    _loc_result = await session.execute(
        _select(_Location).where(
            _Location.company_id == company_id,
            _Location.is_default == True,
        ).limit(1)
    )
    _default_loc = _loc_result.scalars().first()
    await seed_demo_items(session, company_id, user.id, vertical=vertical,
                          default_location_id=_default_loc.id if _default_loc else None)
    await session.commit()
    return {"ok": True, "vertical": vertical, "wiped": len(demo_entity_ids)}

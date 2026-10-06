# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import asyncio
import copy
import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.events.engine import emit_event
from celerp.models.company import Company, Location, User
from celerp.models.accounting import UserCompany
from celerp.services.auth import (
    AuthContext,
    first_usable_company_link,
    get_auth_context,
    get_current_company_id,
    get_current_user,
    get_current_role,
    hash_password,
    issue_token_pair,
    require_install_owner,
    MIN_PASSWORD_LENGTH,
    normalize_role,
    ROLE_LEVELS,
    validate_password,
)
from celerp.services.permissions import (
    PERMISSIONS,
    ROLES,
    get_current_company_settings,
    locked_authority,
    require_permission,
    resolved_grant_roles,
    role_has_permission,
)
from celerp.schemas.numbers import FiniteFloat
from celerp.tax_regimes import get_regime, TAX_REGIMES
from celerp.services import company_lifecycle
from celerp.services.provisioning import provision_additional_company
from celerp.services.terms import terms_templates
from celerp.services.payment_terms import DEFAULT_PAYMENT_TERMS, company_payment_terms
from celerp.services.business_time import business_timezone
from celerp.services.company_lock import lock_company, lock_company_for_deletion, locked_company

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

    company = await locked_company(session, company_id)
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


class RolePermissionPatch(BaseModel):
    perm_key: str
    role_key: str
    granted: bool


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
    # Only the per-company membership is editable here: role and active flag.
    # name and password live on the global User row shared across every company
    # the user belongs to, so a single company's admin must never overwrite them
    # through a membership patch. extra="forbid" rejects those (and any unknown)
    # fields with a 422 rather than silently ignoring them.
    model_config = {"extra": "forbid"}

    role: str | None = None
    is_active: bool | None = None


class ItemSchemaField(BaseModel):
    key: str
    label: str
    type: str  # text|number|money|select|date|boolean|weight|status|image
    editable: bool = True
    required: bool = False
    options: list[str] = Field(default_factory=list)
    visible_to_roles: list[str] = Field(default_factory=list)  # empty = all roles
    position: FiniteFloat = 0
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
    rate: FiniteFloat  # percentage, e.g. 7.0
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
    records: list[SettingsImportRecord] = Field(..., max_length=500)


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
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a new company linked to the current user. Returns JWT scoped to new company."""
    user = ctx.user
    company = await provision_additional_company(session, user=user, company_name=payload.name)
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.error("create_company failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=f"Could not create company: {e}") from e
    # Creating a company is a continuation of the current owner session: pass the
    # snonce it authenticated on so a concurrent revocation cannot be jumped over.
    return await issue_token_pair(session, user=user, company_id=company.id, expected_snonce=ctx.snonce)


@router.get("/me")
async def me(
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await session.get(Company, company_id)
    if company is None:
        logger.warning("GET /companies/me: company_id %s not found in DB", company_id)
        raise HTTPException(status_code=404, detail="Not found")
    # current_role is the authoritative DB membership role; the UI gates
    # permissions on it rather than trusting the client-held token claims.
    return {
        "id": str(company.id),
        "name": company.name,
        "slug": company.slug,
        "settings": company.settings,
        "current_role": role,
    }


@router.get("/commercial-state")
async def commercial_state(_: None = require_permission("manage_integrations")) -> dict:
    """Return the live commercial state the API process holds from the relay WS
    push, for the separate UI process to read over an authenticated seam.

    The UI runs in its own process and cannot see these in-process globals, so it
    reads them here. Gated at the same permission as the settings pages that
    consume it, so tab visibility stays consistent with page access. Only
    non-secret entitlement fields are returned; the co-resident config secrets
    are never read by this path.
    """
    from celerp.gateway.state import (
        get_commercial_context,
        get_commercial_mode,
        get_feature_flags,
        get_partner_identity,
    )
    return {
        "feature_flags": get_feature_flags(),
        "commercial_context": get_commercial_context(),
        "partner_identity": get_partner_identity(),
        "commercial_mode": get_commercial_mode(),
    }


@router.patch("/me")
async def patch_me(payload: CompanyPatch, company_id=Depends(get_current_company_id), _: None = require_permission("manage_company_settings"), session: AsyncSession = Depends(get_session)) -> dict:
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    if payload.name is not None:
        company.name = payload.name.strip()
    # Merge (PATCH semantics): a partial settings payload must not wipe other
    # keys. Replacing wholesale erased e.g. the numbering `sequences`, currency,
    # and category_schemas whenever a caller sent only one field (the UI happens
    # to pre-merge, but partial callers — and a name-only patch — must be safe).
    if payload.settings:
        # Role grants are owner-only and may be written only through the dedicated
        # PATCH /me/role-permissions endpoint (manage_permissions). This door is
        # admin-gated (manage_company_settings), so accepting role_grants here would
        # let an admin self-escalate around the owner gate. role_permissions is the
        # retired storage key; reject it too so it can never be re-introduced.
        if "role_grants" in payload.settings or "role_permissions" in payload.settings:
            raise HTTPException(
                status_code=422,
                detail="Role permissions are set through the permissions matrix, not company settings",
            )
        # Business type carries modules, categories and default terms with it, so it
        # changes only through POST /companies/me/business-type.
        if "vertical" in payload.settings:
            raise HTTPException(
                status_code=422,
                detail="Business type is set through POST /companies/me/business-type, not company settings",
            )
        # The record of which company backup a company was restored from is written only by
        # the restore itself; a restore of that backup finds its company by it.
        if "restored_backup" in payload.settings:
            raise HTTPException(
                status_code=422,
                detail="The restored backup record is set only by restoring a company backup, not company settings",
            )
        # The company's module choice changes only through the enable/disable endpoints,
        # which check installation and dependencies and keep the load list in step.
        if "enabled_modules" in payload.settings:
            raise HTTPException(
                status_code=422,
                detail="Modules are turned on and off on the Modules page, not company settings",
            )
        merged = {**(company.settings or {}), **payload.settings}
        if "timezone" in payload.settings:
            try:
                business_timezone(payload.settings.get("timezone"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        from celerp_docs.routes_payments import (ONLINE_DEPOSIT_ACCOUNT_KEY, WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY,
                                                 require_online_deposit_account)
        for key in (ONLINE_DEPOSIT_ACCOUNT_KEY, WOOCOMMERCE_DEPOSIT_ACCOUNT_KEY):
            value = payload.settings.get(key)
            if value in (None, ""):  # empty: the default
                continue
            if not isinstance(value, str):
                raise HTTPException(status_code=422, detail=f"{key} must be an account code or empty.")
            await require_online_deposit_account(session, company_id, value)
        # Price config must pass the same gate as the dedicated endpoints: the read
        # path trusts stored config, so no door may store what the validator rejects.
        if "price_lists" in payload.settings or "base_price_list" in payload.settings:
            merged_lists = merged.get("price_lists") or []
            error = price_config_error(merged_lists, merged.get("base_price_list"),
                                       (company.settings or {}).get("price_lists") or [])
            if error:
                raise HTTPException(status_code=422, detail=error)
            if merged.get("price_lists"):
                merged["price_lists"] = normalized_price_lists(merged["price_lists"])
        company.settings = merged
    await session.commit()
    return {"ok": True}


@router.patch("/me/role-permissions")
async def patch_role_permissions(
    payload: RolePermissionPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_permissions"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Toggle one (permission, role) cell from the matrix.

    Owner-only (manage_permissions is a fixed owner row, so no owner can revoke
    their own ability to edit permissions). A toggle names a (permission, role)
    cell and whether that role should now hold the permission; only that role's
    membership changes - every other role keeps the grant it already had.
    """
    perm = next((p for p in PERMISSIONS if p.key == payload.perm_key), None)
    if perm is None:
        raise HTTPException(status_code=422, detail=f"Unknown permission '{payload.perm_key}'")
    if payload.role_key not in ROLE_LEVELS:
        raise HTTPException(status_code=422, detail=f"Unknown role '{payload.role_key}'")
    if not perm.grantable:
        raise HTTPException(status_code=403, detail=f"The {perm.key} permission is fixed and cannot be reassigned")

    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings or {})
    grants = dict(settings.get("role_grants") or {})
    # Seed from the currently RESOLVED set on first touch, so toggling one role
    # leaves every other role's grant exactly as it is today.
    roles = set(resolved_grant_roles(settings, perm.key))
    if payload.granted:
        roles.add(payload.role_key)
    else:
        roles.discard(payload.role_key)
    # Floor guard: no override may drop the permission below its floor role.
    if payload.granted and ROLE_LEVELS[payload.role_key] < ROLE_LEVELS[perm.floor_role]:
        raise HTTPException(
            status_code=422,
            detail=f"The {perm.key} permission cannot go below the {perm.floor_role} role",
        )
    grants[perm.key] = sorted(roles, key=lambda r: ROLE_LEVELS[r])
    settings["role_grants"] = grants
    company.settings = settings
    await session.commit()
    return {"ok": True, "perm_key": perm.key, "roles": grants[perm.key]}


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
    _: None = require_permission("manage_company_settings"),
    __: None = require_permission("import_export_data"),
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
        # This legacy transport is a company snapshot import, not arbitrary access
        # to the system-event namespace. User/API-key/backup/migration events have
        # different owners and must never be emitted against the company projection.
        if rec.event_type != "sys.company.created":
            if len(errors) < 10:
                errors.append(f"{rec.entity_id}: event type {rec.event_type!r} is not import-safe")
            skipped += 1
            continue
        if rec.idempotency_key in existing_keys:
            skipped += 1
            continue
        try:
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
async def create_location(
    payload: LocationCreate,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
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
    records: list[dict] = Field(..., max_length=500)


@router.post("/me/locations/import/batch")
async def import_locations_batch(
    payload: LocationBatchImportRequest,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    __: None = require_permission("import_export_data"),
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
async def patch_location(
    location_id: str,
    payload: LocationPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        loc_uuid = uuid.UUID(location_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid location id")
    # Company first, then the location: the order an import commit takes them in (and the
    # tax re-seed below needs the company lock), so the two wait instead of deadlocking.
    await lock_company(session, company_id)
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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.projections import Projection
    from sqlalchemy import func as _func
    try:
        loc_uuid = uuid.UUID(location_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid location id")
    # Locked before the item count: an import naming this location holds it until
    # the import commits, so the count below includes the items it placed here.
    loc = await session.get(Location, loc_uuid, with_for_update=True, populate_existing=True)
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
    items = [
        {"id": str(u.id), "email": u.email, "name": u.name, "role": normalize_role(role), "is_active": uc_active,
         "is_install_owner": bool(u.is_install_owner)}
        for u, role, uc_active in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/me/users/{user_id}/installation-owner")
async def transfer_install_owner(
    user_id: uuid.UUID,
    company_id=Depends(get_current_company_id),
    owner: User = Depends(require_install_owner),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Transfer installation-wide authority to an active user in this company."""
    current = (await session.execute(
        select(User).where(User.is_install_owner.is_(True)).with_for_update()
    )).scalar_one_or_none()
    if current is None or current.id != owner.id:
        raise HTTPException(status_code=403, detail="Installation owner access required")
    if current.id == user_id:
        return {"ok": True}

    target = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )).scalar_one_or_none()
    membership = (await session.execute(
        select(UserCompany).where(
            UserCompany.user_id == user_id,
            UserCompany.company_id == company_id,
            UserCompany.is_active.is_(True),
        ).with_for_update()
    )).scalar_one_or_none()
    if target is None or not target.is_active or membership is None:
        raise HTTPException(status_code=400, detail="Installation owner must be an active user in this company")

    current.is_install_owner = False
    await session.flush()
    target.is_install_owner = True
    from celerp.notifications import service as notif_service
    await notif_service.create_keyed(
        session, company_id, "system", "notif.install_owner", {"name": current.name or current.email},
        user_id=target.id, action_url="/settings/general?tab=users", priority="high",
    )
    await session.commit()
    return {"ok": True}


def _assert_role_assignable(caller_role: str, target_role: str) -> None:
    """A holder of manage_users may not assign a role above their own. The matrix can
    grant manage_users down to any role, so this ceiling is what stops that from
    becoming a self-promotion path."""
    if ROLE_LEVELS[target_role] > ROLE_LEVELS[caller_role]:
        raise HTTPException(status_code=403, detail="You cannot assign a role above your own.")


@router.post("/me/users")
async def create_user(
    payload: UserCreate,
    company_id=Depends(get_current_company_id),
    caller_role: str = Depends(get_current_role),
    _: None = require_permission("manage_users"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.accounting import UserCompany

    if payload.role not in ROLE_LEVELS:
        raise HTTPException(400, f"Invalid role. Must be one of: {', '.join(sorted(ROLE_LEVELS, key=ROLE_LEVELS.get))}")
    _assert_role_assignable(caller_role, payload.role)
    await lock_company(session, company_id)

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

    # Only the branch that actually creates a new password hash enforces the length
    # policy; linking an existing global user never touches this password field.
    try:
        validate_password(payload.password)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
        )

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
    caller_role: str = Depends(get_current_role),
    _: None = require_permission("manage_users"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.accounting import UserCompany
    from sqlalchemy import func as _func

    await lock_company(session, company_id)
    user = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )).scalar_one_or_none()
    link = (await session.execute(
        select(UserCompany).where(
            UserCompany.user_id == user_id,
            UserCompany.company_id == company_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if not user or not link:
        raise HTTPException(status_code=404, detail="User not found")
    # A holder of manage_users may not modify a user whose role outranks their own.
    # Compare on the normalized role so a legacy membership (e.g. salesperson) is
    # ranked at its current level (operator) rather than falling to level 0.
    if ROLE_LEVELS.get(normalize_role(link.role), 0) > ROLE_LEVELS[caller_role]:
        raise HTTPException(status_code=403, detail="You cannot modify a user whose role is above your own.")
    # Track whether any security-sensitive field actually changes. A role change
    # or a membership active-state change must rotate the target's session state
    # so their existing access AND refresh tokens are rejected on next use. Only
    # the membership is editable here: the global User name and password are not,
    # so a company admin can never overwrite fields shared across tenants.
    security_change = False
    if payload.role is not None:
        if payload.role not in ROLE_LEVELS:
            raise HTTPException(400, f"Invalid role. Must be one of: {', '.join(sorted(ROLE_LEVELS, key=ROLE_LEVELS.get))}")
        # Nor may they promote anyone above their own role.
        _assert_role_assignable(caller_role, payload.role)
        # Guard: cannot demote the last active owner
        old_level = ROLE_LEVELS.get(normalize_role(link.role), 0)
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
        if payload.role != link.role:
            security_change = True
        link.role = payload.role
    if payload.is_active is not None:
        if payload.is_active is False and link.is_active:
            if normalize_role(link.role) == "owner":
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
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot deactivate the last owner. Assign another owner first.",
                    )

            if user.is_install_owner:
                active_memberships = (
                    await session.execute(
                        select(_func.count()).where(
                            UserCompany.user_id == user_id,
                            UserCompany.is_active.is_(True),
                        )
                    )
                ).scalar()
                if active_memberships <= 1:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot deactivate the installation owner's last active membership.",
                    )

        if payload.is_active != link.is_active:
            security_change = True
        link.is_active = payload.is_active
    if security_change:
        # invalidate_sessions commits this same session, so the membership
        # change and nonce rotation are one transaction: neither can persist
        # without the other.
        from celerp.services.session_tracker import invalidate_sessions
        await invalidate_sessions(session, str(user_id))
    else:
        await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Item schema configuration
# ---------------------------------------------------------------------------

from celerp.services.field_schema import COST_SCHEMA_KEYS  # noqa: F401 re-export
from celerp.services.field_schema import DEFAULT_ITEM_SCHEMA  # noqa: F401 re-export
from celerp.services.field_schema import get_effective_field_schema  # noqa: F401 re-export


@router.get("/me/item-schema")
async def get_item_schema(company_id=Depends(get_current_company_id), role: str = Depends(get_current_role), settings: dict = Depends(get_current_company_settings), session: AsyncSession = Depends(get_session)) -> list[dict]:
    schema = await get_effective_field_schema(session, company_id)
    if not role_has_permission(settings, role, "view_inventory_costs"):
        schema = [f for f in schema if f.get("key") not in COST_SCHEMA_KEYS]
    return schema


@router.patch("/me/item-schema")
async def patch_item_schema(
    payload: ItemSchemaPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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
    from celerp.services.field_schema import all_category_schemas
    return all_category_schemas(company.settings or {}).get(category, [])


@router.patch("/me/category-schema/{category}")
async def patch_category_schema(
    category: str,
    payload: CategorySchemaPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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
    """Return all category schemas keyed by category name (module defaults plus company overrides)."""
    from celerp.services.field_schema import all_category_schemas
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return all_category_schemas(company.settings)


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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    key = _slugify_category(name)
    if not key:
        raise HTTPException(status_code=422, detail="name produces an empty key")
    company = await locked_company(session, company_id)
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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.projections import Projection
    new_name = str(payload.get("name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=422, detail="name is required")
    new_key = _slugify_category(new_name)
    if not new_key:
        raise HTTPException(status_code=422, detail="name produces an empty key")
    company = await locked_company(session, company_id)
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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from celerp.models.projections import Projection
    company = await locked_company(session, company_id)
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
    company = await locked_company(session, company_id)
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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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
    user=Depends(get_current_user),
    _: None = require_permission("manage_company_settings"),
    __: None = require_permission("import_export_data"),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch import tax rates into company.settings.taxes.

    Deterministic behavior:
    - Key: name
    - If name exists (case-insensitive): skipped
    - Else: created

    NOTE: This remains the legacy settings-import format (records are raw dicts).
    """
    await locked_authority(session, company_id, user.id, ("manage_company_settings", "import_export_data"))
    company = await session.get(Company, company_id)
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


@router.get("/me/payment-terms")
async def get_payment_terms(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company_payment_terms(company.settings)


@router.patch("/me/payment-terms")
async def patch_payment_terms(
    payload: PaymentTermsPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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
    user=Depends(get_current_user),
    _: None = require_permission("manage_company_settings"),
    __: None = require_permission("import_export_data"),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    """Batch import payment terms into company.settings.payment_terms.

    Deterministic behavior:
    - Key: name
    - If name exists (case-insensitive): skipped
    - Else: created
    """
    await locked_authority(session, company_id, user.id, ("manage_company_settings", "import_export_data"))
    company = await session.get(Company, company_id)
    res = BatchImportResult(created=0, skipped=0, errors=[])

    settings = dict(company.settings)
    terms = list(company_payment_terms(settings))
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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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

@router.get("/me/terms-conditions")
async def get_terms_conditions(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> list[dict]:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    configured = company.settings.get("terms_conditions")
    templates = terms_templates(company.settings)
    if configured is not None and templates != configured:
        company = await locked_company(session, company_id)
        templates = terms_templates(company.settings)
        if company.settings.get("terms_conditions") != templates:
            company.settings = {**company.settings, "terms_conditions": templates}
        await session.commit()
    return templates


@router.patch("/me/terms-conditions")
async def patch_terms_conditions(
    payload: TermsConditionsPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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

def _purchasing_list(settings: dict, key: str, sales_key: str, default: list[dict]) -> list[dict]:
    """Purchasing data as stored, or a copy of the sales data it is seeded from."""
    existing = settings.get(key)
    if existing is not None:
        return existing
    return copy.deepcopy(settings.get(sales_key) or default)


async def _seed_purchasing_key(
    session: AsyncSession, company: Company, key: str, sales_key: str, default: list[dict],
) -> list[dict]:
    """Return purchasing data; on first access, copy from sales data and persist."""
    existing = company.settings.get(key)
    if existing is not None:
        return existing
    company = await locked_company(session, company.id)
    existing = company.settings.get(key)
    if existing is not None:
        await session.commit()
        return existing
    seeded = _purchasing_list(company.settings, key, sales_key, default)
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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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
    user=Depends(get_current_user),
    _: None = require_permission("manage_company_settings"),
    __: None = require_permission("import_export_data"),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    await locked_authority(session, company_id, user.id, ("manage_company_settings", "import_export_data"))
    company = await session.get(Company, company_id)
    res = BatchImportResult(created=0, skipped=0, errors=[])
    taxes = list(_purchasing_list(company.settings, "purchasing_taxes", "taxes", DEFAULT_TAX_RATES))
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
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
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
    user=Depends(get_current_user),
    _: None = require_permission("manage_company_settings"),
    __: None = require_permission("import_export_data"),
    session: AsyncSession = Depends(get_session),
) -> BatchImportResult:
    await locked_authority(session, company_id, user.id, ("manage_company_settings", "import_export_data"))
    company = await session.get(Company, company_id)
    res = BatchImportResult(created=0, skipped=0, errors=[])
    terms = list(_purchasing_list(company.settings, "purchasing_payment_terms", "payment_terms", DEFAULT_PAYMENT_TERMS))
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

from celerp.services.units import DEFAULT_UNITS

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
    return company.settings.get("units") or DEFAULT_UNITS


@router.put("/me/units")
async def put_units(
    payload: UnitsPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    _validate_units(payload.units)
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["units"] = [u.model_dump() for u in payload.units]
    company.settings = settings
    await session.commit()
    return settings["units"]


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
    from datetime import datetime, timezone
    from pathlib import Path
    from celerp.modules.loader import (
        first_party_names, is_core_folded, is_first_party, is_running, load_errors,
        loaded_modules, read_manifest_metadata,
    )
    from celerp.modules.meta import SOURCES, read_meta
    from celerp.modules.registry import company_modules

    company = await session.get(Company, company_id)
    settings_dict: dict = company.settings or {} if company else {}
    enabled_names = company_modules(settings_dict)
    loaded_by_name: dict[str, dict] = {m["name"]: m for m in loaded_modules()}
    load_errs = load_errors()
    module_dir_raw = os.environ.get("MODULE_DIR", "")

    def _scan_modules() -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        lock_names = first_party_names()
        for d_str in module_dir_raw.split(","):
            d_str = d_str.strip()
            if not d_str:
                continue
            d = Path(d_str)
            if not d.exists():
                continue
            for pkg_path in sorted(d.iterdir()):
                # Skip the importer's transient landing/deletion dirs
                # (.<name>.incoming-* / .<name>.deleting-*) and any other
                # dot-prefixed entry - they are never installed modules.
                if pkg_path.name.startswith("."):
                    continue
                if not pkg_path.is_dir() or not (pkg_path / "__init__.py").exists():
                    continue
                pkg_name = pkg_path.name
                if pkg_name in seen:
                    continue
                seen.add(pkg_name)
                loaded = loaded_by_name.get(pkg_name)
                manifest_source = loaded or read_manifest_metadata(pkg_path)
                # Provenance and install time drive the source shield and the
                # newest-imported-first ordering. A default is identified by
                # content (its digest matches the committed first-party lock), not
                # by name or sidecar, and never carries an install time (the desktop
                # app re-seeds them on every version bump). A non-default folder
                # with no sidecar (a pre-existing import) falls back to its
                # folder ctime so ordering still has something to sort on.
                is_default = is_first_party(pkg_path)
                if is_default:
                    source = "default"
                    installed_at = None
                else:
                    meta = read_meta(pkg_path)
                    source = meta.get("source")
                    if source not in SOURCES:
                        source = "sideloaded"
                    installed_at = meta.get("installed_at")
                    if installed_at is None:
                        ctime = pkg_path.stat().st_ctime
                        installed_at = datetime.fromtimestamp(
                            ctime, tz=timezone.utc).isoformat()
                results.append({
                    "name": pkg_name,
                    "label": manifest_source.get("display_name") or manifest_source.get("label") or pkg_name,
                    "version": manifest_source.get("version", "unknown"),
                    "description": manifest_source.get("description", ""),
                    "author": manifest_source.get("author", ""),
                    "depends_on": list(manifest_source.get("depends_on") or []),
                    # The module's owned table prefix, surfaced so the UI can
                    # gate the irreversible Purge action on a module that owns
                    # tables. None when the manifest declares none.
                    "table_prefix": manifest_source.get("table_prefix") or None,
                    # A module built into Celerp is on for every company.
                    "enabled": pkg_name in enabled_names or is_core_folded(pkg_name),
                    # Core-folded modules (ai/backup/connectors) are wired at app
                    # construction, never in loaded_by_name - is_running() counts them.
                    "running": is_running(pkg_name),
                    # Why an enabled module is not running (import error, missing
                    # dependency, license) - the UI shows this instead of silence.
                    "load_error": load_errs.get(pkg_name),
                    # First-party bundled modules cannot be removed from the UI.
                    "is_default": is_default,
                    # A demoted default: named in the committed lock but its
                    # content no longer matches. A per-render fact (no state
                    # carried between scans), so the UI notice can never fire
                    # from anything but a genuine content mismatch.
                    "demoted": (not is_default) and pkg_name in lock_names,
                    # Where the module came from, and when it landed.
                    "source": source,
                    "installed_at": installed_at,
                })
        return results

    return await asyncio.to_thread(_scan_modules)


@router.post("/me/modules/{module_name}/enable", dependencies=[require_permission("manage_company_settings")])
async def enable_module(
    module_name: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Turn a module on for this company, with the modules it needs. Refused
    for a name that is not an installed module."""
    from celerp.modules.registry import (
        commit_with_load_set, enable_for_company, get_enabled, hold_module_state, is_installed, restart_needed,
    )

    await hold_module_state(session)
    if not is_installed(module_name):
        raise HTTPException(status_code=404, detail="Module not found.")
    company = await locked_company(session, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    company.settings, also_enabled = enable_for_company(company.settings, module_name)
    await commit_with_load_set(session)
    return {
        "ok": True, "name": module_name, "enabled": True,
        "also_enabled": also_enabled,
        "restart_required": restart_needed([module_name, *also_enabled]),
        "enabled_modules": sorted(get_enabled(company.settings)),
    }


@router.post("/me/modules/{module_name}/disable", dependencies=[require_permission("manage_company_settings")])
async def disable_module(
    module_name: str,
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Turn a module off for this company. Other companies keep using it. Refused for a
    module built into Celerp, which is always on."""
    from celerp.modules.loader import is_core_folded, module_label
    from celerp.modules.registry import (
        ModuleStillNeeded, commit_with_load_set, disable_for_company, get_enabled, hold_module_state,
    )

    if is_core_folded(module_name):
        raise HTTPException(status_code=409, detail=(
            f"{module_label(module_name)} is part of Celerp and is always on."))

    await hold_module_state(session)
    company = await locked_company(session, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    try:
        company.settings = disable_for_company(company.settings, module_name)
    except ModuleStillNeeded as exc:
        raise HTTPException(status_code=409, detail=(
            f"{', '.join(module_label(n) for n in exc.needed_by)} needs this module. "
            "Turn that off first."))
    await commit_with_load_set(session)
    return {
        "ok": True, "name": module_name, "enabled": False, "restart_required": False,
        "enabled_modules": sorted(get_enabled(company.settings)),
    }


async def _refuse_while_in_use(session: AsyncSession, module_name: str, action: str) -> None:
    """409 while any company uses the module or it is still running."""
    from celerp.modules.loader import is_running
    from celerp.modules.registry import load_set
    if module_name in await load_set(session) or is_running(module_name):
        raise HTTPException(
            status_code=409,
            detail=f"A company still uses this module, or it is still running. "
                   f"Turn it off in every company and restart before {action}.")


@router.post("/me/modules/{module_name}/delete", dependencies=[Depends(require_install_owner)])
async def delete_module(
    module_name: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a non-default module no company uses, freeing its name for re-import.
    Installation owner only.

    Refused for default modules (bundled, undeletable) and for any module a
    company still uses or that is still running. Removing the folder frees the name so the same package can be imported
    again later.
    """
    import asyncio
    from celerp.modules.importer import ModuleImportError, remove_module_dir
    from celerp.modules.loader import is_first_party, resolve_module_path
    from celerp.modules.registry import hold_module_state

    await hold_module_state(session)
    pkg_path = resolve_module_path(module_name)
    if pkg_path is None:
        raise HTTPException(status_code=404, detail="Module not found.")
    # A default is identified by content (its digest matches the committed lock),
    # never by name - so a demoted look-alike can be deleted, and no impostor named
    # after a default is shielded from deletion.
    if is_first_party(pkg_path):
        raise HTTPException(status_code=409, detail="Default modules cannot be deleted.")
    await _refuse_while_in_use(session, module_name, "deleting it")

    try:
        await asyncio.to_thread(remove_module_dir, module_name)
    except ModuleImportError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await session.commit()

    return {"ok": True, "name": module_name}


async def _module_tables_with_row_counts(session: AsyncSession, prefix: str) -> list[dict]:
    """Live tables carrying *prefix*, each with an exact row count.

    Where the purge derives its table set from, server-side from the manifest
    prefix rather than any client input, so the tables that get dropped are
    exactly the ones the prefix owns. An empty prefix matches nothing.
    """
    from sqlalchemy import func, inspect as sa_inspect, select as sa_select, table as sa_table

    if not prefix:
        return []

    def _names(sync_session) -> list[str]:
        return sorted(
            t for t in sa_inspect(sync_session.connection()).get_table_names()
            if t.startswith(prefix))

    names = await session.run_sync(_names)
    tables: list[dict] = []
    for name in names:
        count = (await session.execute(
            sa_select(func.count()).select_from(sa_table(name)))).scalar_one()
        tables.append({"name": name, "rows": count})
    return tables


def _drop_module_tables(sync_session, names: list[str]) -> None:
    """Drop *names* with quoted identifiers: one multi-table DROP on Postgres
    (atomic, no CASCADE) and sequential single drops elsewhere (SQLite has no
    multi-table DROP). A dependent object outside the set makes Postgres fail the
    whole statement, which is the intended all-or-nothing behavior."""
    from sqlalchemy import text

    conn = sync_session.connection()
    quote = conn.dialect.identifier_preparer.quote
    quoted = [quote(n) for n in names]
    if conn.dialect.name == "postgresql":
        conn.execute(text("DROP TABLE " + ", ".join(quoted)))
    else:
        for q in quoted:
            conn.execute(text("DROP TABLE " + q))


def _is_fk_dependency_error(exc: Exception) -> bool:
    """True when a DROP was refused because an object outside the drop set still
    depends on a table in it (Postgres SQLSTATE 2BP01), so the caller can explain
    the refusal in plain words instead of leaking SQL."""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code == "2BP01":
        return True
    return "depend" in str(exc).lower()


@router.post("/me/modules/{module_name}/purge-data", dependencies=[Depends(require_install_owner)])
async def purge_module_data(
    module_name: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Drop every table carrying the module's declared prefix, in one transaction. Installation owner only.

    Refused while any company uses the module or it is still running: its data
    must be quiet before it is dropped. The drop list is re-derived from the manifest prefix server-side; no
    client-sent preview is trusted. A module with no matching tables is a clean
    no-op success. A table outside the module still depending on one of these
    tables blocks the whole drop, which rolls back with a plain explanation.
    Deleting the module folder is a separate action and does not touch these tables.
    """
    from celerp.modules.loader import read_manifest, resolve_module_path
    from celerp.modules.registry import hold_module_state

    await hold_module_state(session)
    pkg_path = resolve_module_path(module_name)
    if pkg_path is None:
        raise HTTPException(status_code=404, detail="Module not found.")
    await _refuse_while_in_use(session, module_name, "purging its data")

    prefix = (read_manifest(pkg_path) or {}).get("table_prefix") or ""
    if prefix:
        # Re-checked here: a module copied in by hand never passed the install check.
        from celerp.modules.importer import table_prefix_problem
        problem = table_prefix_problem(module_name, prefix)
        if problem:
            raise HTTPException(status_code=409,
                                detail=f"Could not purge: {problem} Nothing was deleted.")
    tables = await _module_tables_with_row_counts(session, prefix)
    names = [t["name"] for t in tables]
    if not names:
        return {"ok": True, "name": module_name, "dropped": []}

    try:
        async with session.begin_nested():
            await session.run_sync(_drop_module_tables, names)
        await session.commit()
    except Exception as exc:
        if _is_fk_dependency_error(exc):
            raise HTTPException(
                status_code=409,
                detail=("Could not purge: a table outside this module still "
                        "depends on one of its tables. Nothing was deleted."))
        raise HTTPException(status_code=409, detail="Purge failed; nothing was removed.")
    return {"ok": True, "name": module_name, "dropped": names}


class _ImportPathBody(BaseModel):
    path: str


@router.post("/me/modules/import", dependencies=[Depends(require_install_owner)])
async def import_module_upload(
    request: Request,
    file: UploadFile = File(...),
    source: str = Form("sideloaded"),
) -> dict:
    """Install a module package from an uploaded .zip archive. Installation owner only.

    Validation and installation share one code path with every other way a
    module package arrives (celerp.modules.importer), so the security posture
    cannot drift between surfaces. The module lands DISABLED; enabling and
    restarting are separate, deliberate steps in the modules UI.

    `source` records provenance in the module's sidecar (defaults to a plain
    sideload); the community-import surface passes "community".
    """
    import asyncio
    from celerp.modules.importer import (
        MAX_ARCHIVE_BYTES, ModuleImportError, install_from_zip,
    )
    from celerp.modules.meta import IMPORT_SOURCES

    if source not in IMPORT_SOURCES:
        raise HTTPException(status_code=422, detail="Unknown module source.")
    # Reject on the declared length before reading, then read with a hard cap so
    # an oversize (or lying-Content-Length) body cannot be buffered whole in RAM.
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="Archive too large (limit 50 MB).")
    data = await file.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="Archive too large (limit 50 MB).")
    try:
        info = await asyncio.to_thread(install_from_zip, data, source=source)
    except ModuleImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, **info}


@router.post("/me/modules/import-path", dependencies=[Depends(require_install_owner)])
async def import_module_from_path(body: _ImportPathBody) -> dict:
    """Install a module from a local folder path (desktop folder picker). Installation owner only.

    The API runs on the user's own machine in desktop mode, so a path is the
    natural handoff from the native folder picker. Same importer core as the
    zip upload.
    """
    import asyncio
    from celerp.modules.importer import ModuleImportError, install_from_folder

    try:
        info = await asyncio.to_thread(install_from_folder, body.path)
    except ModuleImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, **info}


def _json_dict(resp) -> dict:
    """The relay is a separately-deployed service that can drift in version; a
    hiccup can return a non-JSON or non-object body. Return its JSON only when it
    is actually a dict, else {} - so callers can .get() without an AttributeError
    turning a relay blip into a raw 500."""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _relay_creds() -> tuple[str, str]:
    """Return relay URL + short-lived JWT from the shared credential exchange."""
    import httpx
    from celerp.config import settings as _s
    from celerp.gateway.state import (
        RelayProtocolError, fetch_relay_bearer, relay_http_url)

    api_key = _s.gateway_token
    if not api_key:
        raise HTTPException(status_code=503,
                            detail="Not signed in to Celerp - connect an account first.")
    relay_base = relay_http_url()
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            token = await fetch_relay_bearer(c, api_key=api_key)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not reach the Celerp relay.")
    except RelayProtocolError:
        raise HTTPException(
            status_code=502,
            detail="Relay returned an unexpected authentication response.")
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f"Could not authenticate with relay ({type(exc).__name__}).")
    return relay_base, token


class _BuyBody(BaseModel):
    slug: str
    kind: str = "monthly"   # monthly | once
    custom_text: str | None = None   # buyer-language purchase disclosures for the Checkout page


@router.post("/me/modules/buy", dependencies=[Depends(require_install_owner)])
async def buy_module(body: _BuyBody) -> dict:
    """Start a purchase: ask the relay for a Stripe Checkout URL for this module.
    The UI opens it in the browser, then polls the license. Installation owner only."""
    import httpx
    url, jwt = await _relay_creds()
    payload: dict = {"slug": body.slug, "kind": body.kind}
    if body.custom_text:
        payload["custom_text"] = body.custom_text
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{url}/marketplace/checkout",
                         json=payload,
                         headers={"Authorization": f"Bearer {jwt}"})
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code,
                            detail=_json_dict(r).get("detail") or "Checkout failed")
    body_json = _json_dict(r)
    if not body_json:
        raise HTTPException(status_code=502, detail="Relay returned an unexpected checkout response.")
    return body_json


@router.get("/me/modules/licenses", dependencies=[require_permission("manage_company_settings")])
async def module_licenses() -> dict:
    """Slugs this instance holds an active license for (for buy/install CTAs)."""
    import httpx
    try:
        url, jwt = await _relay_creds()
    except HTTPException:
        return {"licensed": []}   # not signed in: nothing licensed, no error
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(f"{url}/marketplace/my-licenses",
                            headers={"Authorization": f"Bearer {jwt}"})
        items = r.json().get("items", []) if r.status_code == 200 else []
    except Exception:
        items = []
    licensed = [it.get("module_slug") for it in items
                if it.get("status") == "active" and it.get("module_slug")]
    return {"licensed": licensed}


class _MarketplaceDownloadBody(BaseModel):
    slug: str


class _MarketplaceInstallBody(BaseModel):
    path: str


def _marketplace_staging_dir() -> "Path":
    """Where a licensed marketplace archive waits between Download and Install.
    Server-owned; the client only ever sees an opaque path into it."""
    from pathlib import Path

    from celerp.config import settings as _s

    d = Path(_s.data_dir) / "marketplace-downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_staged_marketplace(path: str) -> tuple[bytes, bool, bool]:
    """Read a staged archive and the trust flags the server recorded beside it,
    refusing any path outside the staging directory so a client-supplied path
    cannot read arbitrary files. official/premium come from the server-written
    sidecar, never from the client: the client only hands back the opaque path,
    so it cannot promote a third-party module to official or a paid one to free.
    """
    import json
    from pathlib import Path

    base = _marketplace_staging_dir().resolve()
    p = Path(path).resolve()
    if base not in p.parents:
        raise HTTPException(status_code=400, detail="Staged archive path is invalid.")
    sidecar = p.with_suffix(".json")
    if not p.is_file() or not sidecar.is_file():
        raise HTTPException(status_code=410,
                            detail="This download has expired. Download it again.")
    try:
        flags = json.loads(sidecar.read_text())
    except (ValueError, OSError):
        raise HTTPException(status_code=410,
                            detail="This download is unreadable. Download it again.")
    flags = flags if isinstance(flags, dict) else {}
    return p.read_bytes(), bool(flags.get("is_official")), bool(flags.get("is_paid"))


@router.post("/me/modules/marketplace-download", dependencies=[Depends(require_install_owner)])
async def marketplace_download(body: _MarketplaceDownloadBody) -> dict:
    """Stage a marketplace module for install: fetch it from the relay and hold
    the archive on disk, ready for a following Install. Installation owner only.

    The relay enforces the gates at token issuance: a paid module needs an active
    license, third-party code needs a passed security scan. Never-stuck by design:
    every Download requests a FRESH one-time token, so any failure - relay down,
    download interrupted - is fully recoverable by clicking Download again. The
    bytes land in the staging area only; nothing is installed until Install.
    """
    import json
    from pathlib import Path

    import httpx

    from celerp.gateway.state import relay_error_detail
    from celerp.modules.importer import MAX_ARCHIVE_BYTES

    url, jwt = await _relay_creds()
    headers = {"Authorization": f"Bearer {jwt}"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            # Module metadata decides the official flag (which allows the reserved
            # celerp- name) and the licence-gate marker.
            m = await c.get(f"{url}/marketplace/modules/{body.slug}")
            if m.status_code != 200:
                raise HTTPException(
                    status_code=404 if m.status_code == 404 else 502,
                    detail=relay_error_detail(m, "This module is not available."))
            meta = _json_dict(m)
            if not meta:
                raise HTTPException(status_code=502, detail="The relay sent an invalid response.")
            is_official = bool(meta.get("is_official"))
            # Type-safe: only a real, positive number counts as paid. A string or
            # other truthy-but-wrong type must not misclassify a free module as
            # paid (which would wrongly gate it behind a license check forever).
            price_monthly = meta.get("price_monthly")
            price_once = meta.get("price_once")
            is_paid = any(
                isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
                for v in (price_monthly, price_once)
            )

            r = await c.post(f"{url}/marketplace/install",
                             json={"slug": body.slug}, headers=headers)
            if r.status_code != 200:
                raise HTTPException(
                    status_code=r.status_code,
                    detail=relay_error_detail(r, "The relay refused the download."))
            token = str(_json_dict(r).get("token") or "")
            if not token:
                raise HTTPException(status_code=502, detail="The relay sent an invalid response.")

            d = await c.get(f"{url}/marketplace/download/{token}")
            if d.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=relay_error_detail(d, "The module download failed. Try again."))
            data = d.content
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Could not reach the Celerp relay. Check your connection and try again.")

    if len(data) > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="Downloaded archive too large (limit 50 MB).")

    # Stage the bytes plus a server-owned sidecar carrying the relay's trust
    # verdict, so Install imports with the right official/paid flags without
    # trusting the client or re-contacting the relay.
    dest = _marketplace_staging_dir() / f"{body.slug}.zip"
    dest.write_bytes(data)
    dest.with_suffix(".json").write_text(
        json.dumps({"is_official": is_official, "is_paid": is_paid}))
    return {"ok": True, "path": str(dest)}


@router.post("/me/modules/marketplace-install", dependencies=[Depends(require_install_owner)])
async def marketplace_install(
    body: _MarketplaceInstallBody,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Install a staged marketplace module through the shared importer. Installation owner only.

    Reads the archive Download staged (and the trust flags the server recorded
    beside it) and installs it exactly like every other module package. The
    module lands DISABLED; enabling and restarting are the same deliberate steps
    in the Installed tab that a community module uses - the two tabs behave the
    same way once the package is on disk. A name mismatch is rejected and nothing
    is left behind, so Install can always be retried.
    """
    import asyncio
    from pathlib import Path

    from celerp.modules.importer import (
        ModuleImportError, install_from_zip, remove_module_dir,
    )
    from celerp.modules.registry import hold_module_state

    data, is_official, is_paid = _read_staged_marketplace(body.path)
    # Held until a mismatched package is gone, so no company can turn it on meanwhile.
    await hold_module_state(session)
    try:
        info = await asyncio.to_thread(
            install_from_zip, data, official=is_official, premium=is_paid,
            source="marketplace")
    except ModuleImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    staged = Path(body.path).resolve()
    slug = staged.stem
    if info["name"] != slug:
        # A package whose manifest name differs from the catalog slug must not
        # stay installed (it would dodge the slug's license/scan identity).
        try:
            await asyncio.to_thread(remove_module_dir, info["name"])
        except ModuleImportError:
            pass
        raise HTTPException(
            status_code=422,
            detail="The downloaded package does not match the requested module.")

    # Landed on disk: drop the staged archive and its sidecar.
    staged.unlink(missing_ok=True)
    staged.with_suffix(".json").unlink(missing_ok=True)
    return {"ok": True, **info}


class CompanyReset(BaseModel):
    company_name: str


@router.post("/me/reset", dependencies=[require_permission("manage_company_lifecycle")])
async def reset_company(
    payload: CompanyReset,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Remove the current company: its records, settings, chart of accounts, attachments
    and memberships. Logins and other companies stay.

    The typed name must equal the company's name exactly. All or nothing: files go only
    after the commit. Returns a token pair for another of the caller's companies, or
    ``{"next": "start_company"}`` when this was their last one."""
    from celerp.connectors.ownership import lock_connector_maintenance
    from celerp.services import company_reset, payments
    from celerp.services.migrations import run_cleanup_task

    await lock_connector_maintenance(session)
    # A reset that waited for another to finish finds the company gone, not its role.
    if not await lock_company_for_deletion(session, ctx.company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    await locked_authority(session, ctx.company_id, ctx.user.id, ("manage_company_lifecycle",))
    company = await session.get(Company, ctx.company_id)
    closure = task_id = None
    try:
        try:
            done = await company_reset.reset(session, company, payload.company_name)
        except company_reset.ResetRefused as exc:
            closure = exc.closure
            if exc.__cause__ is not None:
                logger.error("Company reset failed: %s", type(exc.__cause__).__name__)
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        closure, task_id = done.closure, done.task_id
        link = await first_usable_company_link(session, ctx.user.id)
        if link is None:
            await session.commit()
            result = {"next": "start_company"}
        else:
            # The new session continues this one, so it cannot jump a concurrent sign-out.
            result = await issue_token_pair(
                session, user=ctx.user, company_id=link.company_id,
                expected_snonce=ctx.snonce,
            )
    except BaseException:
        await session.rollback()
        # Reopens the company's online payments, or closes them for good if the
        # deletion committed after all.
        await payments.settle_company_closure(closure)
        # A commit that landed but reported failure leaves the cleanup task behind;
        # one that did not land leaves none, and this finds nothing to do.
        if task_id is not None:
            session.expunge_all()
            await run_cleanup_task(session, task_id)
        raise
    await payments.settle_company_closure(closure)
    session.expunge_all()
    await run_cleanup_task(session, task_id)
    return result


@router.delete("/me", dependencies=[require_permission("manage_company_lifecycle")])
async def deactivate_company(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Soft-delete the current company and disconnect its external connectors.

    Business records are preserved. Its connectors are disconnected so they
    can be connected again later.
    """
    import time as _time
    import re as _re2
    import sqlalchemy as sa
    from celerp.connectors.ownership import (
        PRODUCT_CHANNEL_PLATFORMS,
        RESET_STATUS_DEACTIVATED,
        lock_connector_maintenance,
        record_connector_reset,
    )
    from celerp.connectors.remote_state import (
        ConnectorRemoteCleanupError,
        revoke_connector_remote_state,
    )
    from celerp.models.connector_config import ConnectorConfig, OutboundQueue

    await lock_connector_maintenance(session)
    company = await session.get(
        Company, company_id, with_for_update=True, populate_existing=True
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    company_id_str = str(company_id)
    configs = list((await session.scalars(
        sa.select(ConnectorConfig)
        .where(ConnectorConfig.company_id == company_id_str)
        .with_for_update()
    )).all())
    # Product links become history before anything is revoked remotely, so a
    # failure here leaves the company and its connections untouched.
    import celerp_inventory.services as inventory_services
    for platform in PRODUCT_CHANNEL_PLATFORMS:
        try:
            await inventory_services.detach_external_links_for_platform(
                session, company_id_str, platform
            )
        except Exception as exc:
            await session.rollback()
            raise HTTPException(
                status_code=503,
                detail=f"Could not disconnect {platform}; the company was not deactivated.",
            ) from exc
    for config in configs:
        connector_name = config.connector
        webhook_ids = list(config.webhook_ids or [])
        try:
            await revoke_connector_remote_state(
                company_id_str,
                connector_name,
                webhook_ids=webhook_ids,
            )
        except ConnectorRemoteCleanupError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Could not disconnect {connector_name}; "
                    "the company was not deactivated."
                ),
            ) from exc

    company.is_active = False
    connectors = {config.connector for config in configs}
    connectors.update((await session.scalars(
        sa.select(OutboundQueue.connector).where(
            OutboundQueue.company_id == company_id_str
        )
    )).all())
    for connector in connectors:
        record_connector_reset(
            session, company_id_str, connector, status=RESET_STATUS_DEACTIVATED
        )
    await session.execute(
        sa.delete(OutboundQueue).where(
            OutboundQueue.company_id == company_id_str
        )
    )
    await session.execute(
        sa.delete(ConnectorConfig).where(
            ConnectorConfig.company_id == company_id_str
        )
    )
    # Free the slug so the user can re-create a company with the same name later.
    # Strip any previous deactivated suffix first (idempotent), then append new one.
    base_slug = _re2.sub(r"-deactivated-\d+$", "", company.slug)
    company.slug = f"{base_slug}-deactivated-{int(_time.time())}"
    await session.commit()
    return {"ok": True, "company_id": str(company_id), "is_active": False}


@router.post("/me/reactivate", dependencies=[require_permission("manage_company_lifecycle")])
async def reactivate_company(
    company_id=Depends(get_current_company_id),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reactivate the session's company, if it was deactivated. Owner only.

    Connectors disconnected by the deactivation stay disconnected; their names
    are returned so the caller can prompt for an explicit reconnect."""
    try:
        done = await company_lifecycle.reactivate_company(session, company_id, user.id)
    except company_lifecycle.NotAnOwner as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    return {
        "ok": True,
        "company_id": str(done.company_id),
        "is_active": True,
        "connectors_to_reconnect": done.connectors_to_reconnect,
    }


# ---------------------------------------------------------------------------
# Price lists
# ---------------------------------------------------------------------------

from celerp.services.pricing import (
    COST_PRICE_LIST_NAMES,
    DEFAULT_PRICE_LIST_NAME,
    is_derived,
    normalized_price_lists,
    price_config_error,
)

DEFAULT_PRICE_LISTS: list[dict] = [
    {"name": "Retail", "description": "Standard retail price"},
    {"name": "Wholesale", "description": "Wholesale / trade price"},
]


class PriceListsPatch(BaseModel):
    price_lists: list[dict]
    # Set together with price_lists so renaming the base list stays consistent in one write.
    base_price_list: str | None = None


class PriceListNamePatch(BaseModel):
    name: str


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
    if existing is None:
        company = await locked_company(session, company_id)
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
    if not role_has_permission(company.settings, role, "view_inventory_costs"):
        # Without view_inventory_costs: no cost lists, and names/descriptions only. A
        # derived list's factor stays gated because price ÷ factor would reveal a
        # cost-based base.
        price_lists = [
            {"name": pl.get("name", ""), "description": pl.get("description", "")}
            for pl in price_lists
            if pl.get("name", "").lower() not in COST_PRICE_LIST_NAMES
        ]
    return price_lists


@router.patch("/me/price-lists")
async def patch_price_lists(
    payload: PriceListsPatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    error = price_config_error(payload.price_lists,
                               payload.base_price_list or settings.get("base_price_list"),
                               settings.get("price_lists") or [])
    if error:
        raise HTTPException(status_code=422, detail=error)
    settings["price_lists"] = normalized_price_lists(payload.price_lists)
    if payload.base_price_list is not None:
        settings["base_price_list"] = payload.base_price_list
    company.settings = settings
    await session.commit()
    return {"ok": True}


@router.get("/me/base-price-list")
async def get_base_price_list(
    company_id=Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> str:
    company = await session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    return company.settings.get("base_price_list") or DEFAULT_PRICE_LIST_NAME


@router.patch("/me/base-price-list")
async def patch_base_price_list(
    payload: PriceListNamePatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    price_lists = settings.get("price_lists") or []
    target = next((pl for pl in price_lists if pl.get("name") == payload.name), None)
    if target is None:
        raise HTTPException(status_code=422, detail=f"Base price list '{payload.name}' does not exist")
    if is_derived(target):
        raise HTTPException(
            status_code=422,
            detail=f"'{payload.name}' is derived from the base price list and cannot be the base itself",
        )
    settings["base_price_list"] = payload.name
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
    payload: PriceListNamePatch,
    company_id=Depends(get_current_company_id),
    _: None = require_permission("manage_company_settings"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Not found")
    settings = dict(company.settings)
    settings["default_price_list"] = payload.name
    company.settings = settings
    await session.commit()
    return {"ok": True}


class BusinessTypeIn(BaseModel):
    vertical: str


@router.post("/me/business-type", dependencies=[require_permission("manage_company_lifecycle")])
async def set_company_business_type(
    payload: BusinessTypeIn,
    company_id=Depends(get_current_company_id),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set the company's business type. The only way settings["vertical"] changes."""
    from celerp.services.business_type import UnknownBusinessType, set_business_type

    try:
        return await set_business_type(session, company_id, user.id, payload.vertical)
    except UnknownBusinessType:
        raise HTTPException(status_code=422, detail=f"Unknown business type: {payload.vertical!r}")


@router.post("/me/demo/reseed", dependencies=[require_permission("manage_company_lifecycle")])
async def reseed_demo_items(
    company_id=Depends(get_current_company_id),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Replace the untouched demo items with the set for the company's current business type.

    Real inventory and demo items the user edited or used are never removed.
    """
    from celerp.services.demo import replace_demo_items

    company = await locked_company(session, company_id)
    vertical = (company.settings or {}).get("vertical") if company else None
    counts = await replace_demo_items(session, company_id, user.id, vertical)
    await session.commit()
    return {"ok": True, "vertical": vertical, **counts}

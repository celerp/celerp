# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company provisioning: the one place a company, its owner link and its starter data are created.

Three entry points share the same core rows:

- ``provision_registered_company``: the first owner of a fresh install, with the demo catalogue.
- ``provision_additional_company``: an owner adding another company to an existing account.
- ``provision_migration_company``: an inactive company staged for a migration. It gets no module
  seeds and no lifecycle hooks, so the imported books are the only data in it until the migration
  is finished and ``add_missing_required_defaults`` fills in what Celerp still needs.

None of these commit; the caller owns the transaction.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp.services.auth import hash_password
from celerp.services.company_lock import locked_company

DEFAULT_LOCATION_NAME = "Head Office"
DEFAULT_LOCATION_TYPE = "office"
DEFAULT_FISCAL_YEAR_START = "01-01"


async def unique_slug(session: AsyncSession, name: str) -> str:
    """A company web address made from ``name`` that no other company uses."""
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or uuid.uuid4().hex[:12]
    taken = set((await session.execute(
        select(Company.slug).where((Company.slug == base) | Company.slug.like(f"{base}-%"))
    )).scalars())
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


async def _create_company(
    session: AsyncSession, *, owner: User, company_name: str, settings: dict, staged: bool = False,
    company_id: uuid.UUID | None = None,
) -> Company:
    company = Company(
        id=company_id or uuid.uuid4(), name=company_name, slug=await unique_slug(session, company_name),
        settings=settings, is_active=not staged, is_migration_staged=staged,
    )
    session.add(company)
    await session.flush()  # the company row must exist before the user_companies FK
    session.add(UserCompany(id=uuid.uuid4(), user_id=owner.id, company_id=company.id, role="owner"))
    await session.flush()
    return company


def _default_location(company_id: uuid.UUID) -> Location:
    return Location(
        id=uuid.uuid4(), company_id=company_id, name=DEFAULT_LOCATION_NAME,
        type=DEFAULT_LOCATION_TYPE, address=None, is_default=True,
    )


async def _fire_company_created(session: AsyncSession, company_id: uuid.UUID) -> None:
    # Module lifecycle hooks stay best-effort: fire_lifecycle logs a module error without
    # failing the caller's transaction.
    from celerp.modules import slots

    await slots.fire_lifecycle("on_company_created", session=session, company_id=company_id)


async def create_install_owner(session: AsyncSession, *, name: str, email: str, password: str) -> User:
    """Create the first user of a fresh install."""
    user = User(
        id=uuid.uuid4(), email=email, name=name, auth_hash=hash_password(password),
        api_key=None, is_active=True, is_install_owner=True,
    )
    session.add(user)
    await session.flush()
    return user


async def provision_registered_company(
    session: AsyncSession, *, company_name: str, owner_name: str, email: str, password: str,
) -> tuple[Company, User]:
    """Create the install owner, their first company, the default location and the demo data."""
    from celerp.services import demo

    user = await create_install_owner(session, name=owner_name, email=email, password=password)
    company = await _create_company(
        session, owner=user, company_name=company_name,
        settings={"fiscal_year_start": DEFAULT_FISCAL_YEAR_START},
    )
    await _fire_company_created(session, company.id)
    head_office = _default_location(company.id)
    session.add(head_office)
    await session.flush()  # demo items land in the default location
    await demo.seed_demo_items(session, company.id, user.id, default_location_id=head_office.id)
    await demo.seed_self_contacts(
        session, company_id=company.id, actor_id=user.id, person_name=owner_name,
        company_name=company_name, email=email,
    )
    return company, user


async def provision_additional_company(session: AsyncSession, *, user: User, company_name: str) -> Company:
    """Create another company owned by *user*, with its self-contact and default location."""
    from celerp.services import demo

    company = await _create_company(session, owner=user, company_name=company_name, settings={})
    await _fire_company_created(session, company.id)
    await demo.seed_self_contacts(
        session, company_id=company.id, actor_id=user.id, person_name=user.name,
        company_name=company_name, email=user.email,
    )
    session.add(_default_location(company.id))
    await session.flush()
    return company


async def provision_migration_company(session: AsyncSession, *, owner: User, company_name: str) -> Company:
    """Create an inactive, migration-staged company: the owner link only, no seeds, no hooks."""
    return await _create_company(session, owner=owner, company_name=company_name, settings={}, staged=True)


async def provision_restored_company(
    session: AsyncSession, *, owner: User, company_name: str, company_id: uuid.UUID, settings: dict,
) -> Company:
    """Create an empty company for a restored company backup; its records come from the backup."""
    return await _create_company(session, owner=owner, company_name=company_name, settings=settings,
                                 company_id=company_id)


async def ensure_default_location(session: AsyncSession, company_id: uuid.UUID) -> Location:
    """The company's default location, settled deterministically.

    An existing default is kept; otherwise the oldest location becomes the default;
    otherwise the standard default location is created. Only one is ever marked default.
    """
    locations = list((await session.execute(
        select(Location).where(Location.company_id == company_id).order_by(Location.created_at, Location.id)
    )).scalars())
    default = next((loc for loc in locations if loc.is_default), None)
    if default is None and locations:
        default = locations[0]
        default.is_default = True
    elif default is None:
        default = _default_location(company_id)
        session.add(default)
    await session.flush()
    return default


async def add_missing_required_defaults(session: AsyncSession, company_id: uuid.UUID) -> None:
    """Fill in only what a migrated company still lacks: a default location and a fiscal year start."""
    company = await locked_company(session, company_id)
    await ensure_default_location(session, company_id)
    if not (company.settings or {}).get("fiscal_year_start"):
        company.settings = {**(company.settings or {}), "fiscal_year_start": DEFAULT_FISCAL_YEAR_START}
    await session.flush()

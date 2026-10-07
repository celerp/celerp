# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Bringing a deactivated company back.

Every way of reactivating a company goes through ``reactivate_company``: the owner's
own reactivation of the company their session is on, and the reactivation offered when
a backup is restored that was already restored here as a company later deactivated.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.services.provisioning import unique_slug

NOT_AN_OWNER = "Only an owner of this company can reactivate it."

_DEACTIVATED_SUFFIX = re.compile(r"-deactivated-\d+$")


class NotAnOwner(Exception):
    """The caller has no active owner membership of the company."""


@dataclass(frozen=True)
class Reactivated:
    company_id: uuid.UUID
    company_name: str
    reactivated: bool
    connectors_to_reconnect: list[str]


async def reactivate_company(session: AsyncSession, company_id, user_id) -> Reactivated:
    """Reactivate the company for one of its owners and commit.

    The company row is locked first, so of two simultaneous reactivations one does the
    work and the other finds the company already active and returns it unchanged. The
    web address it had before the deactivation comes back unless another company took it
    meanwhile, in which case it gets a free one. Connectors the deactivation disconnected
    stay disconnected; their names are returned so the owner can connect them again."""
    from celerp.connectors import ownership
    company = await session.get(Company, uuid.UUID(str(company_id)), with_for_update=True, populate_existing=True)
    owner = company is not None and await session.scalar(select(UserCompany.id).where(
        UserCompany.user_id == uuid.UUID(str(user_id)), UserCompany.company_id == company.id,
        UserCompany.role == "owner", UserCompany.is_active.is_(True)).limit(1)) is not None
    if not owner:
        await session.rollback()
        raise NotAnOwner(NOT_AN_OWNER)
    if company.is_active:
        done = Reactivated(company_id=company.id, company_name=company.name, reactivated=False,
                           connectors_to_reconnect=[])
        await session.rollback()
        return done
    company.is_active = True
    company.slug = await unique_slug(session, _DEACTIVATED_SUFFIX.sub("", company.slug))
    reconnect = await ownership.connectors_awaiting_reconnect(session, company.id)
    done = Reactivated(company_id=company.id, company_name=company.name, reactivated=True,
                       connectors_to_reconnect=reconnect)
    await session.commit()
    return done

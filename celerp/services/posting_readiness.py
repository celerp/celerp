# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Which posting accounts a company's workflows need, and telling the user when one is missing.

Only the roles of workflows the company actually uses are needed: sales and purchasing
always, tax once taxes appear, inventory once stock exists, and landed cost, foreign
currency and fixed assets once the books hold them. Any other role stays unmapped until
its first use asks for it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    POSTING_ACCOUNTS_PATH,
    ROLE_GROUPS,
    ROLE_LABELS,
    SOURCE_CONTROLS_KEY,
    AccountRole,
)
from celerp.models.projections import Projection
from celerp.services.account_roles import current_settings, unmapped_roles

NOTICE_CATEGORY = "accounting"
NOTICE_TITLE = "Posting accounts need attention"


def _line_items(state: dict) -> list[dict]:
    return [li for li in (state.get("line_items") or []) if isinstance(li, dict)]


async def used_groups(session: AsyncSession, company_id, settings: dict) -> set[str]:
    """The role groups (accounting_roles.ROLE_GROUPS) the company's books or source
    books show it uses."""
    groups = {"core"}
    controls = settings.get(SOURCE_CONTROLS_KEY) or {}
    for group in ("tax", "inventory"):
        if any(controls.get(role.value) for role in ROLE_GROUPS[group]):
            groups.add(group)
    base = str(settings.get("currency") or "USD").upper()
    rows = await session.execute(select(Projection.entity_type, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type.in_(("item", "doc"))))
    for entity_type, state in rows:
        state = state or {}
        if entity_type == "item":
            groups.add("inventory")
            if state.get("landed_contributions"):
                groups.add("landed_cost")
            continue
        if float(state.get("tax") or 0):
            groups.add("tax")
        if str(state.get("currency") or base).upper() != base:
            groups.add("fx")
        for li in _line_items(state):
            if li.get("landed_cost_kind"):
                groups.add("landed_cost")
            if str(li.get("receive_as") or "").lower() == "asset":
                groups.add("fixed_assets")
    return groups


def needed_roles(groups: set[str]) -> list[str]:
    return [role.value for group in ROLE_GROUPS if group in groups for role in ROLE_GROUPS[group]]


async def notify_unmapped(session: AsyncSession, company_id) -> bool:
    """One high-priority notice when a role the company's workflows need has no
    account, deduped on the unread notice so a restart never stacks them. Returns
    whether a notice was created. The caller commits."""
    from celerp.models.notification import Notification
    from celerp.notifications import service as notification_service

    settings = await current_settings(session, company_id)
    needed = set(needed_roles(await used_groups(session, company_id, settings)))
    missing = [r for r in unmapped_roles(settings) if r in needed]
    if not missing:
        return False
    already = (await session.execute(select(Notification.id).where(
        Notification.company_id == company_id, Notification.category == NOTICE_CATEGORY,
        Notification.title == NOTICE_TITLE, Notification.read == False,  # noqa: E712
    ).limit(1))).first()
    if already:
        return False
    labels = ", ".join(ROLE_LABELS[AccountRole(r)] for r in missing)
    await notification_service.create(
        session, company_id, NOTICE_CATEGORY, NOTICE_TITLE,
        f"Choose the account for: {labels}. Until then, anything that posts to them is refused.",
        action_url=POSTING_ACCOUNTS_PATH, priority="high",
    )
    return True

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Which posting accounts a company's workflows need, choosing them, and telling the
user when one is missing.

Only the roles of workflows the company actually uses are needed: sales and purchasing
always, tax once taxes appear, inventory once stock exists, and landed cost, foreign
currency and fixed assets once the books hold them. Any other role stays unmapped until
its first use asks for it.

A migrated company arrives with its source's chart and no posting accounts. Finishing
the migration sets them (``apply_choices``): the source's own control account where it
names exactly one, otherwise the user's choice of an existing account or an added one.
The chart belongs to the accounting module, which lists it through the
``chart_accounts`` slot and adds accounts through ``add_chart_account``.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import (
    POSTING_ACCOUNTS_PATH,
    POSTING_ROLES_SCHEMA,
    POSTABLE_ROLES,
    ROLE_GROUPS,
    ROLE_LABELS,
    ROLE_TYPES,
    SCHEMA_KEY,
    SCOPES_KEY,
    SEEDED_TARGETS,
    SOURCE_CONTROLS_KEY,
    AccountRole,
    allowed_types,
    is_role,
    target_problem,
)
from celerp.models.projections import Projection
from celerp.services.account_roles import (
    current_settings,
    role_map,
    scope_list,
    source_controls,
    target_problems,
    unmapped_roles,
    with_role,
)

NOTICE_CATEGORY = "accounting"
NOTICE_TITLE = "Posting accounts need attention"

CHART_SLOT = "chart_accounts"
ADD_ACCOUNT_SLOT = "add_chart_account"

_GROUP_OF: dict[str, str] = {role.value: group for group, roles in ROLE_GROUPS.items() for role in roles}


class ReadinessError(ValueError):
    """The posting-account choices cannot be saved; the message says why."""


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


async def _chart(session: AsyncSession, company_id) -> dict[str, dict] | None:
    from celerp.modules.slots import get, resolve_handler

    contributions = get(CHART_SLOT)
    if not contributions:
        return None
    rows = await resolve_handler(contributions[0]["handler"])(session, company_id)
    return {row["code"]: row for row in rows}


async def account_names(session: AsyncSession, company_id, codes) -> dict[str, str]:
    """The chart's name for each of ``codes``; a code the chart does not hold, or a
    company without the accounting module, falls back to the code itself."""
    chart = await _chart(session, company_id) or {}
    return {code: (chart.get(code) or {}).get("name") or code for code in codes}


def _candidate_types(role: str) -> frozenset[str]:
    """Types an account may have to be offered for ``role``: exchange gain and loss may
    share one account of either type."""
    if role in (AccountRole.FX_GAIN.value, AccountRole.FX_LOSS.value):
        return allowed_types(role, {AccountRole.FX_GAIN.value: "*", AccountRole.FX_LOSS.value: "*"})
    return ROLE_TYPES[AccountRole(role)]


def _fits(role: str, account: dict) -> bool:
    return (account.get("is_active", True) and account.get("account_type") in _candidate_types(role)
            and not (AccountRole(role) in POSTABLE_ROLES and account.get("has_children")))


def _ranked(role: str, chart: dict[str, dict], controls: list[str]) -> list[dict]:
    """Accounts that can serve ``role``: the source's controls first, then accounts whose
    name shares a word with the role's, then by code. The order only helps the user find
    one; it never chooses."""
    words = {w for w in ROLE_LABELS[AccountRole(role)].lower().split() if len(w) > 3}

    def rank(account: dict) -> tuple:
        named = bool(words & set(str(account.get("name") or "").lower().split()))
        return (account["code"] not in controls, not named, account["code"])

    return [{k: a[k] for k in ("code", "name", "account_type")}
            for a in sorted((a for a in chart.values() if _fits(role, a)), key=rank)]


def _proposals(chart: dict[str, dict]) -> dict[str, dict]:
    """The account to add for each role when the chart has none to offer: the default
    code, or when the chart already holds it, the first free ``<code>-n``. Proposals
    never share a code, so exchange gain and loss are proposed as two accounts."""
    taken = set(chart)
    out: dict[str, dict] = {}
    for role in AccountRole:
        base = SEEDED_TARGETS[role]
        code, n = base, 0
        while code in taken:
            n += 1
            code = f"{base}-{n}"
        taken.add(code)
        out[role.value] = {"code": code, "name": ROLE_LABELS[role], "account_type": sorted(ROLE_TYPES[role])[0]}
    return out


async def readiness(session: AsyncSession, company_id) -> list[dict] | None:
    """Every posting role with what finishing a migration needs to know about it, or
    None when accounting is not running.

    Each row: role, label, group, required (a workflow the company uses needs it),
    current (the account already set), controls (the source books' control accounts),
    preselect (the single suitable control, when there is exactly one), candidates
    (suitable chart accounts, ranked) and proposal (the account to add instead)."""
    chart = await _chart(session, company_id)
    if chart is None:
        return None
    settings = await current_settings(session, company_id)
    needed = set(needed_roles(await used_groups(session, company_id, settings)))
    current = role_map(settings)
    proposals = _proposals(chart)
    rows = []
    for role in AccountRole:
        controls = source_controls(settings, role.value)
        fitting = [c for c in controls if c in chart and _fits(role.value, chart[c])]
        rows.append({
            "role": role.value, "label": ROLE_LABELS[role], "group": _GROUP_OF[role.value],
            "required": role.value in needed, "current": current.get(role.value) or None,
            "controls": controls, "preselect": fitting[0] if len(controls) == 1 and fitting else None,
            "candidates": _ranked(role.value, chart, controls), "proposal": proposals[role.value],
        })
    return rows


def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReadinessError(f"Each added account needs a {what}.")
    return value.strip()


def _new_accounts(add_accounts) -> list[dict]:
    if add_accounts is None:
        return []
    if not isinstance(add_accounts, list) or not all(isinstance(a, dict) for a in add_accounts):
        raise ReadinessError("Added accounts must be a list of accounts.")
    out = []
    for a in add_accounts:
        role = a.get("role")
        if not is_role(role):
            raise ReadinessError(f"Unknown posting role: {role}.")
        out.append({"role": role, "code": _text(a.get("code"), "code"), "name": _text(a.get("name"), "name"),
                    "account_type": _text(a.get("account_type"), "type")})
    return out


def _chosen(roles) -> dict[str, str]:
    if roles is None:
        return {}
    if not isinstance(roles, dict):
        raise ReadinessError("Posting accounts must map each role to an account code.")
    out = {}
    for role, code in roles.items():
        if not is_role(role):
            raise ReadinessError(f"Unknown posting role: {role}.")
        if code in (None, ""):
            continue
        if not isinstance(code, str):
            raise ReadinessError(f"Choose an account code for {ROLE_LABELS[AccountRole(role)].lower()}.")
        out[role] = code.strip()
    return out


async def apply_choices(session: AsyncSession, company_id, choices: dict | None) -> None:
    """Set the company's posting accounts from ``choices`` ({"roles": {role: code},
    "add_accounts": [{role, code, name, account_type}]}) and the source books' single
    controls, adding the chosen new accounts. Every recorded control joins its role's
    history. A role already set is kept: choosing another account for it is refused,
    so a retry cannot overwrite a choice made in between. Raises ReadinessError when a
    needed role is left without an account or a choice cannot take it; the caller owns
    the transaction and rolls it back."""
    from celerp.modules.slots import get, resolve_handler
    from celerp.services.company_lock import lock_chart, locked_company
    from celerp.services.journal_accounts import lock_accounts

    choices = choices or {}
    if not isinstance(choices, dict):
        raise ReadinessError("Posting accounts must map each role to an account code.")
    chosen = _chosen(choices.get("roles"))
    added = _new_accounts(choices.get("add_accounts"))
    company = await locked_company(session, company_id)
    await lock_chart(session, company_id)
    rows = await readiness(session, company_id)
    if rows is None:
        return
    chart = await _chart(session, company_id)
    for account in added:
        if account["role"] in chosen and chosen[account["role"]] != account["code"]:
            raise ReadinessError(f"{ROLE_LABELS[AccountRole(account['role'])]} has two accounts chosen.")
        chosen[account["role"]] = account["code"]
        existing = chart.get(account["code"])
        if existing is not None and (existing["name"], existing["account_type"]) != (
                account["name"], account["account_type"]):
            raise ReadinessError(f"Account code {account['code']} is already in use. Choose that account "
                                 "or add one with another code.")
    final: dict[str, str] = {}
    problems: list[str] = []
    unchosen: list[str] = []
    for row in rows:
        role, label = row["role"], row["label"]
        code = chosen.get(role)
        if row["current"]:
            if code and code != row["current"]:
                problems.append(f"{label} is already set to account {row['current']}; "
                                "change it in Settings after finishing.")
            continue
        code = code or row["preselect"]
        if code:
            final[role] = code
        elif row["required"]:
            unchosen.append(label)
    if unchosen:
        problems.append(f"Choose the posting account for: {', '.join(unchosen)}.")
    if problems:
        raise ReadinessError(" ".join(problems))
    add = resolve_handler(get(ADD_ACCOUNT_SLOT)[0]["handler"])
    for account in added:
        if account["code"] not in chart and final.get(account["role"]) == account["code"]:
            try:
                await add(session, company_id, code=account["code"], name=account["name"],
                          account_type=account["account_type"])
            except HTTPException as exc:
                raise ReadinessError(str(exc.detail)) from None
            chart[account["code"]] = {**account, "is_active": True, "has_children": False}
    held = await lock_accounts(session, company_id, set(final.values())) or {}
    trial = {**role_map(company.settings), **final}
    for role, code in final.items():
        problem = target_problem(role, trial, held.get(code))
        if problem:
            problems.append(problem)
    if problems:
        raise ReadinessError(" ".join(problems))
    settings = {**(company.settings or {}), SCHEMA_KEY: POSTING_ROLES_SCHEMA}
    settings.setdefault(SOURCE_CONTROLS_KEY, {})
    for role, code in final.items():
        settings = with_role(settings, role, code)
    scopes = {k: list(v) for k, v in (settings.get(SCOPES_KEY) or {}).items()}
    for role, codes in settings[SOURCE_CONTROLS_KEY].items():
        scopes.setdefault(role, []).extend(c for c in codes if c not in scopes[role])
    company.settings = {**settings, SCOPES_KEY: scopes}
    await session.flush()


def _status(role: str, current: dict[str, str], chart: dict[str, dict], required: bool) -> tuple[str, str | None]:
    """Whether the role's account can take new postings: ready, missing, inactive or
    wrong_type, with the reason; unused when no account is set and nothing needs one."""
    code = current.get(role)
    if not code:
        return ("missing", target_problems([role], current, None)[role]) if required else ("unused", None)
    account = chart.get(code)
    problem = target_problem(role, current, account)
    if problem is None:
        return "ready", None
    if account is None:
        return "missing", problem
    return ("inactive" if not account.get("is_active", True) else "wrong_type"), problem


async def _older_stock(session: AsyncSession, company_id, settings: dict, chart: dict[str, dict]) -> dict:
    """Older stock on hand that records no inventory account (lot_origin), and the
    accounts each may be picked from: those that have held purchased or opening inventory."""
    from celerp.services.lot_origin import unrecorded_lots

    held = {c for role in (AccountRole.INVENTORY_PURCHASED.value, AccountRole.INVENTORY_OPENING.value)
            for c in scope_list(settings, role)}
    return {"lots": await unrecorded_lots(session, company_id),
            "candidates": [{k: chart[c][k] for k in ("code", "name", "account_type")}
                           for c in sorted(held) if c in chart]}


async def panel(session: AsyncSession, company_id) -> dict | None:
    """Settings > Accounting > Posting accounts: every role with its account, status,
    the accounts it served before (``earlier``, where existing balances stay) and the
    accounts that can serve it; plus older stock that records no inventory account. None when
    accounting is not running."""
    chart = await _chart(session, company_id)
    if chart is None:
        return None
    settings = await current_settings(session, company_id)
    needed = set(needed_roles(await used_groups(session, company_id, settings)))
    current = role_map(settings)
    rows = []
    for role in AccountRole:
        code = current.get(role.value) or None
        status, problem = _status(role.value, current, chart, role.value in needed)
        rows.append({
            "role": role.value, "label": ROLE_LABELS[role], "group": _GROUP_OF[role.value],
            "required": role.value in needed, "code": code,
            "name": (chart.get(code) or {}).get("name") if code else None,
            "status": status, "problem": problem,
            "earlier": [c for c in scope_list(settings, role.value) if c != code],
            "candidates": _ranked(role.value, chart, []),
        })
    return {"roles": rows, "older_stock": await _older_stock(session, company_id, settings, chart)}

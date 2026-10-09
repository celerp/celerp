# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Which posting accounts a company's workflows need, choosing them, and telling the
user when one is missing.

Only the roles of workflows the company actually uses are needed: sales and purchasing
always, tax once taxes appear, inventory once stock exists, work in progress once a
production run exists, and landed cost, foreign currency and fixed assets once the
books hold them. Any other role stays unmapped until
its first use asks for it.

A migrated company arrives with its source's chart and no posting accounts. Finishing
the migration sets them (``apply_choices``): the source's own control account where it
names exactly one, otherwise the user's choice of an existing account or an added one.
The chart belongs to the accounting module, which lists it and adds accounts through
the chart it registers (``celerp.services.journal_accounts.chart_access``).
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
    refusal,
    target_problem,
    unknown_role,
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

_GROUP_OF: dict[str, str] = {role.value: group for group, roles in ROLE_GROUPS.items() for role in roles}


class ReadinessError(ValueError):
    """The posting-account choices cannot be saved; ``detail`` is the refusal saying why
    (``accounting_roles.refusal``), or the chart's own message when the chart refused an
    added account."""

    def __init__(self, detail: dict | str):
        super().__init__(detail["message"] if isinstance(detail, dict) else detail)
        self.detail = detail


def _refused(problems: list[dict]) -> ReadinessError:
    """Every problem with the choices, in one refusal."""
    if len(problems) == 1:
        return ReadinessError(problems[0])
    return ReadinessError(refusal("posting.problems", " ".join(p["message"] for p in problems),
                                  problems=problems))


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
        Projection.company_id == company_id, Projection.entity_type.in_(("item", "doc", "mfg_order"))))
    for entity_type, state in rows:
        state = state or {}
        if entity_type == "mfg_order":
            groups.add("manufacturing")
            continue
        if entity_type == "item":
            groups.add("inventory")
            if state.get("landed_costs"):
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


def _labels(roles: list[str]) -> str:
    return ", ".join(ROLE_LABELS[AccountRole(r)] for r in roles)


async def notify_unmapped(session: AsyncSession, company_id) -> bool:
    """One high-priority notice when a role the company's workflows need has no
    account, deduped on the standing notice so a restart never stacks them. Returns
    whether a notice was created. The caller commits."""
    from celerp.notifications import service as notification_service

    settings = await current_settings(session, company_id)
    needed = set(needed_roles(await used_groups(session, company_id, settings)))
    missing = [r for r in unmapped_roles(settings) if r in needed]
    if not missing:
        return False
    if await notification_service.has_standing(session, company_id, NOTICE_CATEGORY, NOTICE_TITLE):
        return False
    labels = _labels(missing)
    await notification_service.create(
        session, company_id, NOTICE_CATEGORY, NOTICE_TITLE,
        f"Choose the account for: {labels}. Until then, anything that posts to them is refused.",
        action_url=POSTING_ACCOUNTS_PATH, priority="high",
        i18n={"title": "notice.posting_unmapped.title", "body": "notice.posting_unmapped.body",
              "params": {"roles": missing}},
    )
    return True


async def _chart(session: AsyncSession, company_id) -> dict[str, dict] | None:
    from celerp.services.journal_accounts import chart_access

    access = chart_access()
    if access is None:
        return None
    rows = await access.list_accounts(session, company_id)
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


def _brief(account: dict) -> dict:
    """An account as the pickers and labels need it (accounting_roles.account_label)."""
    return {k: account[k] for k in ("code", "name", "account_type", "code_generated")}


def _ranked(role: str, chart: dict[str, dict], controls: list[str]) -> list[dict]:
    """Accounts that can serve ``role``: the source's controls first, then accounts whose
    name shares a word with the role's, then by code. The order only helps the user find
    one; it never chooses."""
    words = {w for w in ROLE_LABELS[AccountRole(role)].lower().split() if len(w) > 3}

    def rank(account: dict) -> tuple:
        named = bool(words & set(str(account.get("name") or "").lower().split()))
        return (account["code"] not in controls, not named, account["code"])

    return [_brief(a) for a in sorted((a for a in chart.values() if _fits(role, a)), key=rank)]


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
    current and current_account (the code already set, and that chart account), controls (the source books' control accounts),
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
        code = current.get(role.value) or None
        rows.append({
            "role": role.value, "label": ROLE_LABELS[role], "group": _GROUP_OF[role.value],
            "required": role.value in needed, "current": code,
            "current_account": _brief(chart[code]) if code in chart else None,
            "controls": controls, "preselect": fitting[0] if len(controls) == 1 and fitting else None,
            "candidates": _ranked(role.value, chart, controls), "proposal": proposals[role.value],
        })
    return rows


def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReadinessError(refusal(f"posting.added_needs_{what}", f"Each added account needs a {what}."))
    return value.strip()


def _new_accounts(add_accounts) -> list[dict]:
    if add_accounts is None:
        return []
    if not isinstance(add_accounts, list) or not all(isinstance(a, dict) for a in add_accounts):
        raise ReadinessError(refusal("posting.added_not_a_list", "Added accounts must be a list of accounts."))
    out = []
    for a in add_accounts:
        role = a.get("role")
        if not is_role(role):
            raise ReadinessError(unknown_role(role))
        out.append({"role": role, "code": _text(a.get("code"), "code"), "name": _text(a.get("name"), "name"),
                    "account_type": _text(a.get("account_type"), "type")})
    return out


def _chosen(roles) -> dict[str, str]:
    if roles is None:
        return {}
    if not isinstance(roles, dict):
        raise ReadinessError(refusal("posting.roles_not_a_map", "Posting accounts must map each role to an account code."))
    out = {}
    for role, code in roles.items():
        if not is_role(role):
            raise ReadinessError(unknown_role(role))
        if code in (None, ""):
            continue
        if not isinstance(code, str):
            raise ReadinessError(refusal("posting.choose_account_for", f"Choose an account code for "
                                         f"{ROLE_LABELS[AccountRole(role)]}.", role=role))
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
    from celerp.services.company_lock import lock_chart, locked_company
    from celerp.services.journal_accounts import add_account, lock_accounts

    choices = choices or {}
    if not isinstance(choices, dict):
        raise ReadinessError(refusal("posting.roles_not_a_map", "Posting accounts must map each role to an account code."))
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
            raise ReadinessError(refusal("posting.two_accounts_chosen", f"{ROLE_LABELS[AccountRole(account['role'])]} "
                                         "has two accounts chosen.", role=account["role"]))
        chosen[account["role"]] = account["code"]
        existing = chart.get(account["code"])
        if existing is not None and (existing["name"], existing["account_type"]) != (
                account["name"], account["account_type"]):
            raise ReadinessError(refusal("posting.code_in_use", f"Account code {account['code']} is already in use. "
                                         "Choose that account or add one with another code.", code=account["code"]))
    final: dict[str, str] = {}
    problems: list[dict] = []
    unchosen: list[str] = []
    for row in rows:
        role, label = row["role"], row["label"]
        code = chosen.get(role)
        if row["current"]:
            if code and code != row["current"]:
                problems.append(refusal("posting.already_set", f"{label} is already set to account {row['current']}; "
                                        "change it in Settings after finishing.", role=role, code=row["current"]))
            continue
        code = code or row["preselect"]
        if code:
            final[role] = code
        elif row["required"]:
            unchosen.append(role)
    if unchosen:
        problems.append(refusal("posting.choose_accounts_for", f"Choose the posting account for: {_labels(unchosen)}.",
                                roles=unchosen))
    if problems:
        raise _refused(problems)
    for account in added:
        if account["code"] not in chart and final.get(account["role"]) == account["code"]:
            try:
                await add_account(session, company_id, account["code"], account["name"],
                                  account["account_type"])
            except HTTPException as exc:
                raise ReadinessError(exc.detail) from None
            chart[account["code"]] = {**account, "is_active": True, "has_children": False}
    held = await lock_accounts(session, company_id, set(final.values())) or {}
    trial = {**role_map(company.settings), **final}
    for role, code in final.items():
        problem = target_problem(role, trial, held.get(code))
        if problem:
            problems.append(problem)
    if problems:
        raise _refused(problems)
    settings = {**(company.settings or {}), SCHEMA_KEY: POSTING_ROLES_SCHEMA}
    settings.setdefault(SOURCE_CONTROLS_KEY, {})
    for role, code in final.items():
        settings = with_role(settings, role, code)
    scopes = {k: list(v) for k, v in (settings.get(SCOPES_KEY) or {}).items()}
    for role, codes in settings[SOURCE_CONTROLS_KEY].items():
        scopes.setdefault(role, []).extend(c for c in codes if c not in scopes[role])
    company.settings = {**settings, SCOPES_KEY: scopes}
    await session.flush()


def _status(role: str, current: dict[str, str], chart: dict[str, dict], required: bool) -> tuple[str, dict | None]:
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
            "currency": str(settings.get("currency") or "USD").upper(),
            "candidates": [_brief(chart[c]) for c in sorted(held) if c in chart]}


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
            "code_generated": bool((chart.get(code) or {}).get("code_generated")) if code else False,
            "status": status, "problem": problem,
            "earlier": [c for c in scope_list(settings, role.value) if c != code],
            "candidates": _ranked(role.value, chart, []),
        })
    return {"roles": rows, "older_stock": await _older_stock(session, company_id, settings, chart)}
